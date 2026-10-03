"""
访问受限诊断 —— 识别"拿不到数据"的各种原因, 并给出可排查的详细页面信息。

**为什么需要它**: 抓不到数据时, 表面的结果都是"0 条", 但底层原因差别极大, 处置方式
也完全不同:

| 类型 | 典型表现 | 处置方向 |
|---|---|---|
| ``login_required`` | 重定向到 /login, 有密码框 | 复用登录会话(storage_state) |
| ``permission_denied`` | **HTTP 401/403 但留在原 URL**, 页面显示"没有权限" | 该账号确实无权限, 或需要加入/报名 |
| ``risk_control`` | 风控页、"访问过于频繁"、Cloudflare 挑战 | 降速、换 IP、稍后重试 |
| ``captcha`` | 人机验证组件 | 降速或人工过一次验证 |
| ``rate_limited`` | 429 / "请求过于频繁" | 降速 |
| ``spa_shell`` | 页面几乎是空壳 | 内容异步渲染, 需要等接口 |
| ``server_error`` | 5xx | 站点故障, 稍后重试 |
| ``empty_page`` | 页面正常但没有列表结构 | 选择器问题, 或本来就没有数据 |

关键教训来自洛谷的训练页: 无权限时它返回 **HTTP 401 但 URL 不变**(不跳登录页),
由前端 JS 渲染出一个 ``Error - 洛谷`` / ``没有权限请求此资源。`` 的错误页。早先的实现
因为"401 需要鉴权"就直接归为"需要登录", 于是提示用户去配会话 —— 方向完全错了。
所以这里**先分类再给建议**, 并且把页面实际显示的内容一并取回来给用户看。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Page

from .models import AccessIssue, TextElement
from .page_diagnostics import collect_page_diagnostics, extract_codes, flatten_text, looks_like_spa_shell

# ---------------------------------------------------------------------------
# 关键词表: (正则, 命中说明, 该线索指向的类型, 权重)
# ---------------------------------------------------------------------------
_KEYWORDS: tuple[tuple[str, str, str, float], ...] = (
    # ---- 权限不足 ----
    (r"没有权限|无权限|无权访问|权限不足|没有访问权限", "页面提示没有权限", "permission_denied", 0.75),
    (r"permission\s+denied|access\s+denied|not\s+authorized|unauthorized", "英文权限不足提示", "permission_denied", 0.7),
    (r"仅(?:管理员|版主|内部人员|特定用户)|只有?.*才(?:能|可)查看", "提示仅特定角色可见", "permission_denied", 0.6),
    (r"未(?:被)?授权|没有授权", "提示未授权", "permission_denied", 0.6),
    # ---- 登录 ----
    (r"请先?登录|需要登录|未登录|登录后(?:才能)?(?:查看|访问|继续)", "页面提示需要登录", "login_required", 0.7),
    (r"登录已?过期|会话过期|重新登录|请重新登录", "提示登录态失效", "login_required", 0.7),
    (r"please\s+(?:log|sign)\s?in|login\s+required", "英文登录提示", "login_required", 0.6),
    # ---- 风控 / 拦截 ----
    (r"访问(?:过于)?频繁|操作(?:过于)?频繁|请稍后再试|请求过于频繁", "提示访问频率过高", "rate_limited", 0.7),
    (r"风控|风险(?:控制|提示)|异常(?:访问|请求|流量)|检测到(?:异常|风险)", "提示风控拦截", "risk_control", 0.75),
    (r"人机验证|安全验证|滑动验证|拖动滑块|请完成验证|验证你是|完成验证", "出现人机/安全验证", "captcha", 0.7),
    (r"verify\s+you\s+are\s+human|are\s+you\s+a\s+robot|unusual\s+traffic|checking\s+your\s+browser",
     "英文人机校验提示", "captcha", 0.7),
    (r"cf-browser-verification|cf_chl_|challenge-platform|just\s+a\s+moment", "Cloudflare 挑战页", "risk_control", 0.7),
    # 注意: 这里**不再**用 "captcha|recaptcha|..." 这类**泛指词**判定。
    # 大量正常页面都会提到它 —— pixiv 页脚写着 "This site is protected by reCAPTCHA",
    # 于是每次分析都被判成"需要人机验证", 而页面上根本没有要过的验证。是否真的在挑战,
    # 由 challenge.py 用"可见的挑战元素"来判断, 并在 detect_access_issue 里覆盖结论。
    (r"您的?(?:IP|ip)(?:地址)?(?:已被)?(?:封禁|限制|禁止)", "提示 IP 被封禁/限制", "risk_control", 0.8),
    (r"(?:access|request)?\s*blocked|已被?拦截|拦截了你的请求", "提示请求被拦截", "risk_control", 0.7),
    (r"地区(?:限制|不支持)|仅限(?:中国大陆|境内)访问|not\s+available\s+in\s+your\s+region", "地区限制", "risk_control", 0.6),
    # ---- 服务端错误 ----
    (r"服务(?:器)?(?:错误|异常|不可用)|内部错误|系统(?:繁忙|错误)|稍后重试", "服务端错误提示", "server_error", 0.5),
    (r"(?:50[0-9])\s*(?:error|错误)?|bad\s+gateway|service\s+unavailable", "5xx 错误字样", "server_error", 0.6),
    (r"找不到(?:页面|该页)|页面不存在|404|not\s+found", "页面不存在", "not_found", 0.5),
    # ---- 其它 ----
    (r"出错啦|出错了|发生错误|页面异常|something\s+went\s+wrong|an\s+error\s+occurred", "通用错误页", "unknown", 0.3),
    (r"需要(?:报名|加入|申请)|先(?:报名|加入)才(?:能|可)", "需要先报名/加入", "permission_denied", 0.55),
)

#: 登录相关 URL 片段(仅在**没有**重定向时作为弱线索)
_LOGIN_URL_HINTS = ("/login", "/signin", "/sign-in", "/logon", "/auth", "/sso", "/passport", "/session/new")


def _same_site(a: str, b: str) -> bool:
    try:
        ha = (urlparse(a).hostname or "").lower().removeprefix("www.")
        hb = (urlparse(b).hostname or "").lower().removeprefix("www.")
        return bool(ha) and ha == hb
    except ValueError:
        return False


def _split_url(url: str) -> tuple[str, str]:
    """拆出 (去掉 fragment 的 URL, fragment)。"""
    parts = urlparse(url or "")
    base = parts._replace(fragment="").geturl()
    return base, parts.fragment


def _normalize(url: str) -> str:
    """归一化用于比较: 去 fragment、去结尾斜杠。"""
    base, _ = _split_url(url)
    return base.rstrip("/")


def url_changed(requested: str, final: str) -> tuple[bool, str]:
    """判断是否发生了**实质**的 URL 变化, 返回 (是否变化, 说明)。

    这里刻意忽略 fragment(``#scoreboard``)。早先的实现直接比较整串 URL, 于是
    ``/training/1096881`` 与 ``/training/1096881#scoreboard`` 被判为"重定向",
    还会打印出"重定向到其它域名: host → None"这种误导性理由 —— 实际上什么都没跳。
    """
    if not requested or not final:
        return False, ""

    req_base, req_frag = _split_url(requested)
    fin_base, fin_frag = _split_url(final)

    if req_base.rstrip("/") == fin_base.rstrip("/"):
        if req_frag != fin_frag:
            return False, f"仅锚点变化({req_frag or '无'} → {fin_frag or '无'}), 页面并未跳转"
        return False, ""

    if not _same_site(requested, final):
        rh = urlparse(requested).hostname or requested
        fh = urlparse(final).hostname or final
        return True, f"跨域名跳转: {rh} → {fh}"
    return True, f"路径被改写: {urlparse(requested).path} → {urlparse(final).path}"


def _build_text_elements(raw: dict[str, Any]) -> list[TextElement]:
    """把采集到的诊断信息整理成有序的文本元素列表。"""
    order = {
        "heading": 0,
        "error": 1,
        "form": 2,
        "captcha": 3,
        "button": 4,
        "message": 5,
        "link": 6,
        "meta": 7,
    }
    elements: list[TextElement] = []

    for item in raw.get("headings") or []:
        text = flatten_text(item.get("text"), 160)
        if text:
            elements.append(
                TextElement(tag=item.get("tag", ""), selector=item.get("selector", ""), text=text, role="heading")
            )

    for item in raw.get("error_blocks") or []:
        text = flatten_text(item.get("text"), 300)
        if text:
            elements.append(TextElement(tag="div", selector=item.get("selector", ""), text=text, role="error"))

    for form in raw.get("forms") or []:
        action = form.get("action") or "(当前页)"
        inputs = ", ".join(n for n in (form.get("inputs") or []) if n)
        text = f"表单 {form.get('method', 'get').upper()} {action}"
        if inputs:
            text += f" · 字段: {inputs}"
        if form.get("has_password"):
            text += " · 含密码框"
        elements.append(TextElement(tag="form", selector=form.get("selector", ""), text=text, role="form"))

    for selector in raw.get("captcha") or []:
        elements.append(TextElement(tag="div", selector=selector, text="验证码 / 人机校验组件", role="captcha"))

    for text in raw.get("buttons") or []:
        value = flatten_text(text, 40)
        if value:
            elements.append(TextElement(tag="button", selector="", text=value, role="button"))

    elements.sort(key=lambda e: order.get(e.role, 9))

    # 去重并按角色保留合理数量, 避免把导航刷成几十条
    limits = {"heading": 6, "error": 6, "form": 4, "captcha": 3, "button": 8, "message": 6, "link": 8, "meta": 4}
    unique: list[TextElement] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for element in elements:
        key = f"{element.role}|{element.text[:50]}"
        if key in seen:
            continue
        if counts.get(element.role, 0) >= limits.get(element.role, 6):
            continue
        seen.add(key)
        counts[element.role] = counts.get(element.role, 0) + 1
        unique.append(element)
    return unique


#: 每种类型的标题与建议
_PLAYBOOK: dict[str, tuple[str, str, list[str]]] = {
    "login_required": (
        "需要登录才能访问",
        "站点要求登录身份, 当前会话是匿名的。",
        [
            "在有头模式下手动登录一次, 把会话保存为 data/session.json, 再把「会话持久化文件」配置指向它",
            "确认目标页面是否本来就要求登录(有些详情页/榜单页对匿名用户不开放)",
            "可切换到「网络抓包」看看页面调用的接口是否返回 401 —— 有些数据匿名可用, 只是接口不同",
        ],
    ),
    "permission_denied": (
        "当前身份没有访问权限",
        "页面加载成功, 但服务端判定当前身份无权查看该资源 —— 注意这**不是**登录问题。",
        [
            "确认该资源是否需要报名/加入后才能查看(例如训练、比赛、班级内页面)",
            "确认当前会话对应的账号是否真的有该资源的查看权限",
            "若确认有权限, 检查是否缺少必要的请求头(如 Referer)或 CSRF token",
            "换一个公开的同类页面测试, 确认框架本身工作正常",
        ],
    ),
    "risk_control": (
        "请求被风控/安全策略拦截",
        "站点识别到异常访问并进行了拦截, 通常与访问频率、IP 信誉或指纹有关。",
        [
            "点界面上的『手动过验证』手动过一次, 通过后通行凭据会存进会话",
            "把「随机限速区间」调大到 3~8 秒, 降低并发页面上限(建议 1~2)",
            "更换代理 IP(配置代理池后框架会自动轮换)",
            "开启「反爬增强」插件: 它会在导航后模拟人类滚动与停留节奏, 并识别拦截页",
            "稍后重试 —— 风控通常是临时的, 隔一段时间再试往往就通了",
        ],
    ),
    "captcha": (
        "站点要求人机验证",
        "出现了验证码/人机校验组件, 说明当前访问被判定为可疑流量。",
        [
            "点界面上的『手动过验证』: 会打开一个可见窗口, 你过完验证后通行凭据自动存进会话",
            "降低抓取频率并减少并发, 验证码通常由频率触发",
            "换 IP 后重试(同一 IP 反复触发会进入更严格的风控)",
            "关闭无头模式(SC_BROWSER__HEADLESS=false)有时能减少触发概率",
            "某些验证码必须人工完成一次; 如需长期稳定采集, 建议改用官方 API",
        ],
    ),
    "rate_limited": (
        "请求过于频繁被限速",
        "站点明确提示访问频率过高。",
        [
            "把「随机限速区间」调大到 5~10 秒",
            "把「并发页面上限」降到 1",
            "减少单次任务的最大翻页数, 分批次采集",
        ],
    ),
    "server_error": (
        "目标站点服务端错误",
        "错误来自站点一方, 不是框架或选择器的问题。",
        [
            "稍后重试, 并适当降低请求频率",
            "确认该页面在浏览器里能正常打开",
            "若持续 5xx, 可能是站点故障或该路径已下线",
        ],
    ),
    "not_found": (
        "页面不存在(404)",
        "目标地址可能已失效或写错了。",
        [
            "确认 URL 拼写, 并检查是否缺少必要的路径前缀",
            "从站点首页/列表页重新找到该资源的最新地址",
        ],
    ),
    "spa_shell": (
        "页面内容尚未渲染(疑似空壳)",
        "抓到的几乎是空页面, 内容由 JavaScript 异步加载, 而这次没有等到。",
        [
            "增大「额外等待」秒数(给异步接口留时间)",
            "增大 `SC_CRAWLER__NETWORK_SETTLE`, 或改用「网络抓包」直接取接口数据",
            "若接口被拦截, 会同时出现权限/风控类提示 —— 优先解决那一项",
        ],
    ),
    "empty_page": (
        "页面正常但没有可提取的列表",
        "没有检测到访问受限的迹象, 更可能是选择器或页面结构问题。",
        [
            "在「结构分析」页确认识别到了哪些候选列表",
            "检查抓取目标描述是否与页面内容匹配",
            "该页面可能确实不是列表页(如详情页、需要先进入列表页再点击)",
        ],
    ),
    "unknown": (
        "页面异常但原因不明",
        "检测到页面显示了错误内容, 但无法归入已知类型。",
        [
            "展开下方的页面文本, 人工判断站点到底提示了什么",
            "在浏览器里用同样参数访问该页面做对比",
            "若是需要登录的站点, 先把会话配置好再试",
        ],
    ),
}


async def detect_access_issue(
    page: Page,
    requested_url: str,
    http_status: Optional[int] = None,
    *,
    allow_login_probe: bool = True,
    challenge_confirmed: Optional[bool] = None,
) -> AccessIssue:
    """诊断当前页面是否存在访问受限, 并给出分类与详细页面信息。

    ``challenge_confirmed``: 由 :mod:`smartcrawler.challenge` 给出的**权威**人机验证结论
    (``True`` 真的在挑战 / ``False`` 没有 / ``None`` 未检测)。传进来是为了纠正本模块基于
    关键词与元素线索的**推断** —— 那些线索很容易误报(例如页脚一句
    "This site is protected by reCAPTCHA" 就会命中)。

    这个函数**不会抛异常**: 诊断属于附加能力, 任何失败都退化为"无问题", 绝不影响抓取。
    """
    issue = AccessIssue(requested_url=requested_url, http_status=http_status)

    raw = await collect_page_diagnostics(page)
    if raw.get("error"):
        logger.debug(f"页面诊断采集失败: {raw['error']}")
        # 采集失败时退化为纯 URL/状态码判断
        changed, why = url_changed(requested_url, page.url)
        issue.final_url = page.url
        issue.redirected = changed
        if changed:
            issue.reasons.append(why)
        if http_status in (401, 403):
            issue.issue_type = "permission_denied"
            issue.detected = True
            issue.confidence = 0.6
            issue.reasons.append(f"HTTP {http_status}(未能读取页面内容)")
        return issue

    final_url = str(raw.get("url") or page.url)
    issue.final_url = final_url
    issue.page_title = str(raw.get("title") or "")
    issue.visible_text = str(raw.get("body_text") or "")
    issue.main_text = str(raw.get("main_text") or "")
    issue.dom_nodes = int(raw.get("dom_nodes") or 0)
    issue.html_length = int(raw.get("html_length") or 0)

    changed, why = url_changed(requested_url, final_url)
    issue.redirected = changed
    if changed:
        issue.reasons.append(why)
    elif "锚点" in why:
        # 锚点变化不是跳转, 但也值得记录一句, 免得用户以为页面被替换了
        issue.reasons.append(why)

    scores: dict[str, float] = {}
    clues: dict[str, Any] = {}

    def add(kind: str, weight: float) -> None:
        scores[kind] = scores.get(kind, 0.0) + weight

    # ---- 线索 1: 关键词 ----
    haystack = f"{issue.page_title}\n{issue.visible_text}\n{issue.main_text}"
    matched: list[str] = []
    for pattern, label, kind, weight in _KEYWORDS:
        if re.search(pattern, haystack, re.IGNORECASE):
            matched.append(label)
            issue.reasons.append(label)
            add(kind, weight)
    clues["keyword_hits"] = matched

    # ---- 线索 2: 页面结构 ----
    clues["password_inputs"] = int(raw.get("password_inputs") or 0)
    clues["form_count"] = len(raw.get("forms") or [])
    clues["captcha_elements"] = len(raw.get("captcha") or [])
    clues["auth_link_count"] = len(raw.get("auth_links") or [])
    clues["page_title"] = issue.page_title

    if clues["password_inputs"] > 0:
        add("login_required", 0.35)
        issue.reasons.append(f"页面存在 {clues['password_inputs']} 个密码输入框")
    login_forms = [f for f in (raw.get("forms") or []) if f.get("has_password")]
    if login_forms:
        add("login_required", 0.25)
        issue.reasons.append(f"存在 {len(login_forms)} 个含密码框的表单")
    if clues["captcha_elements"] > 0:
        add("captcha", 0.3)
        issue.reasons.append(f"检测到 {clues['captcha_elements']} 个验证码/人机校验组件")

    # ---- 线索 3: 重定向 ----
    if changed:
        final_is_login = any(hint in final_url.lower() for hint in _LOGIN_URL_HINTS)
        if final_is_login and allow_login_probe:
            add("login_required", 0.35)
            issue.reasons.append(f"跳转目标带有登录语义: {urlparse(final_url).path or final_url}")
        else:
            add("unknown", 0.15)  # 跳转到非登录页: 提示有变化, 但不定性

    # ---- 线索 4: HTTP 状态码 ----
    # 注意: 401/403 只说明"服务端拒绝", 既可能是要登录, 也可能是没权限。
    # 因此这里给出中性线索, 真正的分类交给上面的关键词与页面结构决定。
    if http_status == 401:
        add("permission_denied", 0.3)
        add("login_required", 0.15)
        issue.reasons.append("HTTP 401 表示请求未通过身份校验")
    elif http_status == 403:
        add("permission_denied", 0.4)
        issue.reasons.append("HTTP 403 表示服务端拒绝该请求")
    elif http_status == 429:
        add("rate_limited", 0.7)
        issue.reasons.append("HTTP 429 表示请求过于频繁")
    elif http_status == 404:
        add("not_found", 0.6)
        issue.reasons.append("HTTP 404 表示页面不存在")
    elif http_status is not None and 500 <= http_status < 600:
        add("server_error", 0.7)
        issue.reasons.append(f"HTTP {http_status} 表示服务端错误")

    # ---- 线索 5: 空壳检测 ----
    # 只在"没有明确错误状态码"时才作为线索。原因: 服务端返回 401/403/5xx 时, 即使
    # 页面可见文本很少, 那也是**服务端明确给出的完整错误响应**, 而不是"还没渲染完"。
    # 例如洛谷的权限页只有几十个字, 把它同时标成 spa_shell 会给出"疑似尚未渲染完成"
    # 这种误导性理由, 让用户去调等待时间而不是解决权限问题。
    error_status = http_status is not None and (http_status >= 400)
    visible_chars = len(re.sub(r"\s+", "", issue.visible_text))
    if not error_status and looks_like_spa_shell(
        issue.visible_text,
        issue.html_length,
        issue.dom_nodes,
        int(raw.get("interactive_count") or 0),
    ):
        add("spa_shell", 0.5)
        issue.reasons.append(f"页面可见内容极少({visible_chars} 个非空白字符)且无可交互元素, 疑似尚未渲染完成")

    # ---- 归因 ----
    if scores:
        kind = max(scores, key=lambda k: scores[k])
        issue.issue_type = kind
        issue.confidence = round(min(scores[kind], 1.0), 2)
        # 只有分数够高才判定"有问题", 避免把正常页面上的"登录"链接也算成限制
        issue.detected = issue.confidence >= 0.5
    else:
        issue.issue_type = "empty_page"
        issue.confidence = 0.0
        issue.detected = False

    # ---- 人机验证的**权威结论**覆盖上面的推断 ----
    # 上面的关键词/元素线索都是"猜", 而 challenge.py 用的是"屏幕上真的有可见的挑战框吗",
    # 那才是事实。因此:
    #   · 真的在挑战 → 强制归为 captcha(无论关键词与元素线索如何);
    #   · 没在挑战   → 把 captcha 从候选里剔除。否则 pixiv 那种页脚写着
    #     "This site is protected by reCAPTCHA" 的正常页面会被判成"需要人机验证",
    #     用户看到『手动过验证』却根本没有验证要过 —— 比不提示更糟。
    if challenge_confirmed is not None and "captcha" in scores:
        if challenge_confirmed:
            scores["captcha"] = max(scores["captcha"], 0.8)
            issue.reasons.insert(0, "屏幕上存在可见的人机验证挑战框(已确认)")
        else:
            scores.pop("captcha", None)
            clues["captcha_elements"] = 0
            # 把基于误报得出的理由一并撤掉, 免得诊断卡里还留着"检测到验证码组件"
            issue.reasons = [r for r in issue.reasons if "验证码" not in r and "人机" not in r]

        if scores:
            kind = max(scores, key=lambda k: scores[k])
            issue.issue_type = kind
            issue.confidence = round(min(scores[kind], 1.0), 2)
            issue.detected = issue.confidence >= 0.5
        else:
            issue.issue_type = "empty_page"
            issue.confidence = 0.0
            issue.detected = False

        if not challenge_confirmed and issue.detected:
            issue.reasons.append("页面上没有可见的人机验证挑战框(仅有脚本锚点)")

    issue.scores = {k: round(v, 2) for k, v in scores.items()}

    # ---- HTTP 401/403 是硬信号: 即使关键词没命中也要报出来 ----
    if not issue.detected and http_status in (401, 403, 429):
        issue.detected = True
        issue.issue_type = issue.issue_type if scores else ("permission_denied" if http_status != 429 else "rate_limited")
        issue.confidence = max(issue.confidence, 0.55)

    issue.clues = clues

    if issue.detected:
        issue.text_elements = _build_text_elements(raw)
        issue.error_codes = extract_codes(
            issue.visible_text, issue.page_title, *[str(v) for v in (raw.get("win_vars") or {}).values()]
        )
        # meta 与 window 变量里可能藏着状态码
        issue.metadata = {
            **(raw.get("meta") or {}),
            **{k: v for k, v in (raw.get("win_vars") or {}).items() if v},
        }
        title, explanation, tips = _PLAYBOOK.get(issue.issue_type, _PLAYBOOK["unknown"])
        issue.title = title
        issue.explanation = explanation
        issue.suggestions = list(tips)
        if issue.redirected:
            issue.suggestions.append(f"当前实际访问的是: {issue.final_url}")
        if issue.error_codes:
            issue.suggestions.append(
                "页面出现的错误码/请求 ID: "
                + ", ".join(f"{k}={v}" for k, v in issue.error_codes.items())
                + " —— 可据此向站点反馈或自查"
            )
        if clues["auth_link_count"] and issue.issue_type == "login_required":
            issue.suggestions.append("页面存在登录入口链接, 说明站点确实要求身份认证")
        else:
            issue.suggestions.append("展开下方的页面文本, 与浏览器中看到的内容做对比")

        logger.warning(f"访问受限[{issue.issue_type}]({issue.confidence:.0%}): {issue.summary()}")
        for reason in issue.reasons[:6]:
            logger.info(f"  · {reason}")

    return issue


def build_empty_page_issue(requested_url: str, final_url: str = "", http_status: Optional[int] = None) -> AccessIssue:
    """构造"页面正常但没数据"的说明, 供提取器在 0 条时使用。"""
    issue = AccessIssue(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        http_status=http_status,
        issue_type="empty_page",
        confidence=0.0,
        detected=False,
    )
    title, explanation, tips = _PLAYBOOK["empty_page"]
    issue.title = title
    issue.explanation = explanation
    issue.suggestions = list(tips)
    return issue


__all__ = [
    "AccessIssue",
    "build_empty_page_issue",
    "detect_access_issue",
    "url_changed",
]
