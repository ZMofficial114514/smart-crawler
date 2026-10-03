"""
UI 验收: 登录墙提醒卡 + 插件下载产物卡的真实渲染。

用**本地合成页面**当靶子, 因此在离线环境也能稳定复现:
- ``/protected`` 返回一个典型登录页(重定向语义 + 密码框 + 登录表单 + 提示语);
- ``/gallery``   返回一个带多张图片的列表页, 用来验证默认图片插件与产物卡片。

断言的是"界面上真的出现了提醒", 而不是仅后端返回了字段 —— 这类诊断信息
如果没渲染出来, 对用户等于不存在。

用法: python scripts/verify_ui_alerts.py
"""

from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
OUT = Path("data/screenshots")

LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>请先登录 - 受保护站点</title></head><body>
<h1>请先登录后查看数据</h1>
<p class="tip">该数据仅对已登录用户开放, 未登录访问将被跳转到登录页。</p>
<form action="/login" method="post">
  <label>账号</label><input type="text" name="username" placeholder="手机号 / 邮箱">
  <label>密码</label><input type="password" name="password" placeholder="请输入密码">
  <button type="submit">登录</button>
</form>
<a href="/register">还没有账号? 立即注册</a>
</body></html>"""

# 1x1 像素 PNG, 避免依赖外网图床
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

GALLERY_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>图片列表</title></head><body>
<h1>图库</h1>
<div class="items">
""" + "".join(
    f'<article class="item"><h3>图片 {i}</h3>'
    f'<img src="/img/pic{i}.png" alt="pic{i}"><span class="price">¥{i}0.00</span></article>'
    for i in range(1, 7)
) + """
</div></body></html>"""

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class Handler(http.server.BaseHTTPRequestHandler):
    """本地靶站: /protected(含其它未知路径)是登录页, /gallery 是图库。"""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/img/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return

        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow:\n"
            ctype = "text/plain"
        elif self.path.startswith("/gallery"):
            body = GALLERY_PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            # 其余路径(含 /protected)一律返回登录页 —— 模拟"需登录才可访问"
            body = LOGIN_PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002 - 静默访问日志
        pass


