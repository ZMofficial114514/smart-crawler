"""
访问受限诊断的验收测试。

用**本地合成页面**而非真实站点: 可复现、离线可跑, 还能精确构造"应该判定为哪一类"
与"不应误判"的样本。真实世界的样本(洛谷训练页)也作为回归基准写进来了。

覆盖:
1. 洛谷式权限不足(HTTP 401 + URL 不变 + "没有权限请求此资源。") → permission_denied,
   **不能**误判成 login_required;
2. 经典登录页(重定向 + 密码框 + 登录表单) → login_required;
3. 风控页(Cloudflare 挑战 / "访问过于频繁" / IP 封禁) → risk_control / rate_limited;
4. 验证码页 → captcha;
5. 正常列表页 → **不判定**(防误报);
6. 空壳页面(几乎无内容) → spa_shell;
7. 纯锚点变化不算跳转;
8. 页面文本元素被正确提取(供界面展示);
9. 错误码/请求 ID 提取;
10. 与 SmartCrawler.analyze_only 的集成。

用法: python scripts/verify_access_control.py
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.async_api import async_playwright  # noqa: E402

from smartcrawler.access_control import detect_access_issue, url_changed  # noqa: E402

# ---------------------------------------------------------------------------
# 合成样本
# ---------------------------------------------------------------------------
# 洛谷训练页的真实结构: SPA 错误页, 没有密码框/登录表单, URL 不变
LUOGU_LIKE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Error - 洛谷</title></head><body>
<div id="app"><div class="container">
  <h1>出错啦</h1>
  <div class="error-message">没有权限请求此资源。</div>
  <p>关于洛谷 · 帮助中心 · 用户协议 · 联系我们</p>
  <a href="/auth/login">登录</a><a href="/auth/register">注册</a>
</div></div>
</body></html>"""

LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>用户登录 - 示例站点</title></head><body>
<h1>请先登录</h1>
<p class="tip">登录后才能查看数据, 未登录用户仅能浏览首页。</p>
<form action="/login" method="post">
  <label>账号</label><input type="text" name="username" placeholder="手机号/邮箱">
  <label>密码</label><input type="password" name="password" placeholder="请输入密码">
  <button type="submit">登录</button>
</form>
<a href="/register">还没有账号?立即注册</a>
</body></html>"""

RISK_CONTROL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>安全验证</title></head><body>
<div class="challenge-platform">
  <h1>正在进行安全验证</h1>
  <p>检测到异常访问, 请完成验证后继续。</p>
  <p>cf_chl_opt 校验中…</p>
  <div class="captcha-container"></div>
</div>
</body></html>"""

RATE_LIMITED = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>访问受限</title></head><body>
<h1>访问过于频繁</h1>
<p>您的请求过于频繁, 请稍后再试。</p>
<p>您的IP地址已被限制访问。</p>
</body></html>"""

CAPTCHA_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>人机验证</title></head><body>
<h1>请完成验证</h1>
<p>拖动滑块完成拼图验证</p>
<iframe src="/recaptcha/api.js"></iframe>
<div id="geetest-box" class="geetest_panel"></div>
</body></html>"""

NORMAL_LIST = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>商品列表 - 示例站点</title></head><body>
<h1>全部商品</h1>
<div class="products">
  <article class="product"><h3>商品 A</h3><span class="price">¥45.00</span></article>
  <article class="product"><h3>商品 B</h3><span class="price">¥52.00</span></article>
  <article class="product"><h3>商品 C</h3><span class="price">¥38.50</span></article>
</div>
<nav><a href="/login">登录</a><a href="/about">关于</a></nav>
</body></html>"""

SPA_SHELL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>App</title></head>
<body><div id="root"></div><script src="/app.js"></script></body></html>"""

SERVER_ERROR = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>502 Bad Gateway</title></head><body>
<h1>502 Bad Gateway</h1>
<p>服务暂时不可用, 请稍后重试。</p>
</body></html>"""

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


async def probe(page, html: str, requested: str, status: int | None = 200):
    """把一个合成页面塞进浏览器并跑一次诊断。

    注意: 这里用 ``set_content`` 把样本直接注入, 因此 ``page.url`` 会是 ``about:blank``。
    对需要"URL 未变"这一前提的用例(如洛谷式 401), 必须改用 :func:`probe_http`, 否则
    ``about:blank`` 与请求 URL 的差异会被正确地判成"跳转", 让用例失去意义。
    """
    await page.set_content(html)
    return await detect_access_issue(page, requested, http_status=status)


async def probe_http(page, base: str, path: str, requested: str | None = None,):
    """通过真实 HTTP 服务打开样本页面, 让 ``page.url`` 与请求地址一致。"""
    url = f"{base}{path}"
    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    return await detect_access_issue(
        page, requested or url, http_status=response.status if response else None
    )


def start_sample_server(samples: dict[str, tuple[str, int]]):
    """起一个本地服务, 把 ``{路径: (HTML, 状态码)}`` 映射成真实 HTTP 响应。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/robots.txt":
                body, status = b"User-agent: *\nDisallow:\n", 200
            elif path in samples:
                html, status = samples[path]
                body = html.encode("utf-8")
            else:
                body, status = b"<html><body>not found</body></html>", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A002 - 静默
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


