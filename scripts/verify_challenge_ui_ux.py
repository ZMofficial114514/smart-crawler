"""
三个用户报障的回归验收(人机验证相关)。

1. **"手动过验证没有弹窗"** —— 排查发现两层原因:
   (a) 页面只*提到* captcha 时被误判成"需要验证", 用户点开当然没有验证可过;
   (b) 报告很长时按钮会随页面滚出视口(y 为负), 根本点不到。
   现在: 误判已修; 验证提示条改为**贴顶悬浮**, 任何滚动位置都能点到。

2. **有验证时仍高亮"以登录态重新分析"** —— 挑战页往往也长得像登录页, 登录判定会给
   "匿名", 界面就把登录当成主行动。现在检测到验证时不再引导登录, 只说明"验证挡住了"。

3. **"手动打开浏览器是好的, 自动访问却说未登录"** —— 因为自动访问(无头、无会话)会
   撞上验证, 看到的是验证页; 而用户手动打开时人已经过了验证。现在这种"被验证挡住"的
   状态会被明确区分出来, 不再冒充登录问题。

用法: python scripts/verify_challenge_ui_ux.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# A: 只"提到" reCAPTCHA, 没有真挑战(复刻 pixiv 页脚)
MENTIONS_ONLY = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>插画社区</title></head><body>
<h1>作品列表</h1>
<div class="works">
<article class="work"><a href="/w/1"><h3>作品一号</h3></a><span class="author">作者甲</span></article>
<article class="work"><a href="/w/2"><h3>作品二号</h3></a><span class="author">作者乙</span></article>
<article class="work"><a href="/w/3"><h3>作品三号</h3></a><span class="author">作者丙</span></article>
</div>
<a href="/login">登录</a><a href="/register">注册账号</a>
<iframe src="https://www.recaptcha.net/recaptcha/enterprise/anchor?k=x"
        style="width:256px;height:60px;visibility:hidden"></iframe>
<p>This site is protected by reCAPTCHA.</p>
</body></html>"""

