"""
页面诊断内容采集 —— 把"这个页面到底显示了什么"结构化地取出来。

存在的意义: 当抓取拿不到数据时, 用户最需要的是**页面实际显示的内容**。日志里只有
"0 条", 而真相往往写在页面上某个 ``<h1>需要登录</h1>`` 或 ``<div class="error">``
里。这个模块用一次 ``page.evaluate`` 把下列信息全部取回:

- 可见正文(带长度上限, 并做折叠空行);
- 标题(``document.title`` + ``h1``/``h2``);
- **主内容区文本**: 剥掉 ``nav``/``footer`` 后的正文, 避免被站点导航刷屏;
- 错误码 / 追踪 ID(从文本与 ``window`` 上的变量里提取);
- 关键元素: 标题、表单、按钮、验证码、错误块、登录/注册链接;
- 结构化元数据: ``meta`` 标签、JSON-LD 里疑似错误的字段。

对 SPA 尤其重要: 例如洛谷在 401 时返回 HTTP 401 + 一个 JS 渲染的错误页, 标题是
``Error - 洛谷``, 正文是 ``出错啦 / 没有权限请求此资源。``, 既没有密码框也没有跳转,
只看 URL 与状态码无法判断究竟是"要登录"还是"没权限"。
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

#: 采集脚本: 单次 evaluate 完成, 避免多次往返
COLLECT_JS = r"""
() => {
    const MAX_TEXT = 8000;
    const MAX_MAIN = 4000;

    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const norm = (s) => (s || '').replace(/[ \t\u00a0]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
    const textOf = (el) => norm(el.innerText || el.textContent || '');

    const buildSelector = (el) => {
        if (!el || el.nodeType !== 1) return '';
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (typeof el.className === 'string' && el.className.trim())
            ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
        let sel = el.tagName.toLowerCase() + cls.map(c => '.' + CSS.escape(c)).join('');
        const parent = el.parentElement;
        if (parent && parent !== document.body) {
            const sibs = [...parent.children].filter(c => c.tagName === el.tagName);
            if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(el) + 1})`;
        }
        return sel;
    };

    // ---- 整页可见正文 ----
    const bodyText = norm(document.body ? (document.body.innerText || '') : '').slice(0, MAX_TEXT);

    // ---- 主内容区: 剥掉常见的外壳 ----
    let mainEl = document.querySelector('main, article, [role="main"], .content, #content, .container');
    if (!mainEl) mainEl = document.body;
    let mainText = '';
    if (mainEl) {
        const clone = mainEl.cloneNode(true);
        clone.querySelectorAll('nav, header, footer, aside, script, style, .nav, .footer, .sidebar')
             .forEach(n => n.remove());
        mainText = norm(clone.innerText || clone.textContent || '').slice(0, MAX_MAIN);
    }

    // ---- 标题 ----
    const headings = [];
    document.querySelectorAll('h1, h2, h3').forEach(el => {
        if (!visible(el)) return;
        const t = textOf(el).slice(0, 160);
        if (t) headings.push({ tag: el.tagName.toLowerCase(), selector: buildSelector(el), text: t });
    });

    // ---- 错误块: 类名/ID 里带 error/alert/notice/warn/tip 的可见容器 ----
    const errorBlocks = [];
    document.querySelectorAll(
        '[class*="error" i], [id*="error" i], [class*="alert" i], [class*="notice" i], ' +
        '[class*="warn" i], [class*="tip" i], [class*="message" i], [role="alert"]'
    ).forEach(el => {
        if (!visible(el) || el.children.length > 8) return;
        const t = textOf(el).slice(0, 300);
        if (t && t.length >= 2) errorBlocks.push({ selector: buildSelector(el), text: t });
    });

    // ---- 表单 / 输入 ----
    const forms = [];
    document.querySelectorAll('form').forEach(el => {
        if (!visible(el)) return;
        forms.push({
            selector: buildSelector(el),
            action: el.getAttribute('action') || '',
            method: (el.getAttribute('method') || 'get').toLowerCase(),
            has_password: Boolean(el.querySelector('input[type="password"]')),
            inputs: [...el.querySelectorAll('input')].slice(0, 6).map(i => i.getAttribute('name') || i.type || ''),
        });
    });
    const passwordInputs = [...document.querySelectorAll('input[type="password"]')].filter(visible).length;

    // ---- 按钮 / 链接(带 href 的才算导航入口) ----
    const buttons = [];
    document.querySelectorAll('button, input[type="submit"], [role="button"], a.button, a.btn').forEach(el => {
        if (!visible(el)) return;
        const t = (el.innerText || el.value || '').trim().slice(0, 40);
        if (t) buttons.push(t);
    });
    const authLinks = [];
    document.querySelectorAll('a[href]').forEach(el => {
        const href = el.getAttribute('href') || '';
        if (/login|signin|sign-in|auth|register|signup|passport/i.test(href)) {
            const t = (el.innerText || '').trim().slice(0, 30) || href;
            authLinks.push({ text: t, href, selector: buildSelector(el) });
        }
    });

    // ---- 验证码 ----
    // **必须可见且尺寸够大**, 否则会把"只是加载了脚本"的锚点当成真实挑战。
    // reCAPTCHA 在不需要挑战时也会插入一个 hidden 的 anchor iframe(pixiv 每次访问都有),
    // 早先只判 visible/尺寸就把它算成"1 个验证码组件", 直接造成误报。
    const bigEnough = (el) => {
        const r = el.getBoundingClientRect();
        return r.width >= 60 && r.height >= 40;
    };
    const captcha = [...document.querySelectorAll(
        'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], ' +
        'iframe[src*="turnstile" i], iframe[src*="challenge" i], ' +
        '[class*="captcha" i], [id*="captcha" i], [class*="geetest" i], [class*="slider" i], ' +
        '[class*="turnstile" i], [data-sitekey]'
    )].filter(el => visible(el) && bigEnough(el)).map(el => buildSelector(el));

    // ---- meta ----
    const meta = {};
    document.querySelectorAll('meta[name], meta[property]').forEach(el => {
        const key = el.getAttribute('name') || el.getAttribute('property');
        const value = el.getAttribute('content');
        if (key && value && value.length < 300 && /error|status|code|message|title|description/i.test(key)) {
            meta[key] = value;
        }
    });

    // ---- window 上的网页初始化变量(常常含状态/错误码) ----
    const winVars = {};
    for (const key of Object.keys(window)) {
        if (!/_(fe|ss)Config|error|status|code|message/i.test(key)) continue;
        try {
            const value = window[key];
            if (value === null || value === undefined) continue;
            if (typeof value === 'object') {
                winVars[key] = JSON.stringify(value).slice(0, 800);
            } else if (typeof value !== 'function') {
                winVars[key] = String(value).slice(0, 300);
            }
        } catch (e) { /* 跨域/不可枚举, 忽略 */ }
    }

    return {
        url: window.location.href,
        title: (document.title || '').trim(),
        body_text: bodyText,
        main_text: mainText,
        headings,
        error_blocks: errorBlocks.slice(0, 8),
        forms,
        password_inputs: passwordInputs,
        buttons: [...new Set(buttons)].slice(0, 25),
        auth_links: authLinks.slice(0, 10),
        captcha,
        meta,
        win_vars: winVars,
        dom_nodes: document.querySelectorAll('*').length,
        interactive_count: document.querySelectorAll('a[href], button, input, select, textarea').length,
        html_length: (document.documentElement ? document.documentElement.outerHTML.length : 0),
    };
}
"""

#: 错误码/请求 ID 的常见写法
_CODE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:error|err)[_\s-]?code\b[\s:=]+([A-Za-z0-9_.-]{2,32})", "error_code"),
    (r"\b(?:status|state)[_\s-]?code\b[\s:=]+([A-Za-z0-9_.-]{2,32})", "status_code"),
    (r"\bcode\b[\s:：=]+(\d{3,6})\b", "code"),
    (r"\b(request|trace|track|log)[_\s-]?id\b[\s:=]+([A-Za-z0-9-]{6,64})", "request_id"),
    (r"\breq(?:uest)?[_\s-]?no\b[\s:=]+([A-Za-z0-9-]{4,64})", "request_id"),
    (r"\b(\d{13,})\b", "timestamp_like"),
)


def extract_codes(*texts: str) -> dict[str, str]:
    """从文本里提取错误码/请求 ID(用于让用户能向站点反馈或自查)。"""
    found: dict[str, str] = {}
    blob = "\n".join(t for t in texts if t)
    for pattern, name in _CODE_PATTERNS:
        match = re.search(pattern, blob, re.IGNORECASE)
        if match:
            # 有捕获组时取最后一组(request_id 的 pattern 有前缀组)
            value = match.groups()[-1]
            if name not in found and value:
                found[name] = value
    return found


def flatten_text(el: Any, limit: int = 200) -> str:
    """把采集到的元素文本压成单行(用于生成摘要)。"""
    text = str(el or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def looks_like_spa_shell(
    text: str,
    html_length: int = 0,
    dom_nodes: int = 0,
    interactive_count: int = 0,
) -> bool:
    """判断页面是否只是一个"空壳"(几乎没有可见内容, 且没有任何可交互元素)。

    **为什么需要"且"**: 只用可见文本长度会误判 —— 一个只有三件商品的极简列表页
    可见文本不到 100 字, 但它是**有内容的**。真正的空壳(``<div id="root"></div>``
    等 JS 挂载点)不仅文本少, 而且**连一个链接/按钮/输入框都没有**。加上这个条件后,
    极简但正常的页面不会被误判。

    ``html_length`` 与 ``dom_nodes`` 保留在签名里用于将来扩展(例如整站都是内联脚本
    的场景), 当前不作为必要条件: 真实站点的错误页 HTML 往往很长、DOM 也不少, 但可见
    文本只有十几个字。
    """
    stripped = re.sub(r"\s+", "", text or "")
    if len(stripped) >= 80:
        return False
    # 文本很少: 再看结构。有可交互元素说明这是个真实页面(只是简短), 不是空壳。
    return interactive_count == 0 or dom_nodes < 15


async def collect_page_diagnostics(page: Page) -> dict[str, Any]:
    """采集页面的诊断信息。失败时返回带 error 字段的空结构(不抛异常)。"""
    try:
        data = await page.evaluate(COLLECT_JS)
        if not isinstance(data, dict):
            return {"error": "采集脚本返回了非字典结果"}
        return data
    except Exception as exc:  # noqa: BLE001 - 诊断采集失败不应影响主流程
        return {"error": f"{type(exc).__name__}: {exc}"}
