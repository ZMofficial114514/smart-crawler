"""
UI 实测: 用真实洛谷页面渲染访问受限诊断卡并截图。

用途是把"用户在界面上能看到什么"固定成证据 —— 尤其是权限不足这种**不跳登录页**
的场景, 卡片必须直接展示页面上的原文(如"没有权限请求此资源。"), 否则用户无从判断。

用法: python scripts/screenshot_luogu_case.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
TARGET = "https://www.luogu.com.cn/training/1096881#scoreboard"
OUT = Path("data/screenshots")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1680, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # ---------- 结构分析页 ----------
        print("=== 结构分析页: 洛谷权限页 ===")
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(900)
        page.fill("#analyzeUrl", TARGET)
        page.click("#btnAnalyzeStart")

        try:
            page.wait_for_selector("#analyzeOutput .login-wall", timeout=180000)
            appeared = True
        except Exception:
            appeared = False

        if not appeared:
            failures.append("分析页没有渲染出诊断卡")
            print("  ✗ 分析页没有渲染出诊断卡")
        else:
            issue_type = page.get_attribute("#analyzeOutput .login-wall", "data-type")
            title = (page.text_content("#analyzeOutput .login-wall__title") or "").strip()
            raw = page.eval_on_selector_all(
                "#analyzeOutput .login-wall__text", "e => e.map(x => x.textContent).join(' | ')"
            )
            suggestions = page.eval_on_selector_all(
                "#analyzeOutput .login-wall__list li", "e => e.map(x => x.textContent)"
            )
            print(f"  类型    : {issue_type}")
            print(f"  标题    : {title}")
            print(f"  页面文本: {(raw or '')[:110]}")
            print(f"  建议数  : {len(suggestions)}")

            if issue_type != "permission_denied":
                failures.append(f"类型是 {issue_type}, 期望 permission_denied")
            if "没有权限" not in (raw or ""):
                failures.append("卡片里没有展示页面上的『没有权限』原文")
            if "权限" not in title:
                failures.append(f"标题未点明权限问题: {title}")

            shot = OUT / "15_luogu_permission_analyze.png"
            page.screenshot(path=str(shot))
            print(f"  截图 -> {shot.name}")

        # ---------- 抓取页 ----------
        print("\n=== 抓取页: 洛谷权限页 ===")
        # 等前一个任务结束(开始按钮恢复)
        try:
            page.wait_for_function(
                "() => { const b = document.querySelector('#btnCrawlStart'); return b && !b.hidden; }",
                timeout=120000,
            )
        except Exception:
            pass
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(700)
        page.fill("#crawlUrl", TARGET)
        page.fill("#crawlGoal", "抓取训练题目的名称与得分")
        page.select_option("#crawlFormat", "")
        page.click("#btnCrawlStart")

        try:
            page.wait_for_selector("#crawlResultCard:not([hidden]) .login-wall", timeout=240000)
            appeared2 = True
        except Exception:
            appeared2 = False

        if not appeared2:
            failures.append("抓取页没有渲染出诊断卡")
            print("  ✗ 抓取页没有渲染出诊断卡")
        else:
            issue_type2 = page.get_attribute("#crawlResultCard .login-wall", "data-type")
            raw2 = page.eval_on_selector_all(
                "#crawlResultCard .login-wall__text", "e => e.map(x => x.textContent).join(' | ')"
            )
            print(f"  类型    : {issue_type2}")
            print(f"  页面文本: {(raw2 or '')[:110]}")
            if issue_type2 != "permission_denied":
                failures.append(f"抓取页类型是 {issue_type2}, 期望 permission_denied")
            if "没有权限" not in (raw2 or ""):
                failures.append("抓取页卡片里没有展示页面原文")

            shot2 = OUT / "16_luogu_permission_crawl.png"
            page.screenshot(path=str(shot2))
            print(f"  截图 -> {shot2.name}")

        browser.close()

    print(f"\n前端错误: {len(errors)}")
    for err in errors[:6]:
        print(f"  ! {err}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 64)
    if failures:
        print("洛谷 UI 实测: 未通过 ✗")
        for item in failures:
            print(f"  - {item}")
    else:
        print("洛谷 UI 实测: 通过 ✓ (卡片如实展示了页面原文)")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
