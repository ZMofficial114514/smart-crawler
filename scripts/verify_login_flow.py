"""
手动登录流程的端到端验收。

**难点**: 真实流程要弹出一个可见浏览器等**用户**操作。这里用一个本地靶站代替真实
站点, 并由测试自己在那个浏览器窗口里"完成登录" —— 也就是直接给页面设一个 Cookie。
这样既跑通了完整链路(启动进程 → 可见窗口 → 探测状态 → 确认 → 保存会话 → 主服务
收到并生效), 又不需要任何真实账号。

覆盖:
1. 仓库里没有凭据时, 摘要正确报告"无会话";
2. 启动登录 → 辅助进程起来 → 状态文件出现;
3. 靶站在"登录窗口"里设下 Cookie 后, 探测到 logged_in;
4. 确认保存 → 会话文件真的写出, 且能读回;
5. 保存的会话在后续抓取中**真的带上**(靶站回显 Cookie);
6. 取消流程能真正结束进程;
7. 并发保护: 已有流程时再次启动被拒绝;
8. 删除会话后摘要回到"无会话"。

用法: python scripts/verify_login_flow.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class TargetSite:
    """本地靶站: 一个"需要登录"的页面。

    ``/`` 未登录时给登录页(带密码框与登录引导文案), 带上 ``demo_token`` Cookie 后
    给已登录页(头像 + 我的收藏 + 退出登录)。``/set-cookie`` 是给测试用的"完成登录"
    入口 —— 相当于用户在浏览器里真的登录了。
    """

    LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>请先登录 - 靶站</title></head><body>
<h1>请先登录</h1><p>登录后才能查看内容</p>
<form action="/auth/login" method="post">
  <input type="text" name="u"><input type="password" name="p">
  <button type="submit">登录</button></form>
<a href="/register">注册账号</a></body></html>"""

    AUTHED_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>我的主页 - 靶站</title></head><body>
