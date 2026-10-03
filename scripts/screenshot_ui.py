"""
UI 视觉验证: 用 Playwright 打开控制台, 截图各页面并收集浏览器控制台报错。

自动化测试能证明接口可用, 但证明不了"界面长得对"。这个脚本真实渲染页面, 因此能
提前发现: JS 加载失败、模块路径写错、样式没生效、布局塌陷等问题。

用法::

    python scripts/screenshot_ui.py                  # 截图到 data/screenshots/
    python scripts/screenshot_ui.py --url http://127.0.0.1:8322
    python scripts/screenshot_ui.py --theme daylight  # 顺带验证浅色主题

需要服务已在运行: python -m smartcrawler web
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

PAGES = [
    ("crawl", "抓取任务"),
    ("analyze", "结构分析"),
    ("requests", "网络抓包"),
    ("results", "结果与历史"),
    ("plugins", "插件"),
    ("settings", "系统配置"),
    ("about", "关于与帮助"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="控制台 UI 截图与前端报错采集")
    parser.add_argument("--url", default="http://127.0.0.1:8322", help="控制台地址")
    parser.add_argument("--out", default="data/screenshots", help="截图输出目录")
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--theme", default="aurora", choices=["aurora", "daylight", "contrast"])
    parser.add_argument("--full", action="store_true", help="整页截图(含滚动区域)")
    parser.add_argument(
        "--keep-all",
        action="store_true",
        help="保留全部页面截图;默认只保留数据详情与日志抽屉(页面截图仅供即时核对, 单张约 4MB)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []
    failed_requests: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,  # 2x 便于检查毛玻璃与描边细节
            locale="zh-CN",
        )
        page = context.new_page()

        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"[pageerror] {exc}"))
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.url} — {req.failure}"))

        print(f"打开 {args.url} …")
        page.goto(args.url, wait_until="networkidle", timeout=60000)
        # 等首屏动画与健康状态回填完成
        page.wait_for_timeout(2600)

        if args.theme != "aurora":
            page.evaluate(f"document.documentElement.dataset.theme = '{args.theme}'")
            page.wait_for_timeout(600)

        shots: list[str] = []
        page_shots: list[str] = []
        for index, (route, label) in enumerate(PAGES):
            page.evaluate(f"window.location.hash = '#/{route}'")
            page.wait_for_timeout(1500 if index else 900)
            path = out_dir / f"{index + 1:02d}_{route}_{args.theme}.png"
            page.screenshot(path=str(path), full_page=args.full)
            shots.append(str(path))
            page_shots.append(str(path))
            print(f"  ✓ {label:<10} -> {path.name}")

        # 顺带验证: 日志抽屉能打开、设置面板确实渲染出控件
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(400)
        page.click("#chipLogs")
        page.wait_for_timeout(1000)
        drawer_shot = out_dir / f"07_logdrawer_{args.theme}.png"
        page.screenshot(path=str(drawer_shot))
        shots.append(str(drawer_shot))
        log_lines = page.eval_on_selector_all(".log-line", "els => els.length")

        page.keyboard.press("Escape")
        page.evaluate("window.location.hash = '#/settings'")
        page.wait_for_timeout(1800)
        config_rows = page.eval_on_selector_all(".config-row", "els => els.length")
        cards = page.eval_on_selector_all(".card", "els => els.length")
        glass_ok = page.evaluate(
            "getComputedStyle(document.querySelector('.card')).backdropFilter"
        )

        # 关键一步: 打开一条历史抓取任务, 渲染真实数据表格。
        # 表单/空列表都容易"看起来正常", 只有真实数据才能暴露表格与列宽问题。
        data_rows = 0
        page.evaluate("window.location.hash = '#/results'")
        page.wait_for_timeout(1800)
        task_items = page.query_selector_all("#taskList .task-item")
        if task_items:
            task_items[0].click()
            page.wait_for_timeout(2400)
            data_rows = page.eval_on_selector_all("table.data tbody tr", "els => els.length")
            detail_shot = out_dir / f"08_task_detail_{args.theme}.png"
            page.screenshot(path=str(detail_shot))
            shots.append(str(detail_shot))
            print(f"  ✓ 任务详情       -> {detail_shot.name} ({data_rows} 行数据)")
        else:
            print("  · 没有历史任务, 跳过数据表格截图")

        print(f"\n界面自检:")
        print(f"  日志抽屉行数     : {log_lines}")
        print(f"  配置控件行数     : {config_rows}")
        print(f"  卡片数量         : {cards}")
        print(f"  数据表格行数     : {data_rows}")
        print(f"  毛玻璃 backdrop  : {glass_ok or '(不支持, 已走降级样式)'}")

        browser.close()

    # 页面截图只用于即时核对样式, 单张约 4MB。默认清掉, 避免 data/ 被截图撑大;
    # 需要留档时加 --keep-all。
    if not args.keep_all:
        removed = 0
        for path in page_shots:
            try:
                Path(path).unlink()
                shots.remove(path)
                removed += 1
            except OSError:
                pass
        print(f"\n已清理 {removed} 张页面截图(保留数据详情/日志抽屉; 用 --keep-all 可全部保留)")

    print(f"\n前端错误: {len(console_errors)}")
    for err in console_errors[:15]:
        print(f"  ! {err}")
    print(f"失败请求: {len(failed_requests)}")
    for req in failed_requests[:15]:
        print(f"  ! {req}")

    print(f"\n共生成 {len(shots)} 张截图 -> {out_dir.resolve()}")
    ok = not console_errors and not failed_requests and config_rows > 20
    print("UI 验证: " + ("通过 ✓" if ok else "存在问题 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
