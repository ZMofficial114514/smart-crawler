"""
人机验证识别与"手动过验证"的验收。

**核心难点是"别误报"**: pixiv 每次访问都会挂上 reCAPTCHA 的 anchor iframe, 但只在部分
情况才真的弹出挑战(实测: 未挑战时该 iframe 是 ``visibility: hidden``)。如果按"页面上有
captcha"判定, 就会变成每次访问都提示"需要人机验证" —— 比不提示更糟。

因此本脚本重点验证两个方向:
1. **真实站点不得误报**(pixiv 登录页/首页、GitHub、books.toscrape);
2. **真的挑战必须命中**(合成 reCAPTCHA bframe / Turnstile 容器 / 滑块页)。

另外验证"手动过验证"的完整链路(靶站在用户访问某个 URL 后放行)。

用法: python scripts/verify_challenge.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from smartcrawler.challenge import detect_challenge, evaluate_challenge  # noqa: E402

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# 合成样本
# ---------------------------------------------------------------------------
RECAPTCHA_CHALLENGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>安全验证</title></head><body>
<h1>请完成验证</h1><p>Verify you are human</p>
<iframe src="https://www.recaptcha.net/recaptcha/enterprise/bframe?k=abc"
        style="width:400px;height:580px;border:0" title="reCAPTCHA"></iframe>
</body></html>"""

RECAPTCHA_ANCHOR_ONLY = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>登录</title></head><body>
<h1>登录</h1><p>This site is protected by reCAPTCHA.</p>
<iframe src="https://www.recaptcha.net/recaptcha/enterprise/anchor?k=abc"
        style="width:256px;height:60px;visibility:hidden"></iframe>
</body></html>"""

TURNSTILE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Just a moment...</title></head><body>
<div class="cf-turnstile" data-sitekey="0x4AAA" style="width:300px;height:65px"></div>
<p>Checking your browser before accessing</p>
</body></html>"""

GEETEST = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>滑动验证</title></head><body>
<div id="geetest-box" class="geetest_panel" style="width:320px;height:200px"></div>
<p>请拖动滑块完成拼图</p>
</body></html>"""

NORMAL_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>商品列表</title></head><body>
<h1>全部商品</h1>
<ul class="list">
<li class="item"><a href="/p/1"><h3>商品 A</h3></a><span class="price">¥10.00</span></li>
<li class="item"><a href="/p/2"><h3>商品 B</h3></a><span class="price">¥20.00</span></li>
<li class="item"><a href="/p/3"><h3>商品 C</h3></a><span class="price">¥30.00</span></li>
</ul></body></html>"""