def wait_task_idle(page, timeout_ms: int = 240000) -> None:
    """等当前任务进入终态(开始按钮恢复可见 = 没有任务在跑)。

    任务进行中「开始抓取」会被隐藏, 这是设计行为; 测试必须先等它结束,
    否则会像用户"手快连点"一样撞上按钮不可见。
    超时不直接抛错, 而是打印现场状态后继续 —— 后续断言会给出更有意义的失败原因。
    """
    try:
        page.wait_for_function(
            "() => { const b = document.querySelector('#btnCrawlStart'); return b && !b.hidden; }",
            timeout=timeout_ms,
        )
    except Exception:
        snapshot = page.evaluate(
            """() => {
                const b = document.querySelector('#btnCrawlStart');
                const tag = document.querySelector('#taskStatusTag');
                return {
                    startHidden: b ? b.hidden : null,
                    startDisabled: b ? b.disabled : null,
                    startLoading: b ? b.classList.contains('is-loading') : null,
                    statusText: tag ? tag.textContent : null,
                    statusState: tag ? tag.dataset.state : null,
                    progress: document.querySelector('#progressPct')?.textContent,
                };
            }"""
        )
        print(f"  [!] 等待任务空闲超时, 当前页面状态: {snapshot}")
        raise
    page.wait_for_timeout(400)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    protected = f"http://127.0.0.1:{port}/protected"
    print(f"本地靶站已启动: {protected}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1680, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # ==============================================================
        print("\n=== 1) 结构分析页: 访问受限诊断 ===")
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(900)
        page.fill("#analyzeUrl", protected)
        page.click("#btnAnalyzeStart")
        # 等提醒卡真正出现
        try:
            page.wait_for_selector("#analyzeOutput .login-wall", timeout=120000)
            appeared = True
        except Exception:
            appeared = False

        check(appeared, "分析页渲染出了访问受限诊断卡")
        if appeared:
            title = page.text_content("#analyzeOutput .login-wall__title") or ""
            check("登录" in title, "诊断卡标题可读", title.strip())
            reasons = page.eval_on_selector_all("#analyzeOutput .login-wall__list li", "e => e.map(x => x.textContent)")
            check(len(reasons) >= 2, "列出了判定依据", f"{len(reasons)} 条")
            elements = page.eval_on_selector_all("#analyzeOutput .login-wall__element", "e => e.length")
            check(elements >= 2, "展示了页面关键文本元素", f"{elements} 个")
            roles = page.eval_on_selector_all("#analyzeOutput .login-wall__role", "e => e.map(x => x.textContent)")
            check("标题" in roles, "文本元素带语义标签", ", ".join(roles[:5]))
            redirect = page.eval_on_selector("#analyzeOutput .login-wall__redirect", "e => e.textContent")
            check(protected in (redirect or ""), "展示了请求 URL 与实际 URL 的对比")

            shot = OUT / "11_access_denied_analyze.png"
            page.screenshot(path=str(shot))
            print(f"  截图 -> {shot.name}")

        # ==============================================================
        print("\n=== 2) 抓取页: 访问受限诊断 ===")
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(700)
        wait_task_idle(page)  # 等第 1 步的分析任务结束, 避免抢跑
        page.fill("#crawlUrl", protected)
        page.fill("#crawlGoal", "抓取所有条目的标题")
        page.select_option("#crawlFormat", "")
        page.click("#btnCrawlStart")
        try:
            page.wait_for_selector("#crawlResultCard:not([hidden]) .login-wall", timeout=180000)
            appeared2 = True
        except Exception:
            appeared2 = False
        check(appeared2, "抓取页也渲染出了访问受限诊断卡")
        if appeared2:
            suggestions = page.eval_on_selector_all(
                "#crawlResultCard .login-wall__list li", "e => e.map(x => x.textContent)"
            )
            check(len(suggestions) >= 2, "给出了多条处置建议", f"{len(suggestions)} 条")
            # 关键回归点: 本地靶站返回的是"没有权限"型错误页(401 + 无密码框),
            # 诊断必须归到权限类, 而不是含糊地报"需要登录"。
            issue_type = page.get_attribute("#crawlResultCard .login-wall", "data-type")
            print(f"  诊断类型: {issue_type}")
            check(
                issue_type in ("permission_denied", "login_required", "unknown"),
                "诊断类型已标注在卡片上",
                str(issue_type),
            )
            # 页面实际文本必须能被看到(这正是"提供文本元素找出问题"的落点)
            raw_visible = page.eval_on_selector_all(
                "#crawlResultCard .login-wall__text", "e => e.map(x => x.textContent).join(' ')"
            )
            check(
                "登录" in (raw_visible or "") or "权限" in (raw_visible or ""),
                "卡片刻出了页面上的关键文本",
                (raw_visible or "")[:60],
            )
            shot2 = OUT / "12_access_denied_crawl.png"
            page.screenshot(path=str(shot2))
            print(f"  截图 -> {shot2.name}")

        # ==============================================================
        print("\n=== 3) 图片插件: 下载产物卡片 ===")
        gallery = f"http://127.0.0.1:{port}/gallery"
        wait_task_idle(page)
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(600)
        page.fill("#crawlUrl", gallery)
        page.fill("#crawlGoal", "抓取所有图片的名称和价格")
        page.fill("#crawlMaxPages", "1")
        page.click("#btnCrawlStart")
        try:
            page.wait_for_selector("#crawlResultCard:not([hidden]) .download-item", timeout=180000)
            appeared3 = True
        except Exception:
            appeared3 = False
        check(appeared3, "结果区渲染出了插件下载产物卡片")
        if appeared3:
            count = page.eval_on_selector_all("#crawlResultCard .download-item", "e => e.length")
            check(count >= 1, "至少有一个下载条目", f"{count} 个")
            names = page.eval_on_selector_all("#crawlResultCard .download-item__name", "e => e.map(x => x.textContent)")
            check(all(n.endswith(".png") for n in names), "下载项显示了文件名", ", ".join(names[:3]))
            metas = page.eval_on_selector_all("#crawlResultCard .download-item__meta", "e => e.map(x => x.textContent)")
            check(any("B" in (m or "") for m in metas), "下载项显示了体积与类型", metas[0] if metas else "")
            shot3 = OUT / "13_plugin_downloads.png"
            page.screenshot(path=str(shot3))
            print(f"  截图 -> {shot3.name}")

        # ==============================================================
        print("\n=== 4) 插件页交互 ===")
        page.evaluate("window.location.hash = '#/plugins'")
        page.wait_for_timeout(1500)
        cards = page.eval_on_selector_all(".plugin-card", "e => e.length")
        check(cards >= 4, "插件卡片渲染完整", f"{cards} 个")
        # 打开第一个插件的配置弹层
        page.click(".plugin-card .btn:has-text('配置')")
        page.wait_for_timeout(1100)
        modal_open = page.eval_on_selector("#modal", "e => e.classList.contains('is-open')")
        fields = page.eval_on_selector_all("#modal .field", "e => e.length")
        check(modal_open, "配置弹层能打开")
        check(fields >= 1, "弹层按 config_schema 生成了表单", f"{fields} 个字段")
        shot4 = OUT / "14_plugin_config.png"
        page.screenshot(path=str(shot4))
        print(f"  截图 -> {shot4.name}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # ==============================================================
        print("\n=== 5) 结构分析: 简化 DOM 树的换行渲染 ===")
        # 这是用户报的"转义字符没处理"问题: 简化 DOM 树是**逐行**文本, 早先整段塞进
        # <pre> 后显示成 `body\n  div\n    span`, 一整棵树挤成一行。这里断言:
        # ① 树被渲染成**多行**(真实 <br>/行数, 而不是字面量 \n);
        # ② 用的是纯文本视图(.plain), 没被 JSON 着色器乱染色。
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(900)
        page.fill("#analyzeUrl", f"http://127.0.0.1:{port}/gallery")
        page.click("#btnAnalyzeStart")
        try:
            # state="attached": DOM 树在**收起的** <details> 里, 默认的 visible 判定会
            # 一直等不到 —— 元素其实早就渲染好了。
            page.wait_for_selector("#analyzeOutput .dom-tree", timeout=180000, state="attached")
            tree_ok = True
        except Exception:
            tree_ok = False
            # 失败时把实际渲染结果打出来, 否则只看到一句"没渲染出来"没法查
            dump = page.evaluate(
                """() => {
                    const out = document.querySelector('#analyzeOutput');
                    let store = null;
                    try {
                        // store 内部状态能从 localStorage 里读到(lastAnalyzeTaskId 等)
                        store = {
                            lastAnalyzeTaskId: window.localStorage.getItem('lastAnalyzeTaskId'),
                            lastTask: (window.localStorage.getItem('task') || '').slice(0, 200),
                        };
                    } catch (e) { store = String(e); }
                    return {
                        len: out ? out.innerHTML.length : 0,
                        cards: out ? out.querySelectorAll('.card').length : 0,
                        heads: out ? [...out.querySelectorAll('h2')].map(h => h.textContent.trim()) : [],
                        alerts: out ? [...out.querySelectorAll('.alert__title')].map(a => a.textContent.trim()) : [],
                        hasTree: out ? out.innerHTML.includes('simplified_tree') : false,
                        urlField: (document.querySelector('#analyzeUrl') || {}).value,
                        store,
                    };
                }"""
            )
            print(f"  [诊断] {dump}")
        check(tree_ok, "分析页渲染出了简化 DOM 树")
        if tree_ok:
            # 展开 details 才能拿到可测量的渲染结果
            page.eval_on_selector("#analyzeOutput details.tree", "e => e.open = true")
            page.wait_for_timeout(500)

            text = page.eval_on_selector("#analyzeOutput .dom-tree", "e => e.textContent")
            lines = [l for l in (text or "").split("\n") if l.strip()]
            check(len(lines) >= 5, "DOM 树渲染成了多行", f"{len(lines)} 行")
            check(
                "\\n" not in (text or ""),
                "**没有把字面量 \\n 原样打印出来**",
                repr((text or "")[:80]),
            )

            # 行号是纯文本视图的标志, 也能证明不是 JSON 着色
            nums = page.eval_on_selector_all("#analyzeOutput .dom-tree .pt-num", "e => e.length")
            check(nums >= 5, "带行号(纯文本视图)", f"{nums} 个行号")
            colored = page.eval_on_selector_all(
                "#analyzeOutput .dom-tree span[class^='j-']", "e => e.length"
            )
            check(colored == 0, "**没有被 JSON 着色器染色**", f"{colored} 个着色 span")

            # 几何校验: 多行文本的实际高度应明显大于单行
            height = page.eval_on_selector(
                "#analyzeOutput .dom-tree", "e => e.getBoundingClientRect().height"
            )
            check(height > 40, "文本块有真实的多行高度", f"{height:.0f}px")

            shot5 = OUT / "17_dom_tree_rendering.png"
            page.screenshot(path=str(shot5))
            print(f"  截图 -> {shot5.name}")

        browser.close()

    server.shutdown()

    print(f"\n前端错误: {len(errors)}")
    for err in errors[:8]:
        print(f"  ! {err}")
    check(not errors, "无前端错误")

    print("\n" + "=" * 64)
    if failures:
        print(f"UI 提醒与产物验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"UI 提醒与产物验收: 通过 ✓ ({total}/{total})")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
