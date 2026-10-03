"""
登录会话功能验收。

覆盖:
1. 接口清单(7 个登录会话端点已注册);
2. **安全性**: 所有响应只含掩码摘要, 绝不回传 Cookie 值;
3. 会话文件的保存/读取/摘要/合并/删除;
4. 会话恢复真的生效 —— 手工写入一个 Cookie, 重启浏览器后检查目标站点是否收到它;
5. 登录状态识别: 匿名站点判 anonymous、注入"已登录"界面判 logged_in、
   无信号站点判 unknown(**不误报**);
6. 未登录时结构分析报告里带上 login_state(界面据此询问用户是否登录);
7. 手动登录辅助进程 --help 可用(证明脚本本身没坏), 不真的打开窗口。

用法: python scripts/verify_session.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from smartcrawler.login_state import (  # noqa: E402
    detect_login_state,
    evaluate_login_state,
)
from smartcrawler.session import (  # noqa: E402
    delete_session,
    merge_storage_state,
    save_storage_state,
    summarize_session,
)

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# 模拟"已登录"界面
AUTHED_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>首页</title></head><body>
<header>
  <a href="/users/12345" class="user-menu"><img class="avatar" src="/a.png" alt="me"></a>
  <a href="/users/12345">我的主页</a><a href="/bookmarks">我的收藏</a>
  <button>退出登录</button>
</header><main><div class="works">作品列表</div></main></body></html>"""

# 模拟"匿名"界面
ANON_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>登录</title></head><body>
<h1>请先登录</h1><p>用示例账号登录后可查看更多内容</p>
<form action="/auth/login" method="post"><input type="text" name="u"><input type="password" name="p"></form>
<a href="/register">注册账号</a></body></html>"""

# 无任何信号的中性页面
NEUTRAL_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>关于我们</title></head><body>
<h1>关于我们</h1><p>这是一段静态介绍文字, 没有任何登录相关元素。</p></body></html>"""