# B: 真的在挑战, 且页面同时长得像登录页
REAL_CHALLENGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>安全验证</title></head><body>
<h1>请完成验证后继续</h1>
<div class="cf-turnstile" data-sitekey="0x4AAAA" style="width:300px;height:65px"></div>
<form action="/login" method="post"><input type="text" name="u"><input type="password" name="p"></form>
<p>Checking your browser before accessing</p>
</body></html>"""


class Site:
    def __init__(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body = b"User-agent: *\nDisallow:\n"
                elif path.startswith("/mentions"):
                    body = MENTIONS_ONLY.encode("utf-8")
                else:
                    body = REAL_CHALLENGE.encode("utf-8")
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
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(60):
        await asyncio.sleep(2)
        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        if detail.get("status") in ("success", "failed", "cancelled"):
            return detail.get("result") or {}
    return None


def browser_checks(base: str, site_base: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1400, "height": 800}, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(base, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        # ==============================================================
        print("\n=== A) 只提到 captcha 的页面: 不应出现『手动过验证』 ===")
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(800)
        page.fill("#analyzeUrl", f"{site_base}/mentions")
        page.click("#btnAnalyzeStart")
        # 等报告出来
        try:
            page.wait_for_selector("#analyzeOutput .card.glass", timeout=180000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

        has_challenge_btn = page.eval_on_selector_all(
            "button", "e => e.some(b => b.textContent.includes('手动过验证'))"
        )
        check(not has_challenge_btn,
              "**没有出现『手动过验证』**(页面上没有真挑战)",
              f"出现了={has_challenge_btn}")
        body = page.eval_on_selector("#analyzeOutput", "e => e.textContent")
        check("Cloudflare" not in (body or "") and "reCAPTCHA(" not in (body or ""),
              "也没有把它标成人机验证卡片")

        # ==============================================================
        print("\n=== B) 真的在挑战: 出现『手动过验证』且不被登录提示盖过 ===")
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(500)
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(800)
        page.fill("#analyzeUrl", f"{site_base}/")
        page.click("#btnAnalyzeStart")
        try:
            page.wait_for_selector("button:has-text('手动过验证')", timeout=180000, state="attached")
            appeared = True
        except Exception:
            appeared = False
        check(appeared, "出现了『手动过验证』入口")
        page.wait_for_timeout(1200)

        if appeared:
            # ---- 2) 不应引导登录 ----
            login_highlight = page.eval_on_selector_all(
                "button", "e => e.some(b => b.textContent.trim() === '登录一次')"
            )
            check(not login_highlight,
                  "**没有出现『登录一次』按钮**(应引导过验证而不是登录)",
                  f"出现了={login_highlight}")
            blocked_note = page.eval_on_selector("#analyzeOutput", "e => e.textContent")
            check("登录状态暂时无法判断" in (blocked_note or ""),
                  "改为说明『登录状态暂时无法判断』(因为被验证挡住)",
                  "" if "登录状态暂时无法判断" in (blocked_note or "") else "未见该说明")

            # ---- 3) 入口在任何滚动位置都能点到 ----
            page.evaluate("document.querySelector('#content').scrollTo(0, 99999)")
            page.wait_for_timeout(500)
            reach = page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('button')]
                        .find(x => x.textContent.includes('手动过验证'));
                    if (!b) return { found: false };
                    const r = b.getBoundingClientRect();
                    const cx = Math.min(Math.max(r.x + r.width / 2, 1), window.innerWidth - 2);
                    const cy = Math.min(Math.max(r.y + r.height / 2, 1), window.innerHeight - 2);
                    const el = document.elementFromPoint(cx, cy);
                    return {
                        found: true,
                        y: Math.round(r.y), vh: window.innerHeight,
                        onScreen: r.y >= 0 && r.bottom <= window.innerHeight,
                        hitIsButton: !!(el && (el === b || b.contains(el))),
                        hitTag: el ? el.tagName : null,
                    };
                }"""
            )
            print(f"  滚到底后按钮: {reach}")
            check(reach.get("onScreen") is True,
                  "**滚到页面底部后按钮仍在视口内**(贴顶悬浮)", f"y={reach.get('y')}")
            check(reach.get("hitIsButton") is True,
                  "该位置的点击确实落在按钮上", str(reach.get("hitTag")))

            # ---- 4) 点击真的能打开弹层(用真实坐标点) ----
            before_errs = len(errors)
            page.mouse.click(
                page.evaluate(
                    """() => {
                        const b = [...document.querySelectorAll('button')]
                            .find(x => x.textContent.includes('手动过验证'));
                        const r = b.getBoundingClientRect();
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    }"""
                )["x"],
                page.evaluate(
                    """() => {
                        const b = [...document.querySelectorAll('button')]
                            .find(x => x.textContent.includes('手动过验证'));
                        const r = b.getBoundingClientRect();
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    }"""
                )["y"],
            )
            page.wait_for_timeout(1600)
            opened = page.evaluate(
                "() => { const m = document.querySelector('#modal'); return !!(m && m.classList.contains('is-open')); }"
            )
            check(opened, "**真实坐标点击后弹层打开了**")
            if opened:
                vis = page.evaluate(
                    """() => {
                        const m = document.querySelector('#modal');
                        const r = m.getBoundingClientRect();
                        return { w: Math.round(r.width), h: Math.round(r.height),
                                 inView: r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0 };
                    }"""
                )
                check(vis.get("inView") is True, "弹层落在可视区域内", str(vis))
                btns = page.eval_on_selector_all("#modal button", "e => e.map(b => b.textContent.trim())")
                check(any("打开验证窗口" in (b or "") for b in btns), "弹层里有『打开验证窗口』")
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)

            check(len(errors) == before_errs, "点击过程中没有前端报错",
                  "; ".join(errors[before_errs:][:3]))

        page.wait_for_timeout(300)
        print(f"\n前端错误总数: {len(errors)}")
        for e in errors[:6]:
            print(f"  ! {e}")
        check(not errors, "全程无前端错误")

        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site()
    print(f"靶站: {site.base}")
    try:
        browser_checks(base, site.base)
    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"人机验证 UX 验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"人机验证 UX 验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