<header><a href="/users/1" class="user-menu"><img class="avatar" src="/a.png"></a>
<a href="/bookmarks">我的收藏</a><a href="/settings">账号设置</a>
<button>退出登录</button></header>
<main><div class="list"><article>条目 A</article><article>条目 B</article><article>条目 C</article></div></main>
</body></html>"""

    def __init__(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body, status = b"User-agent: *\nDisallow:\n", 200
                elif path == "/set-cookie":
                    # 相当于"用户在浏览器里完成了登录"
                    body = b"<html><body>cookie set</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header(
                        "Set-Cookie", "demo_token=LOGGED-IN-OK; Path=/; Max-Age=86400"
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif "demo_token=LOGGED-IN-OK" in (self.headers.get("Cookie") or ""):
                    body, status = outer.AUTHED_HTML.encode("utf-8"), 200
                else:
                    body, status = outer.LOGIN_HTML.encode("utf-8"), 200
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # noqa: A002
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = TargetSite()
    session_file = Path("data/session.json")
    backup = session_file.read_text(encoding="utf-8") if session_file.exists() else None

    print(f"靶站: {site.base}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
            # ==============================================================
            print("=== 1) 清空会话, 确认初始状态 ===")
            # 先复位登录流程状态: 上一个测试脚本(如 verify_login_commit.py)跑完后
            # 进程内的 started_at 仍在, `flow` 字段会残留一条旧状态。这是**测试隔离**
            # 问题而非产品问题 —— 真实使用中用户不会在同一秒里连跑两个登录流程。
            await client.post("/api/session/login/reset")
            await client.delete("/api/session")
            r = await client.get("/api/session")
            data = r.json()
            check(data["session"]["exists"] is False, "初始状态报告为『无会话』")
            check(data["flow"] is None, "初始没有进行中的登录流程")

            # ==============================================================
            print("\n=== 2) 启动手动登录 ===")
            # 辅助进程会打开一个**可见**浏览器指向靶站。
            # pre_auth_url 让它在打开靶站前先访问 /set-cookie —— 相当于"用户在窗口里
            # 完成了登录"。这样整条链路(可见窗口 → 探测 → 确认 → 保存)都是真的在跑,
            # 只是把"人操作"这一步换成了靶站自己的登录动作。
            r = await client.post(
                "/api/session/login",
                json={"url": f"{site.base}/", "pre_auth_url": f"{site.base}/set-cookie"},
            )
            check(r.status_code == 200, "启动登录流程成功", str(r.status_code))
            payload = r.json()
            check(payload.get("ok") is True, "返回 ok=True")
            check(bool(payload.get("session_path")), "告知了会话文件路径")

            # 等辅助进程起来、打开窗口、并在浏览器里探测到已登录
            status = {}
            deadline = time.time() + 120
            detected = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                status = (await client.get("/api/session/login/status")).json()
                if status.get("state") == "error":
                    break
                if status.get("login_state") == "logged_in":
                    detected = True
                    break
            check(status.get("alive") is True or detected, "辅助进程已启动", str(status.get("state")))
            check(
                detected,
                "辅助窗口里探测到『已登录』(login_state=logged_in)",
                f"state={status.get('login_state')} err={status.get('error')}",
            )

            # ==============================================================
            print("\n=== 3) 并发保护 ===")
            r = await client.post("/api/session/login", json={"url": f"{site.base}/"})
            check(r.status_code == 409, "已有流程时再次启动被拒绝(409)", str(r.status_code))

            # ==============================================================
            print("\n=== 4) 确认保存会话 ===")
            # 用户在界面上点"我已登录, 保存会话" —— 辅助进程随即写出 state.json 并退出
            r = await client.post("/api/session/login/confirm")
            check(r.status_code == 200, "确认保存接口可用")
            check(r.json().get("ok") is True, "确认请求已被接受")

            # ==============================================================
            print("\n=== 5) 会话文件真的写出 ===")
            deadline = time.time() + 90
            saved = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                status = (await client.get("/api/session/login/status")).json()
                if status.get("state") in ("saved", "error", "cancelled"):
                    saved = status.get("state") == "saved"
                    break
            check(saved, "流程以 saved 结束", str(status.get("state")))

            overview = (await client.get("/api/session")).json()
            check(overview["session"]["exists"] is True, "摘要报告会话已存在")
            check(overview["session"]["cookies"] >= 1, "会话里有 Cookie",
                  str(overview["session"]["cookies"]))
            check("demo_token" in overview["session"]["auth_cookies"],
                  "识别出 demo_token 是会话类 Cookie")
            check(
                "LOGGED-IN-OK" not in json.dumps(overview),
                "**接口响应里没有 Cookie 值**",
            )

            # ==============================================================
            print("\n=== 6) 会话在后续抓取中生效 ===")
            from smartcrawler.config import get_settings
            from smartcrawler.crawler import SmartCrawler

            # 模拟用户在「系统配置」里把会话持久化文件指到已保存的会话。
            # 界面上的登录入口就是保存到这个路径的, 所以这里等价于"用户配好了就用得上"。
            os.environ["SC_BROWSER__STORAGE_STATE"] = str(session_file)
            settings = get_settings()
            settings.anti_spider.respect_robots = False
            crawler = SmartCrawler(settings)
            try:
                await crawler.start()
                check(crawler.browser.session_restored, "爬虫启动时报告恢复了会话")
                page = await crawler.browser.new_page()
                await crawler.browser.goto(page, f"{site.base}/")
                title = await page.title()
                check(
                    "我的主页" in (title or ""),
                    "带着会话访问时拿到的是**已登录**页面",
                    title or "(无标题)",
                )
                # 顺带确认登录状态识别在真实链路上也认得出
                from smartcrawler.login_state import detect_login_state

                st = await detect_login_state(page, session_restored=True)
                check(st.state == "logged_in", "登录状态识别为 logged_in", st.state)
                await crawler.browser.close_page(page)
            finally:
                await crawler.close()

            # ==============================================================
            print("\n=== 7) 取消流程 ===")
            r = await client.post("/api/session/login", json={"url": f"{site.base}/"})
            if r.status_code == 200:
                await asyncio.sleep(5)
                r = await client.post("/api/session/login/cancel")
                check(r.status_code == 200, "取消接口可用")
                await asyncio.sleep(4)
                status = (await client.get("/api/session/login/status")).json()
                check(status.get("alive") is not True, "取消后辅助进程不再存活")
            else:
                check(False, "取消流程前的启动失败", str(r.status_code))

            await client.post("/api/session/login/reset")

            # ==============================================================
            print("\n=== 8) 删除会话 ===")
            r = await client.delete("/api/session")
            check(r.status_code == 200, "删除接口可用")
            overview = (await client.get("/api/session")).json()
            check(overview["session"]["exists"] is False, "删除后摘要回到『无会话』")

    finally:
        site.stop()
        # 恢复原会话文件, 不给用户留下"登录态没了"的意外
        if backup is not None:
            session_file.write_text(backup, encoding="utf-8")
            print("\n  (已恢复原有的 data/session.json)")

    print("\n" + "=" * 66)
    if failures:
        print(f"手动登录流程验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"手动登录流程验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
