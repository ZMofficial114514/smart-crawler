"""
SmartCrawler 主爬虫模块 —— 各模块的编排中心。

SmartCrawler.crawl() 的完整数据流:

    url ──▶ RobotsChecker(合规) ──▶ RateLimiter(限速)
        ──▶ BrowserManager.goto(带重试)
        ──▶ NetworkRecorder(后台捕获 XHR/fetch/WS)
        ──▶ StructureAnalyzer(DOM 结构报告)
        ──▶ 规则来源三选一:
              ① 调用方直接传入 ExtractionRule
              ② AIEngine.analyze_page(自然语言 goal -> 规则)
              ③ 规则引擎 build_rule_from_structure(离线降级)
        ──▶ Extractor(dom 模式在页面上提取 / json 模式在网络捕获上提取)
        ──▶ 分页跟随(下一页点击 + 重复提取)
        ──▶ 去重(内容哈希) / 增量过滤(状态文件)
        ──▶ Storage.save(json/jsonl/csv/sqlite + Webhook)
        ──▶ TaskResult

⚠️ 合规声明: 本框架默认遵守 robots.txt、默认随机限速 1~3 秒。
   仅限用于合法授权的数据采集场景, 使用者需自行承担合规责任。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Optional, Type

from loguru import logger
from pydantic import BaseModel

from .access_control import build_empty_page_issue, detect_access_issue
from .ai import AIEngine
from .anti_spider import RobotsChecker
from .browser import BrowserManager
from .challenge import detect_challenge
from .config import Settings, get_settings
from .extractor import Extractor
from .lazy_load import scroll_to_load
from .login_state import detect_login_state
from .overlay import dismiss_overlays
from .plugins.builtin._media_transform import MEDIA_LIMIT_KEY, parse_count_from_goal
from .models import (
    ExtractionRule,
    ExtractedItem,
    NetworkRecord,
    PageStructureReport,
    TaskResult,
)
from .network import NetworkRecorder
from .plugins.base import PluginContext
from .plugins.manager import PluginManager
from .storage import Storage, load_state, save_state, update_seen_hashes
from .structure import StructureAnalyzer
from .utils import stable_hash, truncate

#: 这些访问受限类型意味着"当前页面根本不是目标内容", 提取它只会拿到错误页的导航/页脚。
#: 注意 ``empty_page`` / ``spa_shell`` 不在其中 —— 那两种情况页面本身是正常内容, 只是
#: 没有被识别出列表, 仍然值得按用户给的规则尝试提取。
_BLOCKING_ISSUE_TYPES = frozenset(
    {
        "permission_denied",
        "login_required",
        "risk_control",
        "captcha",
        "rate_limited",
        "server_error",
        "not_found",
    }
)

#: 判定"页面确实有可抓内容"的下限。取这两个指标而不是"有没有候选列表", 是因为
#: 有些列表结构检测不出来(懒加载、非重复结构), 但页面上确实有大量链接与图片 ——
#: 那种情况应该继续让提取规则去试, 而不是直接放弃。
_MIN_CONTENT_LINKS = 20
_MIN_CONTENT_IMAGES = 5


def _page_has_content(report: Optional[PageStructureReport]) -> bool:
    """页面是否**确实**有可抓内容(用于避免把"带登录浮层的正常页"当成拦截页)。

    证据优先级: 有候选列表 > 链接/图片数量足够多。用"事实"而不是"诊断推断"来决定
    要不要继续提取 —— duitang 就是典型: 诊断看到密码框判 login_required, 但页面上
    有 230 个链接和 55 张图, 内容其实完全可抓。

    **但"有候选列表"这一条不能无条件成立。** 实测(洛谷 401 页): 错误页的
    **页脚导航**也会被识别成候选列表, 于是框架兴冲冲提取出 7 条"图片上传/云剪贴板/
    主题商店/咕值排名…" —— 全是站点工具链接, 与用户要的数据毫无关系。
    这比"0 条"更糟: 用户以为抓到了东西。

    所以要在"有候选"之外再加一道**质量判据**:
      - 候选里含图片字段, 或
      - 候选条目数达到一定规模(页脚导航通常只有几条到十几条), 或
      - 候选的条目选择器带语义标记(article/product/card/item/post/…),
        **且**不是纯导航容器(nav/footer/aside/header/menu/breadcrumb)。
    三条都不满足时, 即使有候选也按"没有可抓内容"处理。
    """
    if report is None:
        return False
    if report.candidate_lists:
        if any(_candidate_looks_like_data(c) for c in report.candidate_lists):
            return True
        # 候选全是导航性质的: 不据此认为有内容, 继续看链接/图片的数量
    stats = report.dom_stats or {}
    links = int(stats.get("links") or 0)
    images = int(stats.get("images") or 0)
    return links >= _MIN_CONTENT_LINKS or images >= _MIN_CONTENT_IMAGES


#: 条目选择器里出现这些词, 通常意味着"数据卡片"而非导航菜单
_DATA_HINTS = (
    "article", "product", "card", "item", "post", "entry", "result",
    "thumb", "figure", "media", "video", "story", "list-item", "goods",
)
#: 出现这些词则明确是导航/页脚/侧栏
_NAV_HINTS = (
    "nav", "footer", "aside", "header", "menu", "breadcrumb", "sidebar",
    "toolbar", "pagination", "pager", "tag-list", "social",
)
#: 条目少于此数时, 除非有图片字段, 否则不足以证明"页面有数据"
_MIN_DATA_ITEMS = 8


def _candidate_looks_like_data(cand: Any) -> bool:
    """判断一个候选列表是否"像数据区", 而不是站点导航/页脚菜单。

    存在的意义: 拦截页上唯一"结构完整"的东西往往就是页脚导航。把它当数据提取,
    会把错误页的菜单当成抓取结果 —— 实测洛谷 401 页就是这么产出 7 条垃圾的。
    """
    fields = getattr(cand, "sample_fields", None) or []
    # 有图片字段 -> 基本可以确定是数据卡片(导航栏极少带图)
    if any(
        "image" in str(f.get("name", "")).lower()
        or "thumb" in str(f.get("name", "")).lower()
        or str(f.get("attribute", "")).lower() in ("src", "srcset", "data-src")
        for f in fields
    ):
        return True

    selector = str(getattr(cand, "item_selector", "") or "").lower()
    container = str(getattr(cand, "container_selector", "") or "").lower()
    haystack = f"{selector} {container}"

    # 明确的导航容器 -> 直接判否(除非上面已因图片字段通过)
    if any(h in haystack for h in _NAV_HINTS):
        return False
    # 语义化的数据标记 -> 判是
    if any(h in haystack for h in _DATA_HINTS):
        return True
    # 兜底: 条目够多也算(页脚菜单通常很短)
    return int(getattr(cand, "count", 0) or 0) >= _MIN_DATA_ITEMS

COMPLIANCE_NOTICE = (
    "SmartCrawler 合规提示: 请确保目标网站允许采集(robots.txt / 服务条款), "
    "本工具仅用于合法授权的数据采集场景。"
)


class SmartCrawler:
    """智能爬虫主类: 组装浏览器/网络/结构/AI/提取/存储各模块。"""

    #: 访问受限诊断的短轮询参数(应对 SPA 渲染延迟, 见 _diagnose_access)
    DIAGNOSE_RETRIES = 4
    DIAGNOSE_INTERVAL = 1.5

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.browser = BrowserManager(self.settings)
        self.recorder = NetworkRecorder(self.settings)
        self.structure = StructureAnalyzer(self.settings)
        self.ai = AIEngine(self.settings)
        self.extractor = Extractor(self.settings)
        self.storage = Storage(self.settings.storage)
        self.robots = RobotsChecker(self.settings)
        self.plugins = PluginManager(self.settings)
        self._lock = asyncio.Lock()  # 串行化任务(共享一个浏览器实例)
        self._notice_shown = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动浏览器等资源(幂等)。"""
        await self.browser.start()
        if not self._notice_shown:
            logger.info(COMPLIANCE_NOTICE)
            self._notice_shown = True

    async def close(self) -> None:
        """释放全部资源。"""
        await self.browser.close()

    async def __aenter__(self) -> "SmartCrawler":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 核心抓取
    # ------------------------------------------------------------------
    async def crawl(
        self,
        url: str,
        goal: Optional[str] = None,
        rule: Optional[ExtractionRule] = None,
        format: Optional[str] = None,
        output: Optional[str] = None,
        max_pages: Optional[int] = None,
        item_model: Optional[Type[BaseModel]] = None,
        incremental: Optional[bool] = None,
        use_ai: bool = True,
        extra_wait: float = 0.0,
        plugin_ids: Optional[list[str]] = None,
        on_progress: Optional[Callable[[str, str], None]] = None,
        deep_scroll: int = 0,
        scroll_confirm: Optional[Callable[..., Any]] = None,
        scroll_progress: Optional[Callable[[int, Any], None]] = None,
        scroll_rounds: Optional[int] = None,
        media_limit: Optional[int] = None,
    ) -> TaskResult:
        """抓取一个列表页(自动跟随分页), 返回 TaskResult。

        参数:
            url         : 目标页面
            goal        : 自然语言抓取目标, 如 "抓取所有商品名称和价格" (AI 可用时生效)
            rule        : 显式提取规则(优先级最高, 传入则忽略 goal)
            format/output: 输出格式与路径; 两者都为 None 时不落盘, 仅返回结果
            max_pages   : 最多抓取页数(默认取配置 crawler.max_depth)
            item_model  : 可选 Pydantic 模型, 对每条数据做结构校验
            incremental : 增量模式(默认取配置), 只输出与上次不同的内容
            use_ai      : 是否允许 AI 生成规则
            extra_wait  : 页面加载后额外等待秒数(给懒加载/异步请求留时间)
            plugin_ids  : 只运行这些插件(None=用插件自身的启用配置)
            on_progress : 进度回调 (level, message), 供 Web 层把插件日志推给界面
        """
        started = time.perf_counter()
        task_id = uuid.uuid4().hex[:8]
        result = TaskResult(task_id=task_id, url=url, goal=goal)
        max_pages = max(1, max_pages or self.settings.crawler.max_depth)
        if incremental is None:
            incremental = self.settings.crawler.incremental

        # 插件上下文贯穿整个任务: data 字典用于插件之间传递信息(如注入过的请求头)
        self.plugins.begin_task()
        # 媒体下载数量约束: 优先用调用方给的, 否则从自然语言目标里解析("爬取前三张")。
        # 放在 ctx.data 里让所有下载插件共用 —— 用户说"前三张"就是本次任务只要三张,
        # 不该被插件里那个长期配置(默认 200)覆盖。
        task_media_limit = media_limit
        if not task_media_limit and goal:
            task_media_limit = parse_count_from_goal(goal)
            if task_media_limit:
                logger.info(f"从抓取目标里识别到数量约束: 最多 {task_media_limit} 条/个")
        ctx = PluginContext(
            settings=self.settings,
            url=url,
            crawler=self,
            notify=on_progress or (lambda level, message: None),
            data={MEDIA_LIMIT_KEY: task_media_limit} if task_media_limit else {},
        )

        await self.start()
        async with self._lock:
            # ---------- 1. 合规检查 ----------
            if not await self.robots.can_fetch(url):
                result.errors.append("robots.txt 禁止抓取该 URL, 任务终止(合规优先)")
                return self._finalize(result, started)

            # ---------- 2. 插件: 任务开始 ----------
            await self.plugins.run_async("on_start", ctx, only=plugin_ids)

            # ---------- 3. 打开页面并导航 ----------
            self.recorder.clear()
            page = await self.browser.new_page()
            ctx.page = page
            self.recorder.attach(page)
            try:
                # 插件可以在导航前注入请求头等(必须在 goto 之前)
                await self.plugins.run_async("before_navigate", ctx, only=plugin_ids)

                resp = await self.browser.goto(page, url)
                if resp is None:
                    result.errors.append(f"页面导航失败: {url}")
                    return self._finalize(result, started, page)

                ctx.url = page.url or url
                status_code = getattr(resp, "status", None)
                settle = self.settings.crawler.network_settle + extra_wait
                await self.browser.human_scroll(page, times=1, step=400)
                await asyncio.sleep(min(settle, 10.0))

                # ---------- 3.2 关掉挡住内容的登录浮层 ----------
                # 很多站点(堆糖典型)弹一个登录遮罩, 但**内容其实已经在页面里**, 只是被
                # 半透明遮罩盖住。实测 duitang 搜索页: 遮罩在时 55 张图 / 230 链接,
                # 叉掉后一模一样 —— 内容一条没少。若不处理, 诊断会看到密码框判成
                # login_required 进而跳过提取, 用户看到的就是"检测到登录页就终止"。
                dismissed = await dismiss_overlays(page)
                if dismissed.dismissed:
                    result.overlays = dismissed.to_dict()
                    if on_progress:
                        on_progress("INFO", f"遮挡处理: {dismissed.summary}")
                    await asyncio.sleep(0.6)  # 等遮罩消失与内容重排

                # ---------- 3.5 懒加载: 模拟滚动把内容滚出来 ----------
                # 现代 SPA 首屏只渲染骨架(容器在、内容是空的)。不滚就只能拿到一条
                # "看起来完整的空页面": 导航都在, 主内容区一个条目都没有。
                # 实测 pixiv 登录后首页: 不滚的简化树里 /artworks/ 链接 0 个、空容器
                # 118 个; 滚动后才出现作品网格。
                scroll = await self._maybe_scroll(
                    page,
                    deep_scroll=deep_scroll,
                    confirm=scroll_confirm,
                    on_progress=scroll_progress,
                    scroll_rounds=scroll_rounds,
                )
                if scroll:
                    result.lazy_load = scroll.to_dict()
                    if on_progress and scroll.grew:
                        on_progress("INFO", f"懒加载: {scroll.summary()}")
                    if on_progress and scroll.infinite and scroll.reason != "user_stopped":
                        on_progress(
                            "INFO",
                            "内容仍在持续增长(疑似无限流), 已按上限停止 —— "
                            "可在界面上选择『继续向下滚动』加载更多",
                        )

                # 插件: 导航后(补伪装/滚动/停留/识别拦截页)
                await self.plugins.run_async("after_navigate", ctx, only=plugin_ids)

                # ---------- 4. 人机验证(先判定, 因为它是权威依据) ----------
                # 有些站点(如 pixiv 登录)总是拉起验证, 还有站点抓取中途突然弹挑战。
                # 这类验证只有人能过, 框架不尝试绕过 —— 只如实识别并告诉用户可以
                # "手动过一次验证", 之后会话里带上通关凭据即可继续。
                #
                # 顺序很关键: 先拿到权威结论, 再交给访问诊断去纠正它的关键词推断。
                # 否则页脚一句 "This site is protected by reCAPTCHA" 就会被判成
                # "需要人机验证", 用户点开却根本没有验证要过。
                challenge = await detect_challenge(page)
                if challenge.detected:
                    result.challenge = challenge.to_dict()
                    if on_progress:
                        on_progress(
                            "WARNING",
                            f"页面要求{challenge.kind} —— 可在界面上点『手动过验证』"
                            "(打开可见窗口, 过完自动保存会话)",
                        )

                # ---------- 4.5 访问受限诊断 ----------
                # 拿不到数据的原因有很多种(需登录/无权限/风控/验证码/空壳), 表象都是
                # "0 条数据"但处置方式完全不同。这里做分类诊断, 并把页面实际显示的
                # 文本、关键元素、错误码一并记进结果, 由界面提醒用户。
                issue = await self._diagnose_access(
                    page, url, status_code, challenge_confirmed=challenge.detected
                )
                result.access_issue = issue
                if issue.detected:
                    result.errors.append(issue.summary())
                    if on_progress:
                        on_progress("WARNING", f"访问受限[{issue.issue_type}]: {issue.summary()}")

                # ---------- 5. 结构分析 ----------
                report = await self.structure.analyze(page)
                report.access_issue = issue if issue.detected else None
                # 登录状态同样记进报告: 抓取时也需要知道"这份结构是匿名视图还是登录视图"
                login = await detect_login_state(page, session_restored=self.browser.session_restored)
                report.login_state = login.to_dict()
                result.login_state = login.to_dict()
                if on_progress and login.state == "anonymous":
                    on_progress("INFO", f"当前为匿名访问({login.summary()}), 登录后页面结构可能不同")
                result.structure_report = report

                # ---------- 5.5 严重受阻时不再提取 ----------
                # 已经诊断出"这是权限/风控/验证码拦截页"时, 页面上的结构是**错误页的
                # 导航与页脚**, 不是目标内容。继续提取只会得到几条页脚链接, 既污染结果
                # 又让人误以为"抓到东西了"。实测: 洛谷 401 页会因此提出几条"关于洛谷/
                # 帮助中心"之类的条目。
                #
                # **但要先确认页面是不是真的没内容**: 有些站点(堆糖典型)只是弹了个登录
                # 浮层, 内容其实都在 —— 遮罩已被上面关掉, 这时候页面是正常可抓的。
                # 判据用"有没有可抓的内容"(候选列表 / 足够多的链接与图片), 而不是只看
                # 诊断类型: 诊断是从页面特征推断的, 内容存在与否是事实。
                has_content = _page_has_content(report)
                if issue.detected and issue.issue_type in _BLOCKING_ISSUE_TYPES:
                    if has_content:
                        result.errors.append(
                            f"页面带有{issue.title or issue.issue_type}特征, 但检测到可抓内容, "
                            "已继续尝试提取(若结果不符预期, 请先按提示处理该问题)"
                        )
                        if on_progress:
                            on_progress(
                                "INFO",
                                f"页面带有{issue.issue_type}特征, 但存在可抓内容 —— 继续提取",
                            )
                    else:
                        result.errors.append(
                            f"页面被{issue.title or issue.issue_type}拦截, 已跳过提取"
                            "(页面上没有可抓内容)"
                        )
                        if on_progress:
                            on_progress("WARNING", "页面被拦截且无可抓内容, 已跳过提取")
                        return self._finalize(result, started, page)

                if challenge.detected and not has_content:
                    result.errors.append(
                        f"页面要求{challenge.kind}, 已跳过提取(验证未完成时页面不是目标内容)"
                    )
                    if on_progress:
                        on_progress("WARNING", "人机验证未完成, 已跳过提取")
                    return self._finalize(result, started, page)

                # ---------- 6. 确定提取规则 ----------
                if rule is None:
                    if goal and use_ai and self.ai.available:
                        logger.info(f"AI 模式: 根据目标生成规则 -> {goal}")
                        rule = await self.ai.analyze_page(report, goal)
                        if rule is None:
                            logger.warning("AI 未能生成规则, 降级到规则引擎")
                    if rule is None:
                        rule = StructureAnalyzer.build_rule_from_structure(
                            report, max_pages=max_pages
                        )
                result.rule = rule
                if rule is None or rule.list_rule is None:
                    result.errors.append("无法确定提取规则(无候选列表/AI不可用/未传规则)")
                    return self._finalize(result, started, page)
                assert rule.list_rule is not None

                # ---------- 7. 首页提取 ----------
                await self.plugins.run_async("before_extract", ctx, only=plugin_ids)
                # 先等选择器真的命中(SPA 在 network_settle 之后才渲染出列表的情况下,
                # 直接提取会得到 0 条, 而用户完全不知道只是"还没渲染好")
                if not await self._wait_for_items(page, rule):
                    logger.warning(
                        f"等待 {8.0}s 后 item_selector 仍未匹配到元素: "
                        f"{truncate(str(rule.list_rule.item_selector), 80)}"
                    )
                    if on_progress:
                        on_progress(
                            "WARNING",
                            "等待 8 秒后仍没有匹配到列表项 —— 可能是页面渲染更慢, "
                            "或规则与当前页面结构不符(可增大「额外等待」或重新分析)",
                        )
                items = await self._extract_current(page, rule)
                result.pages_crawled = 1
                logger.info(f"第 1 页提取 {len(items)} 条")

                # ---------- 8. 分页跟随 ----------
                items = await self._follow_pagination(page, rule, items, max_pages, result, ctx, plugin_ids)

                # ---------- 9. 去重 / 截断 / 增量 ----------
                items = self.extractor.dedupe(items)
                cap = self.settings.crawler.max_items
                if len(items) > cap:
                    logger.warning(f"条数 {len(items)} 超过上限, 截断到 {cap}")
                    items = items[:cap]
                if incremental:
                    items, new_hashes = self._filter_incremental(items)
                else:
                    new_hashes = [stable_hash(i) for i in items]

                # ---------- 10. 插件: 提取后处理(下载/清洗/补字段) ----------
                # 去重之后执行, 让插件拿到的是最终待落盘的数据集
                items = await self.plugins.run_async("after_extract", ctx, items=items, only=plugin_ids)

                # ---------- 8. 可选的 Pydantic 模型校验 ----------
                if item_model is not None:
                    items, errors = self.extractor.map_to_model(items, item_model)
                    result.errors.extend(errors[:10])

                result.items = items
                result.item_count = len(items)
                result.network_record_count = len(self.recorder.records)
                result.websocket_record_count = len(self.recorder.ws_records)

                # ---------- 10.5 没抓到数据时补一条诊断 ----------
                # 若访问受限已经识别出来, 就保留那个结论(它更有信息量); 否则说明
                # "页面正常但没有列表", 给出选择器方向的排查建议。两种情况用户看到的
                # 都是"0 条", 但下一步该做什么完全不同。
                if not items and (result.access_issue is None or not result.access_issue.detected):
                    result.access_issue = build_empty_page_issue(
                        url, page.url or url, http_status=status_code
                    )
                    if on_progress:
                        on_progress("INFO", "未提取到数据: 未发现访问受限迹象, 更可能是选择器或页面结构问题")

                # ---------- 11. 存储输出 ----------
                if format or output:
                    result.saved_to = await self.storage.save(
                        items, format=format, path=output, source_url=url
                    )
                if incremental and new_hashes:
                    self._save_incremental_state(new_hashes)

                # ---------- 12. 插件: 收尾(汇总产物/额外导出) ----------
                items = await self.plugins.run_async("on_finish", ctx, items=items, only=plugin_ids)

                # 把插件的产物与错误汇总进任务结果
                result.downloads = self.plugins.downloads
                result.plugins_used = self.plugins.used
                result.plugin_errors = self.plugins.errors
                if result.downloads:
                    ok = sum(1 for d in result.downloads if d.ok)
                    logger.info(f"插件共下载 {ok}/{len(result.downloads)} 个文件")
            finally:
                await self.browser.close_page(page)
        return self._finalize(result, started)

    # ------------------------------------------------------------------
    # 仅分析(不提取)
    # ------------------------------------------------------------------
    async def analyze_only(
        self,
        url: str,
        *,
        deep_scroll: int = 0,
        scroll_confirm: Optional[Callable[..., Any]] = None,
        scroll_progress: Optional[Callable[[int, Any], None]] = None,
        scroll_rounds: Optional[int] = None,
    ) -> tuple[Optional[PageStructureReport], dict[str, Any]]:
        """打开页面做结构分析 + 网络监听摘要(调试/规则设计用), 不提取数据。

        ``deep_scroll``: 界面上选择"继续向下滚动"时传入的额外滚动轮次(用于无限流页面)。

        同时做访问受限诊断 —— 这正是用户最需要它的场景: 分析一个"看起来正常"的页面,
        然后立刻知道自己是撞上了登录重定向、权限不足还是风控页, 而不是页面真的没有
        列表结构。诊断结论会附在报告的 ``access_issue`` 字段上。
        """
        await self.start()
        async with self._lock:
            self.recorder.clear()
            page = await self.browser.new_page()
            self.recorder.attach(page)
            try:
                resp = await self.browser.goto(page, url)
                if resp is None:
                    return None, self.recorder.stats()
                await asyncio.sleep(self.settings.crawler.network_settle)

                # 懒加载: 先滚再把结构交出去。不滚的话拿到的是一条"看起来完整的空页面"
                # (导航在、主内容区是空容器), 于是分析结论会变成"这个站没有列表"。
                scroll = await self._maybe_scroll(
                    page,
                    deep_scroll=deep_scroll,
                    confirm=scroll_confirm,
                    on_progress=scroll_progress,
                    scroll_rounds=scroll_rounds,
                )
                report_lazy = scroll.to_dict() if scroll else None

                report = await self.structure.analyze(page)
                report.lazy_load = report_lazy
                # 人机验证**先判**: 它的结论是权威的, 要用来纠正访问诊断的推断
                challenge = await detect_challenge(page)
                report.challenge = challenge.to_dict()
                issue = await self._diagnose_access(
                    page,
                    url,
                    getattr(resp, "status", None),
                    challenge_confirmed=challenge.detected,
                )
                if issue.detected:
                    report.access_issue = issue
                # 登录状态: 有些站点登录前后页面结构完全不同(pixiv 首页就是典型 ——
                # 匿名看到的是注册引导页)。这里给出判定, 由界面询问用户是否要登录,
                # 以免把"未登录的引导页"误当成"这个站没有列表"。
                report.login_state = (
                    await detect_login_state(page, session_restored=self.browser.session_restored)
                ).to_dict()
                return report, self.recorder.stats()
            finally:
                await self.browser.close_page(page)

    async def capture_requests(
        self, url: str, wait_seconds: float = 5.0, scroll: bool = True
    ) -> list[NetworkRecord]:
        """打开页面并捕获网络请求(不分析不提取), 供调试接口/CLI 查看。"""
        await self.start()
        async with self._lock:
            self.recorder.clear()
            page = await self.browser.new_page()
            self.recorder.attach(page)
            try:
                if await self.browser.goto(page, url) is None:
                    return []
                if scroll:
                    await self.browser.human_scroll(page, times=3, step=600)
                await asyncio.sleep(wait_seconds)
                return list(self.recorder.records)
            finally:
                await self.browser.close_page(page)

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    async def _maybe_scroll(
        self,
        page,
        *,
        deep_scroll: int = 0,
        confirm: Optional[Callable[..., Any]] = None,
        on_progress: Optional[Callable[[int, Any], None]] = None,
        scroll_rounds: Optional[int] = None,
    ):
        """按配置决定是否做懒加载滚动。返回 :class:`ScrollOutcome` 或 None。

        ``scroll_rounds``: 界面上用户自己设定的轮数, **优先于配置**。
        取 0 表示"这次不滚动"; 取 None 表示用配置默认值。

        ``deep_scroll``: 界面上选择"继续向下滚动"时传入的**额外**轮次。
        ``confirm``: 交互式续滚回调 —— 传了就能"一直滚到用户说不滚为止"(由服务层提供)。
        """
        cfg = self.settings.crawler
        if not cfg.lazy_load_scroll and deep_scroll <= 0 and not scroll_rounds:
            return None
        base = cfg.lazy_load_max_rounds if scroll_rounds is None else max(int(scroll_rounds), 0)
        rounds = base + max(deep_scroll, 0)
        if rounds <= 0:
            return None
        return await scroll_to_load(
            page,
            max_rounds=rounds,
            step_ratio=cfg.lazy_load_step_ratio,
            confirm=confirm,
            on_progress=on_progress,
        )

    async def _diagnose_access(
        self,
        page,
        url: str,
        http_status: Optional[int],
        *,
        challenge_confirmed: Optional[bool] = None,
    ):
        """做访问受限诊断, 并容忍 SPA 的渲染延迟。

        **为什么需要重试**: 像洛谷这样的单页应用, 401 后错误文案是前端 JS 渲染出来的。
        网络静默期结束时文案有时还没上屏, 直接采集会得到"可见内容极少"的空壳结论,
        于是把"没有权限"误报成"内容未渲染"。这里做一个短轮询: 只要还没采到实质内容,
        就隔一会儿重采一次(最多 ``DIAGNOSE_RETRIES`` 次), 拿到内容即返回。

        代价是最坏多等 6 秒; 收益是诊断结果稳定 —— 这类"偶发空采集"如果不处理,
        会让整个诊断能力看起来时灵时不灵。

        ``challenge_confirmed`` 会透传给诊断逻辑, 用于纠正它对"人机验证"的推断。
        """
        issue = await detect_access_issue(
            page, url, http_status=http_status, challenge_confirmed=challenge_confirmed
        )
        for _ in range(self.DIAGNOSE_RETRIES):
            # 已经采到实质内容, 或者已经识别出确定的问题类型, 就不必再等
            if len(issue.visible_text.strip()) > 40 or issue.detected:
                break
            await asyncio.sleep(self.DIAGNOSE_INTERVAL)
            issue = await detect_access_issue(
                page, url, http_status=http_status, challenge_confirmed=challenge_confirmed
            )
        return issue

    async def _extract_current(self, page, rule: ExtractionRule) -> list[dict[str, Any]]:
        """按规则模式提取当前页面。"""
        if rule.mode == "json":
            items = self.extractor.extract_json_with_rule(self.recorder.records, rule)
            if not items and rule.source == "ai":
                # AI 选了 json 模式但没匹配到: 尝试对最大 JSON 响应重新生成映射
                items = await self._retry_json_with_ai(rule)
            return items
        return await self.extractor.extract_with_rule(page, rule)

    async def _wait_for_items(
        self,
        page,
        rule: ExtractionRule,
        *,
        timeout: float = 8.0,
        interval: float = 0.4,
    ) -> bool:
        """等 item_selector 真的匹配到元素, 返回是否等到。

        **为什么需要它**: 文档里给的等待(`network_settle`, 默认 2.5s)对静态站足够,
        但对 SPA 是"猜时长"。实测 pixiv 搜索页: 同一个选择器, 分析时匹配 60 项,
        而某次抓取在 2.5s 时页面还没渲染出结果区, 提取到 0 条 ——
        用户看到"抓取到 0 条"却不知道只是慢了半秒, 图片插件反而把图都下下来了
        (它有自己的取图路径), 于是现象自相矛盾: 图有了、数据没有。

        这里是**等事实**而不是等时长: 轮询 `item_selector` 是否已匹配到元素, 一旦匹配
        立刻返回(所以正常页面不会变慢), 直到超时才放弃并把结论交给上层。
        """
        if not rule.list_rule or not rule.list_rule.item_selector:
            return False
        selector = rule.list_rule.item_selector
        deadline = time.monotonic() + max(0.0, timeout)
        first = True
        while True:
            try:
                n = await page.evaluate(
                    "(sel) => { try { return document.querySelectorAll(sel).length } "
                    "catch (e) { return -1 } }",
                    selector,
                )
            except Exception:  # noqa: BLE001 - 页面正在导航时会短暂失败
                n = -1
            if isinstance(n, int) and n > 0:
                if not first:
                    logger.info(f"等待生效: 第 {n} 个元素出现(选择器 {truncate(selector, 60)})")
                return True
            if time.monotonic() >= deadline:
                return False
            first = False
            await asyncio.sleep(interval)

    async def _retry_json_with_ai(self, rule: ExtractionRule) -> list[dict[str, Any]]:
        """json 模式兜底: 把最大的 JSON 响应交给 AI 重新分析字段。"""
        json_records = self.recorder.json_records()
        if not json_records or not self.ai.available:
            return []
        biggest = max(json_records, key=lambda r: len(r.body_text or ""))
        logger.info(f"json 规则未命中, 尝试对 {biggest.url[:80]} 重新 AI 分析")
        new_rule = await self.ai.extract_from_json(biggest.body_json, rule.notes or "提取列表数据")
        if new_rule and new_rule.list_rule:
            # 保留原规则的 item_path 失败时使用 AI 的完整规则
            items = self.extractor.extract_json_with_rule([biggest], new_rule)
            if items:
                rule.mode = new_rule.mode
                rule.list_rule = new_rule.list_rule
            return items
        return []

    async def _follow_pagination(
        self,
        page,
        rule: ExtractionRule,
        items: list[dict[str, Any]],
        max_pages: int,
        result: TaskResult,
        ctx: Optional[PluginContext] = None,
        plugin_ids: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """跟随"下一页"按钮翻页并合并各页数据(最多 max_pages 页)。"""
        pagination = rule.pagination
        if not pagination or not pagination.next_selector or max_pages <= 1:
            return items
        for page_no in range(2, max_pages + 1):
            clicked = await self.browser.click(page, pagination.next_selector, timeout=8.0)
            if not clicked:
                # 点击失败可能是最后一页(按钮禁用/消失), 正常结束
                logger.info(f"第 {page_no - 1} 页为最后一页, 停止翻页")
                break
            await self.browser.wait_for_load_state(page, "domcontentloaded", timeout=20.0)
            await asyncio.sleep(min(self.settings.crawler.network_settle, 6.0))

            # 翻页后同样等"新内容真的到位"。SPA 翻页只是改 URL + 重渲染,
            # domcontentloaded 早就过了, 固定睡 2.5s 经常睡在骨架态上 ——
            # 那样提取到的还是旧内容, 去重后表现为"本页无新增"。
            if not await self._wait_for_items(page, rule):
                logger.warning(f"翻到第 {page_no} 页后仍未匹配到列表项, 仍尝试提取")

            # 插件: 每翻完一页(可在此对新页面做懒加载触发/注入)
            if ctx is not None:
                ctx.page = page
                ctx.url = page.url or ctx.url
                ctx.page_no = page_no
                await self.plugins.run_async("on_page", ctx, only=plugin_ids)

            page_items = await self._extract_current(page, rule)
            before = len(items)
            items.extend(page_items)
            items = self.extractor.dedupe(items)
            result.pages_crawled = page_no
            logger.info(f"第 {page_no} 页提取 {len(page_items)} 条, 合计去重后 {len(items)} 条")
            if len(items) == before:
                logger.info("翻页无新增数据, 停止")
                break
        return items

    def _filter_incremental(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """增量过滤: 丢弃状态文件中已见过的内容, 返回 (新增条目, 本轮全部哈希)。"""
        state = load_state(self.settings.crawler.state_path)
        seen: set[str] = set(state.get("seen_hashes", []))
        fresh: list[dict[str, Any]] = []
        all_hashes: list[str] = []
        for item in items:
            h = stable_hash(item)
            all_hashes.append(h)
            if h not in seen:
                fresh.append(item)
        skipped = len(items) - len(fresh)
        if skipped:
            logger.info(f"增量模式: 跳过 {skipped} 条已见数据, 新增 {len(fresh)} 条")
        return fresh, all_hashes

    def _save_incremental_state(self, new_hashes: list[str]) -> None:
        """把本轮内容哈希写入增量状态文件。"""
        state = load_state(self.settings.crawler.state_path)
        state = update_seen_hashes(
            state, new_hashes, capacity=self.settings.crawler.seen_hash_capacity
        )
        save_state(self.settings.crawler.state_path, state)

    def _finalize(
        self, result: TaskResult, started: float, page=None
    ) -> TaskResult:
        """收尾: 记录耗时/成功标志/日志。"""
        from datetime import datetime

        result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        result.finished_at = result.finished_at or datetime.now().isoformat(timespec="seconds")
        result.success = bool(result.items) and not result.errors
        logger.info(
            f"任务 {result.task_id} 结束: success={result.success} "
            f"items={result.item_count} pages={result.pages_crawled} "
            f"耗时 {result.duration_ms:.0f}ms"
        )
        return result


# ---------------------------------------------------------------------------
# 便捷同步入口(脚本场景)
# ---------------------------------------------------------------------------
def run_crawl(url: str, **kwargs: Any) -> TaskResult:
    """同步封装: 在新事件循环中执行一次 crawl(适合简单脚本)。"""
    return asyncio.run(SmartCrawler().crawl(url, **kwargs))