async def main() -> int:
    # 所有样本都通过**真实 HTTP 服务**提供, 这样 page.url 与实际请求地址一致,
    # URL 变化判定才有意义(否则 about:blank 会让每个用例都被判成"跳转")。
    samples = {
        "/denied": (LUOGU_LIKE, 401),
        "/login": (LOGIN_PAGE, 200),
        "/risk": (RISK_CONTROL, 403),
        "/rate": (RATE_LIMITED, 200),
        "/captcha": (CAPTCHA_PAGE, 200),
        "/normal": (NORMAL_LIST, 200),
        "/shell": (SPA_SHELL, 200),
        "/err": (SERVER_ERROR, 502),
        "/code": (
            """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Error</title></head><body>
            <h1>出错啦</h1><p>没有权限请求此资源。 error_code: AUTH_4031 request_id: 7f3a9c2e-11b4 </p>
            </body></html>""",
            401,
        ),
    }
    server, base = start_sample_server(samples)
    print(f"样本服务已启动: {base}")
    print(f"（洛谷式 401 样本: {base}/denied?board=scoreboard）\n")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # ==============================================================
            print("=== 1) 洛谷式权限不足(HTTP 401, URL 不变, 无登录表单) ===")
            # 关键: 请求带 query, 实际 URL 也带同样的 query —— 不构成跳转
            issue = await probe_http(page, base, "/denied?board=scoreboard")
            print(f"  类型={issue.issue_type} 置信度={issue.confidence:.0%} 摘要={issue.summary()}")
            for r in issue.reasons:
                print(f"    · {r}")
            check(issue.detected, "判定为访问受限")
            check(issue.issue_type == "permission_denied", "类型为 permission_denied", issue.issue_type)
            check(issue.issue_type != "login_required", "**没有**误判成 login_required")
            check(issue.confidence >= 0.5, "置信度达到阈值", f"{issue.confidence:.2f}")
            check(not issue.redirected, "URL 未变化时没被误报为跳转")
            check(issue.http_status == 401, "记录了 HTTP 401", str(issue.http_status))
            check(issue.page_title == "Error - 洛谷", "取到了页面标题", issue.page_title)
            check("没有权限" in issue.visible_text, "取到了页面实际文本")
            check("没有权限" in issue.main_text, "主内容区文本可用")
            texts = [e.text for e in issue.text_elements]
            check(any("出错" in t for t in texts), "文本元素含标题『出错啦』")
            check(any("没有权限" in t for t in texts), "文本元素含错误正文")
            check(bool(issue.main_text), "主内容区文本非空")
            check(
                any("报名" in s or "权限" in s for s in issue.suggestions),
                "建议指向权限方向而非登录",
            )
            check(not any("session.json" in s for s in issue.suggestions), "建议里没有误导性的会话配置项")

            # ==============================================================
            print("\n=== 2) 经典登录页 ===")
            issue2 = await probe_http(page, base, "/login")
            print(f"  类型={issue2.issue_type} 置信度={issue2.confidence:.0%}")
            check(issue2.detected, "判定为访问受限")
            check(issue2.issue_type == "login_required", "类型为 login_required", issue2.issue_type)
            check(issue2.clues.get("password_inputs", 0) >= 1, "识别出密码输入框")
            check(any("session.json" in s for s in issue2.suggestions), "建议包含会话复用方案")

            # ==============================================================
            print("\n=== 3) 风控页(Cloudflare 挑战, HTTP 403) ===")
            issue3 = await probe_http(page, base, "/risk")
            print(f"  类型={issue3.issue_type} 置信度={issue3.confidence:.0%}")
            check(issue3.detected, "判定为访问受限")
            check(issue3.issue_type in ("risk_control", "captcha"), "类型为风控/验证类", issue3.issue_type)
            check(
                any("限速" in s or "代理" in s or "反爬" in s for s in issue3.suggestions),
                "建议指向限速/代理/反爬",
            )

            # ==============================================================
            print("\n=== 4) 限速 / IP 限制 ===")
            issue4 = await probe_http(page, base, "/rate")
            print(f"  类型={issue4.issue_type} 置信度={issue4.confidence:.0%}")
            check(issue4.detected, "判定为访问受限")
            check(issue4.issue_type in ("rate_limited", "risk_control"), "类型为限速/风控", issue4.issue_type)

            # ==============================================================
            print("\n=== 5) 验证码页 ===")
            issue5 = await probe_http(page, base, "/captcha")
            print(f"  类型={issue5.issue_type} 置信度={issue5.confidence:.0%}")
            check(issue5.detected, "判定为访问受限")
            check(issue5.issue_type == "captcha", "类型为 captcha", issue5.issue_type)
            check(issue5.clues.get("captcha_elements", 0) >= 1, "识别出验证码组件")

            # ==============================================================
            print("\n=== 6) 正常列表页(防误报) ===")
            issue6 = await probe_http(page, base, "/normal")
            print(f"  类型={issue6.issue_type} 检测={issue6.detected} 置信度={issue6.confidence:.0%}")
            # 导航里有 <a href="/login">登录</a>, 但不能因此判定为登录墙
            check(not issue6.detected, "正常页面未被误判", f"类型={issue6.issue_type} 分数={issue6.scores}")

            # ==============================================================
            print("\n=== 7) 空壳页面 ===")
            issue7 = await probe_http(page, base, "/shell")
            print(f"  类型={issue7.issue_type} 置信度={issue7.confidence:.0%}")
            check(issue7.issue_type == "spa_shell", "识别为空壳页面", issue7.issue_type)
            check(
                any("额外等待" in s or "网络抓包" in s for s in issue7.suggestions),
                "建议指向等待或改用接口",
            )

            # ==============================================================
            print("\n=== 8) 服务端错误 ===")
            issue8 = await probe_http(page, base, "/err")
            print(f"  类型={issue8.issue_type} 置信度={issue8.confidence:.0%}")
            check(issue8.issue_type == "server_error", "识别为服务端错误", issue8.issue_type)

            # ==============================================================
            print("\n=== 9) URL 变化判定(锚点不算跳转) ===")
            changed, why = url_changed(
                "https://www.luogu.com.cn/training/1096881",
                "https://www.luogu.com.cn/training/1096881#scoreboard",
            )
            check(not changed, "纯锚点变化不算跳转", why)
            changed2, why2 = url_changed("https://a.com/x", "https://a.com/login")
            check(changed2, "路径变化算跳转", why2)
            changed3, why3 = url_changed("https://a.com/x", "https://b.com/x")
            check(changed3, "跨域名算跳转", why3)
            check("跨域名" in why3, "跨域名说明可读", why3)
            changed4, _ = url_changed("https://a.com", "https://a.com/")
            check(not changed4, "结尾斜杠差异不算跳转")

            # ==============================================================
            print("\n=== 10) 错误码 / 请求 ID 提取 ===")
            issue10 = await probe_http(page, base, "/code")
            print(f"  提取到: {issue10.error_codes}")
            check(bool(issue10.error_codes), "提取到了错误码/请求 ID")
            check(
                any("AUTH_4031" in str(v) for v in issue10.error_codes.values()),
                "错误码内容正确",
                str(issue10.error_codes),
            )

            # ==============================================================
            print("\n=== 11) 与 SmartCrawler 的集成(analyze_only + crawl) ===")
            from smartcrawler.config import get_settings
            from smartcrawler.crawler import SmartCrawler

            settings = get_settings()
            settings.anti_spider.respect_robots = False  # 本地测试服务

            crawler = SmartCrawler(settings)
            try:
                report, _ = await crawler.analyze_only(f"{base}/denied")
            finally:
                await crawler.close()

            check(report is not None, "结构分析返回报告")
            if report is not None:
                check(report.access_issue is not None, "报告带上了 access_issue")
                if report.access_issue is not None:
                    check(
                        report.access_issue.issue_type == "permission_denied",
                        "集成路径同样归类为 permission_denied",
                        report.access_issue.issue_type,
                    )
                    print(f"    摘要: {report.access_issue.summary()}")

            crawler = SmartCrawler(settings)
            try:
                result = await crawler.crawl(f"{base}/denied", max_pages=1)
            finally:
                await crawler.close()

            check(result.item_count == 0, "无权限页面确实抓不到数据")
            check(result.access_issue is not None, "抓取结果带上了 access_issue")
            if result.access_issue is not None:
                check(
                    result.access_issue.issue_type == "permission_denied",
                    "抓取路径归类为 permission_denied",
                    result.access_issue.issue_type,
                )
                check(bool(result.access_issue.visible_text), "带上了页面文本供排查")
                print(f"    页面文本: {result.access_issue.visible_text[:80]!r}")

            # 正常页面抓不到数据时, 应给出"空页面"而非权限类结论
            crawler = SmartCrawler(settings)
            try:
                result_ok = await crawler.crawl(f"{base}/normal", max_pages=1)
            finally:
                await crawler.close()
            check(result_ok.item_count > 0, "正常页面能抓到数据", f"{result_ok.item_count} 条")
            check(
                result_ok.access_issue is None or not result_ok.access_issue.detected,
                "正常页面没有误报访问受限",
            )

            await browser.close()
    finally:
        server.shutdown()

    print("\n" + "=" * 66)
    if failures:
        print(f"访问受限诊断: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"访问受限诊断: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
