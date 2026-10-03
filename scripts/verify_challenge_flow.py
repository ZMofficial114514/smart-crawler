"""
"手动过人机验证"完整链路验收。

与 `verify_login_flow.py` 同构, 但走的是 **challenge 模式**: 靶站先给出挑战页(让人过),
过完后下发通行 Cookie。验证:

1. challenge 模式下辅助进程能起来, 状态里带 ``mode=challenge``;
2. 窗口里过了验证(靶站的 /clear 相当于用户手动点完)后, 状态如实反映"挑战已消失";
3. 确认保存后, **通行凭据进了会话文件**;
4. 之后用这份会话访问, 不再遇到挑战(拿到正常页面);
5. 挑战未过时如实回报, 不假装成功。

用法: python scripts/verify_challenge_flow.py [--port 8322]
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


TURNSTILE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Just a moment...</title></head><body>
<h1>正在验证</h1>
<div class="cf-turnstile" data-sitekey="0x4AAA" style="width:300px;height:65px"></div>
<p>Checking your browser before accessing</p>
</body></html>"""

NORMAL_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>内容页</title></head><body>
<h1>正常内容</h1>
<ul class="list">
<li class="item"><a href="/p/1"><h3>条目 A</h3></a><span class="price">¥10.00</span></li>
<li class="item"><a href="/p/2"><h3>条目 B</h3></a><span class="price">¥20.00</span></li>
<li class="item"><a href="/p/3"><h3>条目 C</h3></a><span class="price">¥30.00</span></li>
</ul></body></html>"""


class ChallengeSite:
    def __init__(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body, status = b"User-agent: *\nDisallow:\n", 200
                elif path == "/clear":
                    body = b"<html><body>cleared</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "cf_clearance=CHALLENGE-PASSED; Path=/; Max-Age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif "cf_clearance=CHALLENGE-PASSED" in (self.headers.get("Cookie") or ""):
                    body, status = NORMAL_HTML.encode("utf-8"), 200
                else:
                    body, status = TURNSTILE_HTML.encode("utf-8"), 200
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

    site = ChallengeSite()
    session_file = Path("data/session.json")
    backup = session_file.read_text(encoding="utf-8") if session_file.exists() else None
    print(f"靶站: {site.base}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
            # ==============================================================
            print("=== 1) 清空会话, 复位流程 ===")
            await client.post("/api/session/login/reset")
            await client.delete("/api/session")
            data = (await client.get("/api/session")).json()
            check(data["session"]["exists"] is False, "初始无会话")

            # ==============================================================
            print("\n=== 2) 启动 challenge 模式流程 ===")
            # pre_auth_url 让窗口在打开靶站前先访问 /clear —— 相当于"用户在窗口里
            # 完成了人机验证"。正常使用时这一步由用户亲手完成。
            r = await client.post(
                "/api/session/login",
                json={
                    "url": f"{site.base}/",
                    "mode": "challenge",
                    "pre_auth_url": f"{site.base}/clear",
                },
            )
            check(r.status_code == 200, "challenge 模式流程已启动", str(r.status_code))
            payload = r.json()
            check(payload.get("mode") == "challenge", "返回里标注了 mode=challenge")

            status = {}
            deadline = time.time() + 120
            cleared = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                status = (await client.get("/api/session/login/status")).json()
                if status.get("state") == "error":
                    break
                if status.get("challenge_detected") is False and status.get("state") == "waiting":
                    cleared = True
                    break
            print(f"  状态: state={status.get('state')} mode={status.get('mode')} "
                  f"challenge_detected={status.get('challenge_detected')}")
            check(status.get("mode") == "challenge", "状态文件里带着 mode=challenge")
            check(cleared, "**检测到挑战已消失**(用户可以点保存了)",
                  f"challenge_detected={status.get('challenge_detected')}")
            print(f"  界面提示: {status.get('step')}")

            # ==============================================================
            print("\n=== 3) 确认保存通行凭据 ===")
            t0 = time.time()
            r = await client.post("/api/session/login/confirm")
            result = r.json()
            print(f"  confirm 耗时 {time.time() - t0:.1f}s -> session_saved={result.get('session_saved')}")
            check(result.get("session_saved") is True, "报告会话已保存", str(result.get("message")))
            check(session_file.exists(), "**返回时会话文件已经存在**")
            if session_file.exists():
                stored = json.loads(session_file.read_text(encoding="utf-8"))
                names = [c.get("name") for c in stored.get("cookies") or []]
                check("cf_clearance" in names, "通行凭据(cf_clearance)已进会话文件", str(names))

            # ==============================================================
            print("\n=== 4) 带上通行凭据后不再遇到挑战 ===")
            r = await client.post("/api/analyze", json={"url": f"{site.base}/"})
            check(r.status_code in (200, 202), "发起分析", str(r.status_code))
            task_id = (r.json().get("task") or {}).get("id")

            report = None
            deadline = time.time() + 120
            while time.time() < deadline and task_id:
                await asyncio.sleep(2)
                detail = (await client.get(f"/api/tasks/{task_id}")).json()
                if detail.get("status") in ("success", "failed", "cancelled"):
                    report = (detail.get("result") or {}).get("report")
                    break
            check(report is not None, "分析任务完成")
            if report:
                chal = report.get("challenge") or {}
                print(f"  报告标题: {report.get('title')!r}")
                print(f"  报告里的 challenge: detected={chal.get('detected')}")
                check(report.get("title") == "内容页",
                      "拿到的是**过验证后**的正常页面", str(report.get("title")))
                check(chal.get("detected") is False,
                      "**不再被判定为挑战**", str(chal.get("detected")))
                check(len(report.get("candidate_lists") or []) >= 1,
                      "能识别出内容列表",
                      f"{len(report.get('candidate_lists') or [])} 个")

            # ==============================================================
            print("\n=== 5) 挑战没过时如实回报 ===")
            await client.post("/api/session/login/reset")
            await client.delete("/api/session")
            r = await client.post("/api/analyze", json={"url": f"{site.base}/"})
            task_id2 = (r.json().get("task") or {}).get("id")
            report2 = None
            deadline = time.time() + 120
            while time.time() < deadline and task_id2:
                await asyncio.sleep(2)
                detail = (await client.get(f"/api/tasks/{task_id2}")).json()
                if detail.get("status") in ("success", "failed", "cancelled"):
                    report2 = (detail.get("result") or {}).get("report")
                    break
            if report2:
                chal2 = report2.get("challenge") or {}
                print(f"  无会话时: title={report2.get('title')!r} challenge={chal2.get('detected')}")
                check(chal2.get("detected") is True,
                      "**没有通行凭据时如实报告遇到挑战**", str(chal2.get("detected")))

    finally:
        site.stop()
        if backup is not None:
            session_file.write_text(backup, encoding="utf-8")
            print("\n  (已恢复原有的 data/session.json)")

    print("\n" + "=" * 66)
    if failures:
        print(f"手动过验证链路: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"手动过验证链路: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
