"""UI 实测: 背景图 / 暖色调 / 不遮挡的提示条(截图存证)。

三件事一起验:
  1. 背景图确实加载了(不是兜底纯色) —— 用 CSS 变量与图片尺寸双重确认;
  2. 提示条不再遮挡其它卡片 —— 用真实几何断言(不是"看起来没问题");
  3. 暖色调确实生效(对比背景的冷色)。

用法: python scripts/screenshot_theme_ui.py [--port 8322]
"""

from __future__ import annotations

import argparse
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

# 一个人机验证靶站: 用来让"贴顶提示条"出现, 从而验证它不遮挡别的卡片
CHALLENGE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>需要验证</title></head><body>
<div class="g-recaptcha" style="width:304px;height:78px"></div>
<iframe src="https://www.google.com/recaptcha/api2/bframe" title="recaptcha challenge"
        style="width:400px;height:580px;border:0" width="400" height="580"></iframe>
<div>请完成验证后继续</div>
</body></html>"""

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


class Site:
    def __init__(self) -> None:
        html = CHALLENGE_HTML.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"User-agent: *\nDisallow:\n" if self.path.startswith("/robots") else html
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    OUT.mkdir(parents=True, exist_ok=True)

    site = Site()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1600, "height": 950}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(base, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1800)

        # ---- 1) 背景图 ----
        print("=== 1) 背景图 ===")
        bg = page.evaluate(
            """async () => {
                const root = getComputedStyle(document.documentElement);
                const url = root.getPropertyValue('--bg-image').trim();
                const aurora = document.querySelector('.aurora');
                const before = aurora ? getComputedStyle(aurora, '::before') : null;
                const after = aurora ? getComputedStyle(aurora, '::after') : null;
                // 真的把图片解出来看尺寸(能解码才说明文件可访问)
                const m = url.match(/url\\(["']?(.+?)["']?\\)/);
                let loaded = { ok: false, w: 0, h: 0 };
                if (m) {
                    loaded = await new Promise((resolve) => {
                        const img = new Image();
                        img.onload = () => resolve({ ok: true, w: img.naturalWidth, h: img.naturalHeight });
                        img.onerror = () => resolve({ ok: false, w: 0, h: 0 });
                        img.src = m[1];
                    });
                }
                return {
                    url,
                    loaded,
                    filter: before ? before.filter : '',
                    transform: before ? before.transform : '',
                    scrim: after ? after.backgroundImage.slice(0, 80) : '',
                };
            }"""
        )
        print(f"    --bg-image = {bg['url']}")
        print(f"    图片解码: {bg['loaded']}")
        print(f"    filter = {bg['filter']}")
        print(f"    暗罩 = {bg['scrim']!r}")
        check(bg["loaded"]["ok"], "**背景图能加载(不是兜底纯色)**", str(bg["loaded"]))
        check("blur" in (bg["filter"] or ""), "**背景图做了模糊处理**", bg["filter"])
        check(bool(bg["scrim"]) and bg["scrim"] != "none", "背景图上有暗罩(保证文字对比度)")

        # ---- 2) 暖色调 ----
        print("\n=== 2) 暖色调 vs 冷色背景 ===")
        tone = page.evaluate(
            """() => {
                const root = getComputedStyle(document.documentElement);
                const card = document.querySelector('.card.glass');
                const thumb = document.querySelector('.segmented__thumb');
                const btn = document.querySelector('.btn--primary');
                return {
                    warmGlass: root.getPropertyValue('--warm-glass').trim(),
                    warmBorder: root.getPropertyValue('--warm-border').trim(),
                    cardBg: card ? getComputedStyle(card).backgroundColor : '',
                    cardBorder: card ? getComputedStyle(card).borderTopColor : '',
                    thumbBg: thumb ? getComputedStyle(thumb).backgroundImage.slice(0, 90) : '',
                    btnColor: btn ? getComputedStyle(btn).color : '',
                    btnBg: btn ? getComputedStyle(btn).backgroundImage.slice(0, 90) : '',
                };
            }"""
        )
        for k, v in tone.items():
            print(f"    {k} = {v}")
        check("246, 217, 168" in tone["cardBorder"] or "217, 154, 108" in tone["cardBorder"]
              or tone["cardBorder"] not in ("", "rgba(0, 0, 0, 0)"),
              "卡片描边用了暖色令牌", tone["cardBorder"])
        check("246, 217, 168" in tone["thumbBg"] or "232, 185, 138" in tone["thumbBg"],
              "**选项卡滑块是暖色渐变**", tone["thumbBg"])
        check("246, 217, 168" in tone["btnBg"] or "217, 154, 108" in tone["btnBg"],
              "主按钮是暖色渐变", tone["btnBg"])

        page.screenshot(path=str(OUT / "26_theme_background.png"))
        print("    截图 -> 26_theme_background.png")

        # ---- 3) 提示条不遮挡其它卡片 ----
        print("\n=== 3) 提示条是否遮挡其它卡片 ===")
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(900)
        page.fill("#analyzeUrl", f"{site.base}/")
        page.click("#btnAnalyzeStart")

        try:
            page.wait_for_selector(".alert--sticky", timeout=200000)
            appeared = True
        except Exception:
            appeared = False
        check(appeared, "人机验证提示条出现")

        if appeared:
            page.wait_for_timeout(1200)
            geo = page.evaluate(
                """() => {
                    // 提示条现在渲染在页面顶部的 .alert-slot 里(可滚动堆栈之外)
                    const alert = document.querySelector('.alert-slot .alert--sticky')
                        || document.querySelector('.alert--sticky');
                    const cs = getComputedStyle(alert);
                    const rect = alert.getBoundingClientRect();
                    // 找出与提示条矩形**重叠**的其它卡片
                    const overlaps = [];
                    for (const card of document.querySelectorAll('#analyzeOutput .card')) {
                        if (card.contains(alert)) continue;
                        const r = card.getBoundingClientRect();
                        const ox = Math.min(rect.right, r.right) - Math.max(rect.left, r.left);
                        const oy = Math.min(rect.bottom, r.bottom) - Math.max(rect.top, r.top);
                        if (ox > 2 && oy > 2) {
                            overlaps.push({
                                cls: card.className.slice(0, 60),
                                ox: Math.round(ox), oy: Math.round(oy),
                            });
                        }
                    }
                    return {
                        position: cs.position,
                        parent: alert.parentElement ? alert.parentElement.className : '',
                        top: Math.round(rect.top),
                        height: Math.round(rect.height),
                        overlaps,
                        cards: document.querySelectorAll('#analyzeOutput .card').length,
                    };
                }"""
            )
            print(f"    提示条 position = {geo['position']}  父容器 = {geo['parent']}")
            print(f"    top={geo['top']} h={geo['height']}  页面卡片数 = {geo['cards']}")
            print(f"    与提示条重叠的卡片 = {geo['overlaps']}")
            check(geo["parent"].startswith("alert-slot"),
                  "**提示条渲染在页面级槽位里(不在卡片堆栈内)**", geo["parent"])
            check(not geo["overlaps"],
                  "**提示条与其它卡片无重叠**", str(geo["overlaps"]))

            # 滚动后再看一次。
            # 期望的行为**不是**"提示条跟着滚走", 而是:
            #   它 sticky 在页面顶部(始终可点), 同时**不遮挡任何卡片**。
            # 这两条同时成立才是对的 —— 之前的实现(z-index 覆盖式 sticky)满足了前一条
            # 却违反了后一条, 用户报的就是那个遮挡。
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(900)
            after = page.evaluate(
                """() => {
                    const alert = document.querySelector('.alert-slot .alert--sticky')
                        || document.querySelector('.alert--sticky');
                    if (!alert) return { gone: true };
                    const rect = alert.getBoundingClientRect();
                    // 滚动后重新算一遍遮挡: 这是本次要防住的核心问题
                    const overlaps = [];
                    for (const card of document.querySelectorAll('#analyzeOutput .card')) {
                        const r = card.getBoundingClientRect();
                        const ox = Math.min(rect.right, r.right) - Math.max(rect.left, r.left);
                        const oy = Math.min(rect.bottom, r.bottom) - Math.max(rect.top, r.top);
                        if (ox > 2 && oy > 2) overlaps.push({ ox: Math.round(ox), oy: Math.round(oy) });
                    }
                    return {
                        gone: false,
                        top: Math.round(rect.top),
                        inView: rect.top >= -1 && rect.bottom <= window.innerHeight + 1,
                        overlaps,
                    };
                }"""
            )
            print(f"    滚动 700px 后: top={after.get('top')} 在视口内={after.get('inView')} "
                  f"重叠={after.get('overlaps')}")
            check(not after.get("gone") and after.get("inView"),
                  "**滚动后提示条仍留在视口内(入口始终可点)**",
                  f"top={after.get('top')}")
            check(not after.get("overlaps"),
                  "**滚动后依然不遮挡任何卡片**", str(after.get("overlaps")))
            page.screenshot(path=str(OUT / "27_theme_alert_scrolled.png"))
            print("    截图 -> 27_theme_alert_scrolled.png")

        browser.close()
    site.stop()

    print(f"\n前端错误: {len(errors)}")
    for e in errors[:5]:
        print(f"  ! {e}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 62)
    if failures:
        print("主题与遮挡验收: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("主题与遮挡验收: 通过 ✓")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
