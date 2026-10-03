"""
UI 布局与状态回归测试: 真实提交一次抓取, 在**任务进行中**截图, 并断言:

1. 右侧粘滞侧栏(.stack--side)与左侧表单/结果卡**不重叠** —— 用几何矩形相交判定,
   而不是靠肉眼看截图。这是"进度容器与其他容器重叠"那个 bug 的固化判据。
2. 任务结束后状态标签切到「已完成」、进度到 100%、"取消任务"按钮隐藏 ——
   这是"抓取成功后仍显示进行中"那个 bug 的固化判据。

用法: python scripts/verify_ui_layout.py [目标URL]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://books.toscrape.com"
OUT = Path("data/screenshots")

# 重叠判定的容差(像素): 允许描边/阴影造成的 1~2px 视觉贴合
TOLERANCE = 2


def rect(page, selector: str) -> dict | None:
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom,
                    left: r.left, right: r.right, hidden: el.offsetParent === null};
        }""",
        selector,
    )


def overlaps(a: dict, b: dict) -> bool:
    """两个矩形是否有实际重叠面积(排除容差内的贴边)。"""
    if not a or not b or a["hidden"] or b["hidden"]:
        return False
    dx = min(a["right"], b["right"]) - max(a["left"], b["left"])
    dy = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return dx > TOLERANCE and dy > TOLERANCE


