"""
回归验收: "先匿名分析 → 登录 → 用登录态重新分析" 必须拿到**登录后的页面**。

这是用户报障的场景, 也是既有测试**没覆盖**的一个盲区: 其他会话测试都是"一开始就有
会话", 而真实使用是"匿名分析过一次之后再登录"。区别在于: 分析用的爬虫实例是**匿名时
就已经启动**的那个 —— 只有被正确重建, 新会话才会生效。

曾经的 bug: 前端每 1.5 秒轮询 `login_status`, 会话往往在**轮询里**就被收集了
(`_login_collected=True`); 随后 `confirm` 走的是另一条分支, 只回报"已保存"却**没有
重置浏览器**。于是文件里明明有新会话, 重新分析用的却还是登录前的匿名实例。

用法: python scripts/verify_login_reanalyze.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
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


# 复刻 pixiv: 匿名给"登录引导页", 登录后给**结构完全不同**的内容页
ANON_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>插画社区 - 请登录</title></head><body>
<h1>用示例账号登录</h1>
<a href="/login">登录</a><a href="/register">注册账号</a>
<form action="/auth/login" method="post"><input name="u"><input type="password" name="p"></form>
</body></html>"""

AUTHED_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>我的主页 - 插画社区</title></head><body>
<header><a href="/users/1"><img class="avatar" src="/a.png"></a>
<a href="/bookmarks">我的收藏</a><button>退出登录</button></header>
<main><ul class="works">
<li class="work"><a href="/w/1"><h3>作品 一号</h3></a><span class="author">作者甲</span></li>
<li class="work"><a href="/w/2"><h3>作品 二号</h3></a><span class="author">作者乙</span></li>
<li class="work"><a href="/w/3"><h3>作品 三号</h3></a><span class="author">作者丙</span></li>
<li class="work"><a href="/w/4"><h3>作品 四号</h3></a><span class="author">作者丁</span></li>
</ul></main></body></html>"""


class Site:
    """带头像/个人页入口的"登录后"页面, 与匿名页结构完全不同。"""

    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body = b"User-agent: *\nDisallow:\n"
                elif path == "/do-login":
                    body = b"<html><body>ok</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "auth_token=SECRET; Path=/; Max-Age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    authed = "auth_token=SECRET" in (self.headers.get("Cookie") or "")
                    body = (AUTHED_HTML if authed else ANON_HTML).encode("utf-8")
                self.send_response(200)
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


async def analyze(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.post("/api/analyze", json={"url": url})
    if r.status_code not in (200, 202):
        print(f"    启动分析失败 HTTP {r.status_code}")
        return None
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(60):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return (d.get("result") or {}).get("report")
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site()
    session_file = Path("data/session.json")
    backup = session_file.read_text(encoding="utf-8") if session_file.exists() else None
    # **必须从"无会话"开始** —— 这正是用户的起点, 也是这个 bug 的必要条件
    session_file.unlink(missing_ok=True)
    print(f"靶站: {site.base}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
            await client.post("/api/session/login/reset")

            # ==============================================================
            print("=== 1) 匿名分析(第一次, 用户看到未登录页面) ===")
            r1 = await analyze(client, f"{site.base}/")
            check(r1 is not None, "匿名分析完成")
            if r1:
                ls = r1.get("login_state") or {}
                print(f"    title={r1.get('title')!r} login={ls.get('state')} "
                      f"session_restored={ls.get('session_restored')}")
                check(r1.get("title") == "插画社区 - 请登录", "拿到的是匿名页",
                      str(r1.get("title")))
                check(ls.get("state") == "anonymous", "判定为未登录", str(ls.get("state")))
                check(ls.get("session_restored") is False, "此时没有会话可用")

            # ==============================================================
            print("\n=== 2) 登录并保存会话 ===")
            r = await client.post(
                "/api/session/login",
                json={"url": f"{site.base}/", "pre_auth_url": f"{site.base}/do-login"},
            )
            check(r.status_code == 200, "登录流程启动", str(r.status_code))

            deadline = time.time() + 120
            detected = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                st = (await client.get("/api/session/login/status")).json()
                if st.get("login_state") == "logged_in":
                    detected = True
                    break
                if st.get("state") == "error":
                    break
            check(detected, "辅助窗口里探测到已登录", str(st.get("login_state")))

            confirmed = (await client.post("/api/session/login/confirm")).json()
            check(confirmed.get("session_saved") is True, "会话已保存",
                  str(confirmed.get("message")))
            check(session_file.exists(), "会话文件确实写出")
            if session_file.exists():
                names = [c.get("name") for c in json.loads(
                    session_file.read_text(encoding="utf-8")).get("cookies") or []]
                check("auth_token" in names, "会话文件含登录 Cookie", str(names))

            # ==============================================================
            print("\n=== 3) 用登录态重新分析(用户报障的那一步) ===")
            r2 = await analyze(client, f"{site.base}/")
            check(r2 is not None, "重新分析完成")
            if r2:
                ls2 = r2.get("login_state") or {}
                print(f"    title={r2.get('title')!r} login={ls2.get('state')} "
                      f"session_restored={ls2.get('session_restored')}")
                check(ls2.get("session_restored") is True,
                      "**这次分析确实带上了会话**", str(ls2.get("session_restored")))
                check(r2.get("title") == "我的主页 - 插画社区",
                      "**拿到的是登录后的页面**", str(r2.get("title")))
                check(ls2.get("state") == "logged_in",
                      "判定为已登录", str(ls2.get("state")))
                # 登录后页面的结构与匿名页完全不同 —— 这条断言才是"真的换了页面"
                check(len(r2.get("candidate_lists") or []) >= 1,
                      "识别出登录后页面的列表",
                      f"{len(r2.get('candidate_lists') or [])} 个")
                check("作品 一号" in (r2.get("simplified_tree") or ""),
                      "内容里能看到登录后才有作品条目")

            # ==============================================================
            print("\n=== 4) 再分析一次(确认会话持续有效, 不是偶然) ===")
            r3 = await analyze(client, f"{site.base}/")
            if r3:
                check(r3.get("title") == "我的主页 - 插画社区",
                      "第二次重新分析仍是登录后页面", str(r3.get("title")))

            # ==============================================================
            print("\n=== 5) 删除会话后应回到匿名 ===")
            await client.delete("/api/session")
            r4 = await analyze(client, f"{site.base}/")
            if r4:
                ls4 = r4.get("login_state") or {}
                print(f"    title={r4.get('title')!r} session_restored={ls4.get('session_restored')}")
                check(r4.get("title") == "插画社区 - 请登录",
                      "删除会话后回到匿名页", str(r4.get("title")))
                check(ls4.get("session_restored") is False, "不再带上会话")

    finally:
        site.stop()
        if backup is not None:
            session_file.write_text(backup, encoding="utf-8")
        else:
            session_file.unlink(missing_ok=True)

    print("\n" + "=" * 66)
    if failures:
        print(f"登录态重新分析: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"登录态重新分析: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