class ChallengeSite:
    """靶站: 访问 /challenge 时给出挑战页, 访问 /clear 相当于"用户过了验证"。"""

    def __init__(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body, status = b"User-agent: *\nDisallow:\n", 200
                elif path == "/clear":
                    # 相当于用户在可见窗口里完成了验证: 下发通行 Cookie
                    body = b"<html><body>cleared</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "cf_clearance=PASSED-OK; Path=/; Max-Age=86400")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                elif "cf_clearance=PASSED-OK" in (self.headers.get("Cookie") or ""):
                    body, status = NORMAL_PAGE.encode("utf-8"), 200
                else:
                    body, status = TURNSTILE.encode("utf-8"), 200
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
        # ==============================================================
        print("=== 1) 纯函数判定 ===")
        cases = [
            ("可见 bframe(挑战中)",
             {"challenge_frames": [{"src": "https://www.recaptcha.net/recaptcha/enterprise/bframe?k=1", "width": 400, "height": 580}]},
             True),
            ("只有 hidden anchor(未挑战)",
             {"challenge_frames": [], "explicit_containers": [], "text_hit": False}, False),
            ("仅文案提示(不判)",
             {"challenge_frames": [], "explicit_containers": [], "text_hit": True}, False),
            ("显式容器",
             {"explicit_containers": [{"tag": "div", "cls": "cf-turnstile", "width": 300, "height": 65}]}, True),
        ]
        for label, raw, expected in cases:
            result = evaluate_challenge(raw)
            check(result.detected == expected, label, f"detected={result.detected} 期望 {expected}")

        check(
            evaluate_challenge(
                {"challenge_frames": [{"src": "https://www.recaptcha.net/recaptcha/enterprise/bframe?k=1"}]}
            ).kind == "Google reCAPTCHA",
            "能识别出 reCAPTCHA 厂商",
        )
        check(
            evaluate_challenge(
                {"explicit_containers": [{"tag": "div", "cls": "cf-turnstile"}]}
            ).kind == "Cloudflare Turnstile",
            "能识别出 Turnstile 厂商",
        )

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()

            # ==============================================================
            print("\n=== 2) 合成页面(真实渲染) ===")
            for label, html, expected in (
                ("reCAPTCHA 挑战", RECAPTCHA_CHALLENGE, True),
                ("只有 anchor(不判)", RECAPTCHA_ANCHOR_ONLY, False),
                ("Turnstile 挑战", TURNSTILE, True),
                ("极验滑块", GEETEST, True),
                ("普通列表页", NORMAL_PAGE, False),
            ):
                await page.set_content(html)
                await asyncio.sleep(0.5)
                ch = await detect_challenge(page)
                check(ch.detected == expected, label, f"detected={ch.detected} 期望 {expected}")

            # ==============================================================
            print("\n=== 3) 真实站点(不得误报) ===")
            for label, url in (
                ("pixiv 登录页", "https://accounts.pixiv.net/login"),
                ("pixiv 首页", "https://www.pixiv.net/"),
                ("GitHub 首页", "https://github.com/"),
                ("books.toscrape", "https://books.toscrape.com/"),
            ):
                target = await browser.new_page()
                try:
                    await target.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(4)
                    ch = await detect_challenge(target)
                    check(not ch.detected, f"{label} 未误报",
                          f"detected={ch.detected} conf={ch.confidence:.2f}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! {label} 跳过: {type(exc).__name__}")

            # ==============================================================
            print("\n=== 4) 靶站上的挑战链路 ===")
            probe = await browser.new_page()
            await probe.goto(f"{site.base}/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.8)
            ch = await detect_challenge(probe)
            check(ch.detected, "靶站确实给出挑战", f"kind={ch.kind}")
            check("Turnstile" in ch.kind, "识别为 Turnstile", ch.kind)

            # 模拟"用户过了验证"(下发通行 Cookie)后再访问
            await probe.goto(f"{site.base}/clear", wait_until="domcontentloaded", timeout=30000)
            await probe.goto(f"{site.base}/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.8)
            ch2 = await detect_challenge(probe)
            check(not ch2.detected, "过验证后不再判定为挑战", f"detected={ch2.detected}")

            # ==============================================================
            print("\n=== 5) 抓取/分析链路会带上 challenge ===")
            async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
                try:
                    r = await client.post("/api/session/login", json={"url": f"{site.base}/", "mode": "bogus"})
                    check(r.status_code == 422, "非法 mode 被拒绝(422)", str(r.status_code))
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! 服务未运行, 跳过接口检查: {type(exc).__name__}")
                    client_ok = False
                else:
                    client_ok = True

                if client_ok:
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
                        print(f"    报告里的 challenge: detected={chal.get('detected')} "
                              f"kind={chal.get('kind')}")
                        check(chal.get("detected") is True,
                              "**报告里带上了 challenge 判定**(界面据此显示『手动过验证』)",
                              str(chal.get("detected")))

            await browser.close()
    finally:
        site.stop()
        if backup is not None:
            session_file.write_text(backup, encoding="utf-8")
            print("\n  (已恢复原有的 data/session.json)")

    print("\n" + "=" * 66)
    if failures:
        print(f"人机验证验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"人机验证验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
