"""UI 实测: 人机验证提示条与「手动过验证」弹层(截图存证)。

用本地靶站给出一个真实的挑战页, 让分析任务产出 challenge 判定, 从而走真实的
"识别 → 提示 → 弹层" 路径, 而不是只测渲染函数。
"""

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
OUT = Path("data/screenshots")

TURNSTILE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Just a moment...</title></head><body>
<h1>正在验证您的浏览器</h1>
<div class="cf-turnstile" data-sitekey="0x4AAAAAAAB" style="width:300px;height:65px"></div>
<p>Checking your browser before accessing this site.</p>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = b"User-agent: *\nDisallow:\n" if path == "/robots.txt" else TURNSTILE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A002
        pass


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{port}/"
    print(f"挑战靶站: {target}")

    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1680, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(800)
        page.fill("#analyzeUrl", target)
        page.click("#btnAnalyzeStart")

        # 等"手动过验证"按钮出现
        try:
            page.wait_for_selector("button:has-text('手动过验证')", timeout=180000)
            appeared = True
        except Exception:
            appeared = False

        print(f"验证提示条: {'✓ 出现' if appeared else '✗ 未出现'}")
        if not appeared:
            failures.append("分析页没有出现『手动过验证』入口")
            heads = page.eval_on_selector_all(
                "#analyzeOutput h2", "e => e.map(x => x.textContent.trim())"
            )
            print(f"  当前卡片: {heads}")
        else:
            # 验证提示现在渲染在页面顶部的 .alert-slot 里(可滚动报告之外的专属槽位),
            # 这样它既 sticky 常驻可点, 又不会像卡片内 sticky 那样遮住别的卡片。
            text = page.eval_on_selector(
                "#analyzeAlertSlot .alert--warning", "e => e.textContent"
            )
            slot = page.eval_on_selector(
                "#analyzeAlertSlot",
                "e => ({ cls: e.className, pos: getComputedStyle(e).position })",
            )
            print(f"  提示槽位: {slot}")
            if slot.get("pos") != "sticky":
                failures.append(f"提示槽位应为 sticky, 实际 {slot.get('pos')}")
            print(f"  提示文案: {(text or '')[:140]}")
            for needle, label in (
                ("人机验证", "点明了是人机验证"),
                ("只有人能完成", "说明了只有人能完成"),
                ("不会尝试绕过", "声明不尝试绕过"),
            ):
                ok = needle in (text or "")
                print(f"    {'✓' if ok else '✗'} {label}")
                if not ok:
                    failures.append(label)
            shot = OUT / "21_challenge_banner.png"
            page.screenshot(path=str(shot))
            print(f"  截图 -> {shot.name}")

            # 打开弹层
            page.click("button:has-text('手动过验证')")
            page.wait_for_timeout(1500)
            opened = page.eval_on_selector("#modal", "e => e.classList.contains('is-open')")
            print(f"验证弹层: {'✓ 已打开' if opened else '✗ 未打开'}")
            if not opened:
                failures.append("验证弹层未打开")
            else:
                body = page.eval_on_selector("#modal", "e => e.textContent")
                for needle, label in (
                    ("打开验证窗口", "有『打开验证窗口』按钮"),
                    ("验证已完成, 保存会话", "有『验证已完成』按钮"),
                    ("不会读取或代填任何凭据", "声明不读取凭据"),
                ):
                    ok = needle in (body or "")
                    print(f"    {'✓' if ok else '✗'} {label}")
                    if not ok:
                        failures.append(label)
                shot2 = OUT / "22_challenge_modal.png"
                page.screenshot(path=str(shot2))
                print(f"  截图 -> {shot2.name}")
                page.keyboard.press("Escape")

        browser.close()
    server.shutdown()

    print(f"\n前端错误: {len(errors)}")
    for e in errors[:6]:
        print(f"  ! {e}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 60)
    if failures:
        print("人机验证 UI 实测: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("人机验证 UI 实测: 通过 ✓")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