def overlap_area(a: dict, b: dict) -> tuple[float, float]:
    if not a or not b or a["hidden"] or b["hidden"]:
        return (0.0, 0.0)
    dx = min(a["right"], b["right"]) - max(a["left"], b["left"])
    dy = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return (max(dx, 0.0), max(dy, 0.0))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1680, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # ---------- 1) 滚动位置下的重叠检查(静态布局) ----------
        print("=== 布局重叠检查(多个滚动位置) ===")
        side_sel, form_sel, result_sel = ".stack--side", ".card--form", "#crawlResultCard"
        for scroll in (0, 300, 700, 1200):
            page.evaluate(f"document.querySelector('#content').scrollTo(0, {scroll})")
            page.wait_for_timeout(320)
            side = rect(page, side_sel)
            form = rect(page, form_sel)
            result = rect(page, result_sel)
            bad_form = overlaps(side, form)
            bad_result = overlaps(side, result)
            print(
                f"  scroll={scroll:>5}px  侧栏y={side['y']:7.1f} 表单y={form['y']:7.1f}"
                f"  与表单重叠={bad_form}  与结果卡重叠={bad_result}"
            )
            if bad_form:
                dx, dy = overlap_area(side, form)
                failures.append(f"scroll={scroll}: 侧栏与表单重叠 {dx:.0f}x{dy:.0f}px")
            if bad_result:
                dx, dy = overlap_area(side, result)
                failures.append(f"scroll={scroll}: 侧栏与结果卡重叠 {dx:.0f}x{dy:.0f}px")

        # ---------- 2) 提交任务, 在运行中再次检查并截图 ----------
        page.evaluate("document.querySelector('#content').scrollTo(0, 0)")
        print(f"\n=== 提交抓取任务: {TARGET} ===")
        page.fill("#crawlUrl", TARGET)
        page.fill("#crawlGoal", "抓取所有书籍的名称、价格和详情页链接")
        page.fill("#crawlMaxPages", "2")
        page.select_option("#crawlFormat", "json")
        page.click("#btnCrawlStart")

        # 等到任务确实进入 running(状态标签出现"进行中")
        page.wait_for_function(
            """() => {
                const tag = document.querySelector('#taskStatusTag');
                return tag && (tag.textContent.includes('进行中') || tag.textContent.includes('已完成'));
            }""",
            timeout=45000,
        )
        # 明确等到"进行中"再断言 —— 只等"进行中或已完成"会与快速任务赛跑:
        # 任务若在 4 秒内结束, 取消按钮本就该隐藏, 断言会误报。
        page.wait_for_function(
            """() => {
                const tag = document.querySelector('#taskStatusTag');
                return tag && tag.textContent.trim() === '进行中';
            }""",
            timeout=45000,
        )
        page.wait_for_timeout(1500)  # 让浏览器启动/导航产生真实进度

        running_shot = OUT / "09_running_task.png"
        page.screenshot(path=str(running_shot))
        print(f"  运行中截图 -> {running_shot.name}")

        # 运行中: 滚到页面底部(最容易暴露重叠的位置)
        page.evaluate("document.querySelector('#content').scrollTo(0, 99999)")
        page.wait_for_timeout(400)
        side, form, result = rect(page, side_sel), rect(page, form_sel), rect(page, result_sel)
        running_overlap = overlaps(side, result)
        print(f"  运行中+滚到底: 侧栏与结果卡重叠={running_overlap}")
        if running_overlap:
            dx, dy = overlap_area(side, result)
            failures.append(f"运行中滚动到底: 侧栏与结果卡重叠 {dx:.0f}x{dy:.0f}px")

        # 检查取消按钮可见(说明确实在运行)
        cancel_visible = page.evaluate("() => !document.querySelector('#btnCrawlCancel').hidden")
        print(f"  取消按钮可见(进行中): {cancel_visible}")
        if not cancel_visible:
            failures.append("任务运行中却没有显示取消按钮")

        # ---------- 3) 等任务结束, 检查终态 UI ----------
        print("\n=== 等待任务结束 ===")
        page.wait_for_function(
            """() => {
                const tag = document.querySelector('#taskStatusTag');
                if (!tag) return false;
                const t = tag.textContent.trim();
                return t === '已完成' || t === '失败' || t === '已取消';
            }""",
            timeout=180000,
        )
        page.wait_for_timeout(1200)

        status_text = page.text_content("#taskStatusTag").strip()
        status_state = page.get_attribute("#taskStatusTag", "data-state")
        pct = page.text_content("#progressPct").strip()
        # 等加载态复位(提交时的 spinner 由 withLoading 在 POST 返回后恢复)
        page.wait_for_function(
            "() => !document.querySelector('#btnCrawlStart').classList.contains('is-loading')",
            timeout=15000,
        )
        start_state = page.evaluate(
            """() => {
                const b = document.querySelector('#btnCrawlStart');
                return {hidden: b.hidden, disabled: b.disabled, loading: b.classList.contains('is-loading')};
            }"""
        )
        cancel_hidden = page.evaluate("() => document.querySelector('#btnCrawlCancel').hidden")
        result_visible = page.evaluate("() => !document.querySelector('#crawlResultCard').hidden")
        rows = page.eval_on_selector_all("table.data tbody tr", "els => els.length")
        running_attr = page.get_attribute("#progressCard", "data-running")
        fill_width = page.evaluate("() => document.querySelector('#progressFill').style.width")

        # 时间线终态检查: 成功结束后所有步骤都应为 done(绿色圆点), 不能停在灰色
        steps = page.eval_on_selector_all(
            "#timeline .timeline__item",
            "els => els.map(e => ({status: e.dataset.status, label: e.textContent.trim().slice(0, 12)}))",
        )
        step_statuses = [s["status"] for s in steps]
        dot_colors = page.evaluate(
            """() => [...document.querySelectorAll('#timeline .timeline__dot')]
                .map(d => getComputedStyle(d).borderColor)"""
        )

        print(f"  状态标签   : {status_text} (data-state={status_state})")
        print(f"  进度       : {pct}  (fill width={fill_width})")
        print(f"  开始按钮   : hidden={start_state['hidden']} disabled={start_state['disabled']}")
        print(f"  取消按钮隐藏: {cancel_hidden}")
        print(f"  progressCard data-running: {running_attr}")
        print(f"  结果卡可见 : {result_visible}   表格行数: {rows}")
        print(f"  时间线步骤 : {step_statuses}")

        done_shot = OUT / "10_task_done.png"
        page.screenshot(path=str(done_shot))
        print(f"  完成截图   -> {done_shot.name}")

        if status_text != "已完成":
            failures.append(f"任务结束后状态标签是「{status_text}」, 应为「已完成」")
        if pct != "100%":
            failures.append(f"任务结束后进度是 {pct}, 应为 100%")
        if not cancel_hidden:
            failures.append("任务结束后『取消任务』按钮仍然可见")
        if start_state["hidden"]:
            failures.append("任务结束后『开始抓取』按钮没有恢复显示")
        if start_state["disabled"]:
            failures.append("任务结束后『开始抓取』按钮仍处于禁用状态")
        if not result_visible or rows == 0:
            failures.append("任务结束后没有渲染结果表格")
        if steps and any(s != "done" for s in step_statuses):
            failures.append(f"时间线步骤未全部完成: {step_statuses}")
        if len(steps) != 8:
            failures.append(f"时间线步骤数为 {len(steps)}, 应为 8")
        if running_attr != "false":
            failures.append(f"progressCard data-running={running_attr}, 应为 false")

        # ---------- 4) 其它页面的栅格重叠检查 ----------
        print("\n=== 其它页面滚动重叠检查 ===")
        for route in ("analyze", "requests", "results", "settings", "about"):
            page.evaluate(f"window.location.hash = '#/{route}'")
            page.wait_for_timeout(900)
            page.evaluate("document.querySelector('#content').scrollTo(0, 99999)")
            page.wait_for_timeout(360)
            # 卡片之间不应相互覆盖: 取同一父容器下的相邻卡片比较
            bad = page.evaluate(
                """() => {
                    const cards = [...document.querySelectorAll('.page.is-active .card')]
                        .filter(c => c.offsetParent !== null);
                    const r = c => c.getBoundingClientRect();
                    let hits = 0;
                    for (let i = 0; i < cards.length; i++) {
                        for (let j = i + 1; j < cards.length; j++) {
                            const a = r(cards[i]), b = r(cards[j]);
                            const dx = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                            const dy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                            // 卡片在视觉上可以横向并列, 因此只统计纵向+横向都明显相交的情况
                            if (dx > 4 && dy > 4) hits++;
                        }
                    }
                    return hits;
                }"""
            )
            print(f"  {route:<9} 卡片重叠对数: {bad}")
            if bad:
                failures.append(f"{route} 页存在 {bad} 对卡片重叠")

        browser.close()

    print(f"\n前端错误: {len(errors)}")
    for err in errors[:10]:
        print(f"  ! {err}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 64)
    if failures:
        print(f"布局/状态回归: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("布局/状态回归: 通过 ✓ (无重叠, 终态 UI 正确)")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
