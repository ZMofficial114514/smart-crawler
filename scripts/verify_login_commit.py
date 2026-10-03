"""
复现并验收"保存会话后没有下一步 / 会话不生效"这个 bug。

**用户实际遇到的两个问题**(本脚本把它们固化成断言):

1. 在可见窗口登录后点「我已登录, 保存会话」, 界面提示成功, **但会话文件其实没写出来** ——
   因为收集逻辑挂在"辅助进程已退出"上, 而辅助进程写完数据后还要停留两秒才退出, 前端
   一看到"已保存"就停止轮询了。用户随后重新分析发现会话不生效。
2. 保存完会话**没有下一步** —— 弹层里没有"用登录后的身份继续"的入口。

本脚本验证:
- 确认保存时接口**等到会话文件真的落盘**才返回, 且 `session_saved` 可信;
- 返回后立刻检查 `data/session.json` 确实存在且含 Cookie;
- 新会话**立刻**被爬虫采用(不需要重启服务);
- 会话生效可用接口核实(`/api/session/verify`) —— 这就是新增的"下一步"能力;
- 未登录状态下核实接口会如实回报"未生效", 而不是假装成功。

用法: python scripts/verify_login_commit.py [--port 8322]
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


class TargetSite:
    """需要登录的靶站: 带 demo_token 才是已登录视图。"""

    LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>请先登录 - 靶站</title></head><body><h1>请先登录</h1>
<p>登录后才能查看内容</p>
<form action="/auth/login" method="post"><input name="u"><input type="password" name="p"></form>
</body></html>"""

    AUTHED_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>我的主页 - 靶站</title></head><body>
<header><a href="/users/1" class="user-menu"><img class="avatar" src="/a.png"></a>
<a href="/bookmarks">我的收藏</a><button>退出登录</button></header>
<main><ul class="items">
<li class="item"><a href="/items/1"><h3>条目 A</h3></a><span class="price">¥10.00</span></li>
<li class="item"><a href="/items/2"><h3>条目 B</h3></a><span class="price">¥20.00</span></li>
<li class="item"><a href="/items/3"><h3>条目 C</h3></a><span class="price">¥30.00</span></li>
<li class="item"><a href="/items/4"><h3>条目 D</h3></a><span class="price">¥40.00</span></li>
</ul></main></body></html>"""

    def __init__(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body, status = b"User-agent: *\nDisallow:\n", 200
                elif path == "/set-cookie":
                    body = b"<html><body>ok</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "demo_token=COMMIT-OK; Path=/; Max-Age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif "demo_token=COMMIT-OK" in (self.headers.get("Cookie") or ""):
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
        async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
            # ==============================================================
            print("=== 1) 先清空会话 ===")
            await client.delete("/api/session")
            data = (await client.get("/api/session")).json()
            check(data["session"]["exists"] is False, "初始无会话")

            # ==============================================================
            print("\n=== 2) 启动登录并完成它 ===")
            r = await client.post(
                "/api/session/login",
                json={"url": f"{site.base}/", "pre_auth_url": f"{site.base}/set-cookie"},
            )
            check(r.status_code == 200, "登录流程已启动", str(r.status_code))

            deadline = time.time() + 120
            detected = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                status = (await client.get("/api/session/login/status")).json()
                if status.get("login_state") == "logged_in":
                    detected = True
                    break
                if status.get("state") == "error":
                    break
            check(detected, "辅助窗口里已探测到登录", str(status.get("login_state")))

            # ==============================================================
            print("\n=== 3) 核心回归: 确认保存必须等到文件真的落盘 ===")
            # 这一条就是用户遇到的 bug: 以前接口立刻返回"已请求保存会话", 前端随即停止
            # 轮询, 而辅助进程还在写数据 —— 会话文件永远写不出来。
            t0 = time.time()
            r = await client.post("/api/session/login/confirm")
            elapsed = time.time() - t0
            payload = r.json()
            print(f"  confirm 耗时 {elapsed:.1f}s -> ok={payload.get('ok')} "
                  f"session_saved={payload.get('session_saved')}")

            check(payload.get("session_saved") is True, "接口报告会话已保存",
                  str(payload.get("message")))
            # 返回的那一刻文件就必须已经存在 —— 不做任何等待
            check(session_file.exists(), "**返回时会话文件已经存在**(无需再等)")
            if session_file.exists():
                stored = json.loads(session_file.read_text(encoding="utf-8"))
                names = [c.get("name") for c in stored.get("cookies") or []]
                check("demo_token" in names, "会话文件里有 demo_token", str(names))

            # ==============================================================
            print("\n=== 4) 新会话立刻被采用(无需重启服务) ===")
            r = await client.post("/api/session/verify", json={"url": f"{site.base}/"})
            check(r.status_code == 200, "核实接口可用", str(r.status_code))
            verified = r.json()
            print(f"  判定: {verified.get('login_state')} "
                  f"(带上会话={verified.get('session_restored')}) 标题={verified.get('page_title')!r}")
            check(verified.get("session_restored") is True, "本次访问确实带上了会话")
            check(verified.get("logged_in") is True, "**用新会话访问被识别为已登录**",
                  str(verified.get("summary")))
            check(verified.get("page_title") == "我的主页 - 靶站",
                  "拿到的是已登录视图的标题", str(verified.get("page_title")))

            # ==============================================================
            print("\n=== 5) 保存后能确实继续(这就是之前缺的下一步) ===")
            # 会话生效后, 结构分析与抓取都该拿到登录后的内容
            r = await client.post("/api/analyze", json={"url": f"{site.base}/"})
            check(r.status_code in (200, 202), "发起结构分析", str(r.status_code))
            task_id = (r.json().get("task") or {}).get("id")

            report = None
            deadline = time.time() + 120
            while time.time() < deadline and task_id:
                await asyncio.sleep(2)
                detail = (await client.get(f"/api/tasks/{task_id}")).json()
                if detail.get("status") in ("success", "failed", "cancelled"):
                    report = (detail.get("result") or {}).get("report")
                    break
            check(report is not None, "分析任务完成并返回报告")
            if report:
                ls = report.get("login_state") or {}
                print(f"  报告里的登录状态: {ls.get('state')} "
                      f"(session_restored={ls.get('session_restored')})")
                check(ls.get("state") == "logged_in",
                      "**分析报告里也识别为已登录**", str(ls.get("state")))
                check(ls.get("session_restored") is True, "分析时带上了会话")
                # 登录后的页面才有列表结构; 匿名引导页是没有的
                check(len(report.get("candidate_lists") or []) >= 1,
                      "拿到了登录后页面的候选列表",
                      f"{len(report.get('candidate_lists') or [])} 个")

            # ==============================================================
            print("\n=== 6) 会话未生效时如实回报(不假装成功) ===")
            await client.delete("/api/session")
            r = await client.post("/api/session/verify", json={"url": f"{site.base}/"})
            verified2 = r.json()
            print(f"  判定: {verified2.get('login_state')} 带上会话={verified2.get('session_restored')}")
            check(verified2.get("session_restored") is False,
                  "删除会话后不再带上会话")
            check(verified2.get("logged_in") is False,
                  "**如实回报未登录**, 而不是假装成功", str(verified2.get("summary")))

    finally:
        site.stop()
        if backup is not None:
            session_file.write_text(backup, encoding="utf-8")
            print("\n  (已恢复原有的 data/session.json)")

    print("\n" + "=" * 66)
    if failures:
        print(f"登录提交验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"登录提交验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