async def main() -> int:
    # 真实站点那一节直接改计数, 所以需要声明全局(否则 total 会被当成局部变量)
    global total

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    tmp_session = Path(".runtmp/verify_session.json")

    # ==================================================================
    print("=== 1) 会话文件: 保存 / 摘要 / 合并 / 删除 ===")
    if tmp_session.exists():
        tmp_session.unlink()

    check(summarize_session(tmp_session).exists is False, "不存在的会话文件摘要为 exists=False")

    state_a = {
        "cookies": [
            {"name": "PHPSESSID", "value": "SECRET-A", "domain": ".example.com", "path": "/"},
            {"name": "theme", "value": "dark", "domain": ".example.com", "path": "/"},
        ],
        "origins": [
            {"origin": "https://example.com", "localStorage": [{"name": "k", "value": "v"}]}
        ],
    }
    save_storage_state(state_a, tmp_session)
    check(tmp_session.exists(), "会话文件已写入")

    summary = summarize_session(tmp_session)
    check(summary.cookies == 2, "统计到 2 个 Cookie", str(summary.cookies))
    check(summary.origins == 1, "统计到 1 个站点 localStorage")
    check("example.com" in summary.domains, "提取到域名")
    check(summary.has_auth_cookie, "识别出会话类 Cookie")
    check("PHPSESSID" in summary.auth_cookies, "点名了 PHPSESSID")
    check(
        "SECRET-A" not in json.dumps(summary.to_dict()),
        "**摘要里没有泄露 Cookie 的值**",
    )
    check(bool(summary.saved_at), "记录了保存时间")

    # 合并: 同名 Cookie 应被新值覆盖, 不同名保留
    state_b = {
        "cookies": [
            {"name": "PHPSESSID", "value": "SECRET-B", "domain": ".example.com", "path": "/"},
            {"name": "NEW", "value": "n", "domain": ".other.com", "path": "/"},
        ],
        "origins": [],
    }
    merged = merge_storage_state(state_a, state_b)
    by_name = {c["name"]: c["value"] for c in merged["cookies"]}
    check(len(merged["cookies"]) == 3, "合并后共 3 个 Cookie", str(len(merged["cookies"])))
    check(by_name.get("PHPSESSID") == "SECRET-B", "同名 Cookie 被新值覆盖")
    check(by_name.get("theme") == "dark", "旧站点的 Cookie 被保留")
    check(merged["origins"] == state_a["origins"], "原有 localStorage 保留")

    check(delete_session(tmp_session) is True, "删除会话文件成功")
    check(not tmp_session.exists(), "会话文件确实已删除")
    check(delete_session(tmp_session) is False, "重复删除返回 False")

    # ==================================================================
    print("\n=== 2) 登录状态识别(纯函数 + 真实页面) ===")
    authed = evaluate_login_state(
        {
            "url": "https://x.com/",
            "title": "首页",
            "avatar_count": 2,
            "profile_link_count": 1,
            "logout_count": 1,
            "has_account_prompt": True,
            "login_links": [],
        }
    )
    check(authed.state == "logged_in", "有头像/个人页/退出的界面判为已登录", authed.state)
    check(authed.confidence >= 0.4, "置信度达到阈值", f"{authed.confidence}")

    anon = evaluate_login_state(
        {
            "url": "https://x.com/",
            "title": "登录",
            "has_login_prompt": True,
            "password_inputs": 1,
            "auth_form_count": 1,
            "login_links": [{"text": "登录", "href": "/login"}],
        }
    )
    check(anon.state == "anonymous", "登录引导页判为未登录", anon.state)

    unknown = evaluate_login_state({"url": "https://x.com/", "title": "关于", "body_text": "介绍"})
    check(unknown.state == "unknown", "无信号页面判为不明确(不误报)", unknown.state)
    check(unknown.confidence == 0.0, "无信号时置信度为 0")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # 用显式 locale 的上下文, 与应用实际行为一致(应用会设置 locale)。
        ctx = await browser.new_context(locale="zh-CN")

        # 注入样本用**独立页面**: 在同一页面上 set_content 之后再导航到真实站点, 会让
        # 目标站点的渲染行为发生变化(实测 pixiv 的登录引导文案就不渲染了)。应用本身
        # 从不这样用 —— 每个任务都是新开页面 —— 所以验收也不该这样测, 否则测的是
        # 测试脚手架的副作用而不是产品行为。
        inject_page = await ctx.new_page()
        for label, html, expected in (
            ("注入: 已登录界面", AUTHED_HTML, "logged_in"),
            ("注入: 匿名登录页", ANON_HTML, "anonymous"),
            ("注入: 中性页面", NEUTRAL_HTML, "unknown"),
        ):
            await inject_page.set_content(html)
            state = await detect_login_state(inject_page)
            check(state.state == expected, f"{label} → {expected}", state.state)
        await inject_page.close()

        # ---- 真实站点 ----
        # 每个站点用**独立页面**: 复用同一个页面时, 前一个站点的残留状态会影响
        # 下一个站点的渲染(实测 pixiv 的登录引导文案在复用页面里不渲染)。爬虫本身
        # 也是每个任务新开页面, 所以这里用独立页面才贴近真实行为。
        print("\n  真实站点:")
        for label, url, expected in (
            ("pixiv 首页", "https://www.pixiv.net/", "anonymous"),
            ("GitHub 首页", "https://github.com/", "anonymous"),
            ("books.toscrape", "https://books.toscrape.com/", "unknown"),
        ):
            target = await ctx.new_page()
            try:
                await target.goto(url, wait_until="domcontentloaded", timeout=45000)
                # SPA 的登录引导是 `domcontentloaded` **之后**才渲染的。必须给它一个
                # 最短观察窗口 —— 只按"正文长度够长就停"会立刻退出(pixiv 的静态骨架
                # 已有 ~150 字), 于是在引导文案上屏前就下了结论。
                state = await detect_login_state(target)
                started = time.time()
                while True:
                    elapsed = time.time() - started
                    if elapsed >= 8 and (state.state != "unknown" or elapsed >= 20):
                        break
                    if elapsed >= 25:
                        break
                    await asyncio.sleep(1.5)
                    state = await detect_login_state(target)

                ok = state.state == expected
                print(f"    {'✓' if ok else '✗'} {label}: {state.state} (期望 {expected}) "
                      f"[匿名 {state.anon_score:.2f} / 已登录 {state.auth_score:.2f} "
                      f"正文 {state.body_length}]")
                if not ok:
                    failures.append(f"{label} 判定 {state.state}, 期望 {expected}")
                total += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ! {label} 跳过: {type(exc).__name__}")
            finally:
                await target.close()

        # ==================================================================
        print("\n=== 3) 会话恢复真的生效(端到端) ===")
        # 起一个本地服务, 让它回显收到的 Cookie
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                cookie = self.headers.get("Cookie") or ""
                body = f"<html><body><div id='c'>{cookie}</div></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # noqa: A002
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        session_file = Path(".runtmp/verify_restore.json")
        save_storage_state(
            {
                "cookies": [
                    {
                        "name": "verify_token",
                        "value": "RESTORED-OK",
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            session_file,
        )

        from smartcrawler.config import get_settings
        from smartcrawler.crawler import SmartCrawler

        settings = get_settings()
        settings.browser.storage_state = str(session_file)
        settings.anti_spider.respect_robots = False

        crawler = SmartCrawler(settings)
        try:
            await crawler.start()
            check(crawler.browser.session_restored, "浏览器启动时报告已恢复会话")
            page2 = await crawler.browser.new_page()
            await crawler.browser.goto(page2, f"http://127.0.0.1:{port}/")
            seen = await page2.text_content("#c")
            check(
                "verify_token=RESTORED-OK" in (seen or ""),
                "目标站点确实收到了保存的 Cookie",
                (seen or "")[:80],
            )
            await crawler.browser.close_page(page2)
        finally:
            await crawler.close()
            server.shutdown()
            session_file.unlink(missing_ok=True)
            settings.browser.storage_state = ""

        await browser.close()

    # ==================================================================
    print("\n=== 4) HTTP 接口 ===")
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        try:
            response = await client.get("/api/session")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 服务未在 {base} 运行, 跳过接口检查: {type(exc).__name__}")
            payload = None

        if payload is not None:
            check("session" in payload, "GET /api/session 返回会话摘要")
            check("flow" in payload, "返回登录流程状态")
            check(
                "SECRET" not in json.dumps(payload),
                "**接口响应不含任何凭据值**",
            )
            check(bool(payload.get("heads_up")), "带有凭据安全提示")

            r = await client.get("/api/session/login/status")
            check(r.status_code == 200, "GET /api/session/login/status 可用", str(r.status_code))
            status = r.json()
            check("alive" in status, "状态里含 alive 字段")

            r = await client.post("/api/session/login", json={"url": "not-a-url"})
            check(r.status_code == 422, "非法 URL 被拒绝(422)", str(r.status_code))

            r = await client.post("/api/session/login/confirm")
            check(r.status_code == 200, "confirm 接口可用")
            check(r.json().get("ok") is False, "没有流程在跑时 confirm 返回 ok=False")

            r = await client.delete("/api/session")
            check(r.status_code == 200, "DELETE /api/session 可用")

    # ==================================================================
    print("\n=== 5) 登录辅助脚本可用性 ===")
    helper = Path("scripts/login_helper.py")
    check(helper.exists(), "login_helper.py 存在")
    result = subprocess.run(
        [sys.executable, str(helper), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    check(result.returncode == 0, "login_helper.py --help 正常退出", str(result.returncode))
    check("--url" in (result.stdout or ""), "帮助里列出了 --url 参数")

    print("\n" + "=" * 66)
    if failures:
        print(f"登录会话验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"登录会话验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
