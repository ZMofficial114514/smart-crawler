"""
登录状态识别 —— 判断"当前这次访问是登录态还是匿名".

**为什么必须区分**: 有些站点登录前后**页面结构完全不同**(例如 pixiv 首页: 匿名看到
的是注册/登录引导页, 登录后才是作品瀑布流)。如果不加区分, 结构分析会把引导页当成
"这个网站没有列表", 用户则拿着完全错误的结论去调选择器。

判定是**双向**的, 两边都要有信号才下结论:

- **已登录信号**: 头像/用户菜单/退出登录/我的收藏、"我的""个人中心"等入口、
  在 URL 里出现 ``/users/<数字>`` 这类个人页链接、昵称元素;
- **未登录信号**: 登录/注册按钮与链接、密码输入框(表单可为空)、
  ``accounts.*/login`` 这类鉴权域名的链接、指向登录页的 OAuth 表单。

两类都不明显时返回 ``unknown`` —— 宁可不说, 也不要误导用户去登录一个根本不需要登录
的站点。置信度 = 命中信号加权求和后的归一化结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from playwright.async_api import Page

# ---------------------------------------------------------------------------
# 判定脚本: 一次 evaluate 取回所有候选信号
# ---------------------------------------------------------------------------
DETECT_JS = r"""
() => {
    const visible = (el) => {
        if (!el) return false;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const textOf = (el) => ((el.innerText || el.textContent || '')).trim().replace(/\s+/g, ' ');

    // "存在于文档里"—— 比 visible() 宽松: **只要不是被刻意藏起来的**(visibility:hidden /
    // opacity:0)就算存在, 不再要求有尺寸、也不要求在视口内。
    //
    // 为什么必须放宽到这一步(实测数据支撑):
    //   pixiv 登录后, /logout /stacc /bookmark.php /history.php 等 8 个登录专属入口
    //   **全部**在 DOM 里, 但**全部**处在 <div class="hidden">(display:none) 的响应式
    //   容器中; 而匿名访问时这 8 个路径**一个都不存在**(只有 /login 与 /signup)。
    //   也就是说: 这些路径"存在"本身就是服务端按登录用户渲染内容的证据, 与它当前
    //   有没有被排版出来无关。于是之前的 visible() 判据把已登录页面判成了
    //   "登录状态不明确 · 置信度 0%"。
    //
    // 刻意**不**排除 display:none: 那是站点做响应式最常见的隐藏方式(桌面版/移动版各留
    // 一份 DOM), 被它隐藏的元素在另一种视口下就是可见的, 因此"存在"应当算数。
    // 而 visibility:hidden / opacity:0 是"藏内容"的语义(如折叠面板), 仍按不存在处理。
    const present = (el) => {
        if (!el) return false;
        for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
            const s = window.getComputedStyle(p);
            if (s.visibility === 'hidden' || s.opacity === '0') return false;
        }
        return true;
    };

    const collect = (selector, limit) => {
        const out = [];
        for (const el of document.querySelectorAll(selector)) {
            if (!visible(el)) continue;
            const t = textOf(el);
            if (!t || t.length > 40) continue;
            out.push({ text: t, href: el.getAttribute('href') || '', tag: el.tagName.toLowerCase() });
            if (out.length >= limit) break;
        }
        return out;
    };

    // 头像/用户菜单: **只在页头/导航里找**。内容区里的用户链接属于"作品作者",
    // 不代表访客自己登录了 —— pixiv 匿名首页有 18 个 /users/ 链接(都是画师),
    // 早先把它们当成"已登录"信号, 于是一个完全匿名的页面被判成已登录。
    const chromeScope = document.querySelector('header, nav, [role="banner"], [class*="header" i], [class*="Header" i]');
    const scopeRoot = chromeScope || document.body;

    // 头像候选: **必须同时是 <img> 且 src 像头像**。
    //
    // 两个真实误判教出来的教训:
    //   1) 只按类名找会漏掉 pixiv 的真实头像(一个无 class 的 <img>, 父级是哈希类名);
    //   2) 但把选择器放宽到"类名里带 user/icon 的任意元素"又会**误命中装饰性元素** ——
    //      匿名首页有一个 <div class="BackgroundSlideshow_userIcon____rg1">(48x48 的背景
    //      轮播图标), 它不是头像, 却让匿名页面白拿了 0.30 的已登录分。
    // 所以判据是"img + src 特征 + 尺寸像头像", 三者缺一不可。
    const avatarSelectors = [
        '[class*="avatar" i] img', 'img[class*="avatar" i]',
        'img[class*="user-icon" i]', 'img[class*="userIcon" i]',
        'img[class*="userIcon" i]',
        '[class*="userMenu" i] img', '[class*="user-menu" i] img',
        '[class*="AccountMenu" i] img', '[class*="account-menu" i] img',
        'header img[src*="profile" i]', 'header img[src*="avatar" i]',
        'nav img[src*="profile" i]',
    ];
    // 头像判据: 有真实 src + 尺寸像头像(方形、24~96px)。
    // 尺寸这一条是主力 —— 它能挡掉 logo(宽扁)与装饰图; src 关键词只作为额外佐证,
    // 不能作为必要条件(pixiv 的头像 URL 里并没有 avatar/user 字样)。
    const looksLikeAvatarSrc = (img) => {
        const src = img.getAttribute('src') || '';
        if (!src || src.startsWith('data:')) return false;
        const r = img.getBoundingClientRect();
        const squareish = Math.abs(r.width - r.height) <= 8;
        const sizeOk = r.width >= 24 && r.width <= 96 && r.height >= 24 && r.height <= 96;
        return squareish && sizeOk;
    };
    let avatar = 0;
    for (const sel of avatarSelectors) {
        for (const el of scopeRoot.querySelectorAll(sel)) {
            if (el.tagName.toLowerCase() !== 'img') continue;   // 排除装饰性 div/span
            if (!visible(el)) continue;
            if (!looksLikeAvatarSrc(el)) continue;
            avatar++;
        }
    }
    // 兜底: 页头/导航里"方形、24~96px、有真实 src"的图片。
    // 只在确实存在语义化页头时启用 —— scopeRoot 退化成 body 时(有些站点没有
    // <header>/<nav>, 实测 pixiv 匿名页就是)全页扫描会把内容区配图算成头像。
    if (avatar === 0 && chromeScope) {
        for (const img of scopeRoot.querySelectorAll('img')) {
            if (img.tagName.toLowerCase() !== 'img') continue;
            if (!visible(img)) continue;
            if (!looksLikeAvatarSrc(img)) continue;
            avatar++;
            if (avatar >= 3) break;
        }
    }

    // "只属于我"的功能入口: 收藏/关注/设置/消息/我的作品。
    // 这些是登录后才存在的导航项, 比"页面上有用户链接"可靠得多。
    //
    // **多语言是必需的**, 不是可选项: 站点按 locale 渲染不同文案, 漏掉某一种语言就会
    // 在那种语境下完全失效。这里一开始只有中英文, 实测在 pixiv 上就漏了日文菜单。
    //
    // **不能只看 href**: 早先图省事加了 ``my\/`` 想匹配 /my/xxx, 结果任何含 "my/" 的
    // URL 都命中; 中文"的作品"里的"作"甚至被 ``作品`` 匹配到画家名
    // (实测匿名页命中『YUNA的作品』-> https://www.pixiv.net/users/7036095)。
    // 所以: 文字用多语言关键词, href 只匹配**明确的账户路径**白名单。
    const ownNavWords = /(我的|個人|个人|收藏|关注|追蹤|消息|通知|设置|設定|书签|浏览记录|动态|约稿|参加群组|作品管理|创作|マイページ|ブックマーク|フォロー|フォロワー|設定|通知|メッセージ|履歴|マンガ|小説|마이페이지|북마크|팔로우|설정|알림|bookmark|favorite|following|setting|notification|mypage)/i;
    const ownNavHrefs = ['/bookmark', '/mypage', '/my/', '/settings', '/following',
                         '/history', '/dashboard', '/stacc', '/marker_all',
                         '/manage/', '/group/group_list', '/novel/marker'];
    const ownNavLinks = [...scopeRoot.querySelectorAll('a[href]')]
        .filter(a => {
            if (!visible(a)) return false;
            const text = (a.innerText || '').trim();
            const href = a.getAttribute('href') || '';
            // 文字必须**自己**像"我的入口"(短标签), 避免匹配到画师名之类的长文本
            if (text && text.length <= 12 && ownNavWords.test(text)) return true;
            return ownNavHrefs.some(h => href.includes(h));
        })
        .map(a => ({ text: textOf(a).slice(0, 30), href: a.getAttribute('href') || '' }));

    // 登录专属的**账户路径**: 这些 URL 只有登录用户才会被渲染出来, 与文案语言无关,
    // 因此比文字匹配更稳。为了对抗"整块侧边栏被响应式容器隐藏", 这里用 present()
    // 而不是 visible()。
    const ACCOUNT_PATHS = ['/logout', '/stacc', '/dashboard', '/history.php',
                           '/bookmark.php', '/novel/marker_all', '/manage/requests',
                           '/group/group_list', '/settings', '/mypage'];
    const accountPathLinks = [...document.querySelectorAll('a[href]')]
        .filter(a => present(a) && ACCOUNT_PATHS.some(p => (a.getAttribute('href') || '').includes(p)))
        .map(a => ({ text: textOf(a).slice(0, 30), href: a.getAttribute('href') || '' }));

    // 登录/注册入口
    const loginHrefs = [...document.querySelectorAll('a[href]')]
        .map(a => a.getAttribute('href') || '')
        .filter(h => /(^|\/)(login|signin|sign-in|logon|register|signup|sign-up|auth)(\/|$|\?)/i.test(h) ||
                     /accounts\.[^/]+\//i.test(h));
    const authForms = [...document.querySelectorAll('form[action]')]
        .map(f => f.getAttribute('action') || '')
        .filter(a => /login|signin|auth|accounts\./i.test(a));

    // 退出登录入口。两个改进都是必需的:
    //   1) **同时看 href** —— pixiv 的退出是 <a href="/logout.php?return_to=%2F">, 只按
    //      文案匹配时, 文案一旦是脚本渲染/非中英文就会漏掉; href 是结构性的, 更可靠;
    //   2) 用 present() 而不是 visible() —— 同上, 整块侧边栏可能被判为不可见。
    //   3) 日文 ログアウト / 韩文 로그아웃 一并覆盖。
    const logoutCount = [...document.querySelectorAll('a, button')]
        .filter(el => {
            if (!present(el)) return false;
            const t = textOf(el);
            const h = el.getAttribute('href') || '';
            return /退出|登出|注销|ログアウト|로그아웃|log\s?out|sign\s?out/i.test(t + ' ' + h)
                || /\/logout/i.test(h);
        })
        .length;

    const bodyText = (document.body ? (document.body.innerText || '') : '').slice(0, 20000);

    return {
        url: window.location.href,
        title: (document.title || '').trim(),
        // 未登录方向的信号
        login_links: collect('a[href]', 60).filter(a =>
            /^(登录|登陆|注册|立即登录|免费注册|sign\s?in|log\s?in|sign\s?up|register)$/i.test(a.text) ||
            /(^|\/)(login|signin|register|signup)(\/|$|\?)/i.test(a.href) ||
            /accounts\./i.test(a.href)
        ).slice(0, 8),
        login_link_count: loginHrefs.length,
        auth_form_count: authForms.length,
        password_inputs: [...document.querySelectorAll('input[type="password"]')].filter(visible).length,
        // 已登录方向的信号
        avatar_count: avatar,
        own_nav_count: ownNavLinks.length,
        own_nav: ownNavLinks.slice(0, 6),
        logout_count: logoutCount,
        // 登录专属账户路径的命中数(与文案语言无关的结构性证据)
        account_path_count: accountPathLinks.length,
        account_paths: accountPathLinks.slice(0, 8),
        // 文案
        // 未登录方向的信号。中英日韩都覆盖: 站点的语言取决于浏览器 locale, 只认中文
        // 会在其它语境下漏判(pixiv 在无 locale 的默认上下文里就渲染英文引导页)。
        has_login_prompt: /请先?登录|需要登录|登录后(才能)?|用\s*\S*\s*账号登录|注册账号|立即注册|免费注册|未登录|ログイン(する|してください)|新規登録|(log\s?in|sign\s?in)\s+(with|to|required)|please\s+(log\s?in|sign\s?in)|create\s+an?\s+account|sign\s?up\s+(for\s+)?free/i.test(bodyText),
        has_account_prompt: /我的(主页|收藏|关注|账号|消息)|个人中心|账号设置|退出登录|我的作品|マイページ|ブックマーク|フォロー中|ログアウト|\bmy\s+(bookmarks|favorites|follow|account|profile|works|page)\b|log\s?out|sign\s?out/i.test(bodyText),
        body_text: bodyText,
        // 正文长度: 供调用方判断"页面是否已经渲染出内容"(SPA 的引导文案是异步来的,
        // 采早了会得到"信号不足"的结论)。只暴露长度, 不把整段正文传回去。
        body_length: bodyText.replace(/\s+/g, '').length,
    };
}

"""

#: 未登录信号 -> 权重
_ANON_SIGNALS: dict[str, float] = {
    "login_prompt": 0.45,      # 文案里出现"用 xx 账号登录 / 注册账号"
    "login_links": 0.25,       # 页面上有登录/注册链接
    "password_input": 0.30,    # 密码输入框
    "auth_form": 0.25,         # 表单提交到 login/accounts
}

#: 已登录信号 -> 权重
#:
#: 注意这里**没有**"页面存在个人页链接"这一项: 内容型站点(画廊/社区/商城)的列表里
#: 到处是作者/卖家的个人页链接, 与访客是否登录毫无关系。pixiv 匿名首页有 18 个
#: ``/users/`` 链接(全是画师), 早先把它当信号会让一个完全匿名的页面被判成"已登录"。
#: 取而代之的是"只属于我"的入口: 收藏/关注/设置, 以及页头里的头像与退出登录。
_AUTH_SIGNALS: dict[str, float] = {
    "logout": 0.55,           # 有"退出登录" —— 最强的已登录证据
    "avatar": 0.30,           # 页头有头像/用户菜单
    "own_nav": 0.30,          # 页头有"我的收藏/关注/设置"这类仅登录后存在的入口
    "account_prompt": 0.25,   # 文案里有"我的收藏/个人中心"
    # 登录专属**账户路径**(/logout /stacc /dashboard /bookmark.php …)。
    # 与文案语言无关, 且不要求元素可见 —— 很多站点把侧边栏塞进响应式容器里整体隐藏,
    # 但服务端**把登录用户的菜单吐出来了**这件事本身就是登录证据。实测 pixiv 正是
    # 这种情况: 8 个登录专属入口都在 DOM 里, 却全部判为不可见, 于是已登录页面
    # 得到"登录状态不明确 · 置信度 0%"。
    "account_path": 0.35,
}


@dataclass
class LoginState:
    """登录状态判定结果。"""

    #: logged_in / anonymous / unknown
    state: str = "unknown"
    confidence: float = 0.0
    #: 判定依据(人类可读)
    reasons: list[str] = field(default_factory=list)
    #: 各信号的命中情况(便于用户理解归因)
    signals: dict[str, Any] = field(default_factory=dict)
    anon_score: float = 0.0
    auth_score: float = 0.0
    #: 页面上看到的登录/注册入口文案(用于界面提示)
    login_entries: list[dict[str, str]] = field(default_factory=list)
    page_title: str = ""
    url: str = ""
    #: 正文非空白字符数, 用于判断页面渲染进度
    body_length: int = 0
    #: 是否使用了已保存的会话(由调用方注入)
    session_restored: bool = False
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def logged_in(self) -> bool:
        return self.state == "logged_in"

    def summary(self) -> str:
        label = {"logged_in": "已登录", "anonymous": "未登录(匿名)", "unknown": "登录状态不明确"}[self.state]
        return f"{label} · 置信度 {self.confidence:.0%}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "logged_in": self.logged_in,
            "confidence": self.confidence,
            "summary": self.summary(),
            "reasons": self.reasons,
            "signals": self.signals,
            "anon_score": round(self.anon_score, 2),
            "auth_score": round(self.auth_score, 2),
            "login_entries": self.login_entries,
            "page_title": self.page_title,
            "url": self.url,
            "body_length": self.body_length,
            "session_restored": self.session_restored,
            "checked_at": self.checked_at,
        }


def evaluate_login_state(
    raw: dict[str, Any],
    *,
    session_restored: bool = False,
    threshold: float = 0.4,
    fallback_url: str = "",
) -> LoginState:
    """由采集到的原始信号计算出判定结果。

    刻意做成**纯函数**: 异步路径(:func:`detect_login_state`, 用于 Web 服务与爬虫)与
    同步路径(:func:`detect_login_state_sync`, 用于登录辅助进程)共用同一套判定逻辑,
    避免"两个地方各写一遍导致口径不一致"。
    """
    state = LoginState(session_restored=session_restored)
    state.url = str(raw.get("url") or fallback_url)
    state.page_title = str(raw.get("title") or "")

    signals = {
        "login_prompt": bool(raw.get("has_login_prompt")),
        "login_links": len(raw.get("login_links") or []) > 0,
        "login_link_count": int(raw.get("login_link_count") or 0),
        "password_input": int(raw.get("password_inputs") or 0) > 0,
        "auth_form": int(raw.get("auth_form_count") or 0) > 0,
        "logout": int(raw.get("logout_count") or 0) > 0,
        "avatar": int(raw.get("avatar_count") or 0) > 0,
        "avatar_count": int(raw.get("avatar_count") or 0),
        "own_nav": int(raw.get("own_nav_count") or 0) > 0,
        "own_nav_count": int(raw.get("own_nav_count") or 0),
        "account_path": int(raw.get("account_path_count") or 0) > 0,
        "account_path_count": int(raw.get("account_path_count") or 0),
        "account_prompt": bool(raw.get("has_account_prompt")),
    }
    state.signals = signals
    # 正文长度(非空白字符数): 调用方用它判断"页面渲染到什么程度了"
    state.body_length = int(raw.get("body_length") or 0)
    state.login_entries = [
        {"text": str(e.get("text") or ""), "href": str(e.get("href") or "")}
        for e in (raw.get("login_links") or [])[:6]
    ]

    # ---- 累计得分 ----
    anon = 0.0
    auth = 0.0
    for key, weight in _ANON_SIGNALS.items():
        if signals.get(key):
            anon += weight
    for key, weight in _AUTH_SIGNALS.items():
        if signals.get(key):
            auth += weight

    if signals.get("login_prompt"):
        state.reasons.append("页面文案出现『登录/注册账号』类引导")
    if signals.get("login_links"):
        # 用**可见**入口数量而不是 href 计数: 后者会把隐藏的备用入口也算进来,
        # 显示成"有 0 个登录入口链接"这种自相矛盾的话。
        shown = len(state.login_entries) or signals["login_link_count"]
        state.reasons.append(f"页面有 {shown} 个登录/注册入口链接")
    if signals.get("password_input"):
        state.reasons.append("页面存在密码输入框(且未见任何已登录特征)")
    if signals.get("auth_form"):
        state.reasons.append("存在提交到登录/鉴权地址的表单")
    if signals.get("logout"):
        state.reasons.append("页面有『退出登录』入口")
    if signals.get("avatar"):
        state.reasons.append(f"页头检测到 {signals['avatar_count']} 个头像/用户菜单元素")
    if signals.get("own_nav"):
        state.reasons.append(
            f"页头有 {signals['own_nav_count']} 个『仅登录后存在』的入口"
            "(收藏/关注/设置等)"
        )
    if signals.get("account_path"):
        state.reasons.append(
            f"页面渲染出 {signals['account_path_count']} 个登录专属入口"
            "(退出登录/我的收藏/浏览记录等), 说明服务端已按登录用户返回内容"
        )
    if signals.get("account_prompt"):
        state.reasons.append("页面文案出现『我的收藏/个人中心』类入口")

    # 一个站点同时出现两类信号很常见(例如已登录页脚仍留"登录"链接), 因此看差值。
    # 但"没有已登录特征"本身也是未登录的证据: 一个要求登录的页面(或站点首页)
    # 若完全没有头像/个人页/退出入口, 那它极可能就是匿名视图。没有这条, GitHub 首页
    # 这种"只有一个 Sign in 链接"的页面会落到 unknown, 用户得不到任何提示。
    if auth == 0.0 and anon > 0.0:
        anon += 0.15
        state.reasons.append("页面完全没有任何已登录特征(无头像/个人页/退出入口)")

    # ---- 结构性兜底: 登录表单密集, 且完全没有已登录特征 ----
    # 有些站点(尤其 SPA)的登录引导**文案**是异步渲染的, 采早了就抓不到文字; 但它的
    # 登录表单是服务端直出的。pixiv 匿名首页实测就是这样: 文案可能没上屏, 但页面上
    # 有 4 个提交到 accounts.pixiv.net 的表单。只靠文字判断会得到"信号不足",
    # 而"多个鉴权表单 + 零个已登录特征"本身已经是很强的匿名证据。
    auth_form_count = int(raw.get("auth_form_count") or 0)
    if auth_form_count >= 2 and auth == 0.0:
        anon += 0.35
        state.reasons.append(
            f"页面有 {auth_form_count} 个提交到登录/鉴权地址的表单, 且无任何已登录特征"
        )

    net = auth - anon
    state.anon_score = anon
    state.auth_score = auth

    if auth >= threshold and net > 0.05:
        state.state = "logged_in"
        state.confidence = round(min(auth, 1.0), 2)
    elif anon >= threshold and net < -0.05:
        state.state = "anonymous"
        state.confidence = round(min(anon, 1.0), 2)
    else:
        state.state = "unknown"
        state.confidence = round(min(max(anon, auth), 1.0), 2)
        if anon or auth:
            state.reasons.append(
                f"两类信号接近(未登录 {anon:.2f} / 已登录 {auth:.2f}), 无法确定, 按不明确处理"
            )

    return state


async def detect_login_state(
    page: Page,
    *,
    session_restored: bool = False,
    threshold: float = 0.4,
) -> LoginState:
    """判断当前页面是登录态还是匿名态(异步, 用于 Web 服务与爬虫)。

    **不会抛异常**: 识别失败时返回 ``unknown``, 绝不干扰正常分析流程。
    """
    try:
        raw: dict[str, Any] = await page.evaluate(DETECT_JS)
    except Exception as exc:  # noqa: BLE001
        state = LoginState(session_restored=session_restored)
        state.reasons.append(f"登录状态识别脚本执行失败({type(exc).__name__}), 按不明确处理")
        return state
    return evaluate_login_state(
        raw, session_restored=session_restored, threshold=threshold, fallback_url=page.url
    )


def detect_login_state_sync(
    page: Any,
    *,
    session_restored: bool = False,
    threshold: float = 0.4,
) -> LoginState:
    """同步版本(用于同步 API 的登录辅助进程)。"""
    try:
        raw: dict[str, Any] = page.evaluate(DETECT_JS)
    except Exception as exc:  # noqa: BLE001
        state = LoginState(session_restored=session_restored)
        state.reasons.append(f"登录状态识别脚本执行失败({type(exc).__name__}), 按不明确处理")
        return state
    return evaluate_login_state(
        raw, session_restored=session_restored, threshold=threshold, fallback_url=str(getattr(page, "url", ""))
    )


def login_state_from_dict(data: Optional[dict[str, Any]]) -> Optional[LoginState]:
    """从字典还原(用于从任务结果里读回判定)。"""
    if not data:
        return None
    state = LoginState(
        state=str(data.get("state") or "unknown"),
        confidence=float(data.get("confidence") or 0.0),
        reasons=list(data.get("reasons") or []),
        signals=dict(data.get("signals") or {}),
        anon_score=float(data.get("anon_score") or 0.0),
        auth_score=float(data.get("auth_score") or 0.0),
        login_entries=list(data.get("login_entries") or []),
        page_title=str(data.get("page_title") or ""),
        url=str(data.get("url") or ""),
        body_length=int(data.get("body_length") or 0),
        session_restored=bool(data.get("session_restored")),
    )
    return state


__all__ = [
    "LoginState",
    "detect_login_state",
    "detect_login_state_sync",
    "evaluate_login_state",
    "login_state_from_dict",
    "DETECT_JS",
]
