"""
真实站点回归: 洛谷训练页(HTTP 401 权限不足, 不跳登录页)。

这是用户实际报的场景。跑通它等于确认三件事:
1. 导航不再把 401 当失败丢掉 —— 页面能拿到, 诊断才有素材;
2. 归类为 ``permission_denied`` 而不是 ``login_required``(否则用户会去折腾会话配置);
3. 页面实际文本与关键元素被提取出来, 足以让人判断问题所在。

用法: python scripts/verify_luogu_case.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

TARGET = "https://www.luogu.com.cn/training/1096881"
FRAGMENT_URL = f"{TARGET}#scoreboard"

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


async def main() -> int:
    from smartcrawler.config import get_settings
    from smartcrawler.crawler import SmartCrawler

    settings = get_settings()
    print(f"目标: {FRAGMENT_URL}\n")

    crawler = SmartCrawler(settings)
    try:
        # ---- 1) 结构分析路径 ----
        print("=== 结构分析 ===")
        report, _stats = await crawler.analyze_only(FRAGMENT_URL)
        check(report is not None, "拿到了结构报告(401 页面没被丢弃)")
        if report is not None:
            print(f"  标题: {report.title!r}")
            issue = report.access_issue
            check(issue is not None, "报告带上了访问受限诊断")
            if issue is not None:
                print(f"  类型: {issue.issue_type} | 置信度: {issue.confidence:.0%}")
                print(f"  摘要: {issue.summary()}")
                check(issue.detected, "判定存在访问受限")
                check(
                    issue.issue_type == "permission_denied",
                    "归类为 permission_denied(不是 login_required)",
                    issue.issue_type,
                )
                check(issue.http_status == 401, "记录了 HTTP 401", str(issue.http_status))
                check(not issue.redirected, "未把锚点变化误报为跳转")
    finally:
        await crawler.close()

    # ---- 2) 抓取路径 ----
    print("\n=== 抓取任务 ===")
    crawler = SmartCrawler(settings)
    try:
        result = await crawler.crawl(FRAGMENT_URL, goal="抓取训练题目的名称", max_pages=1)
    finally:
        await crawler.close()

    print(f"  条数: {result.item_count} | 耗时: {result.duration_ms:.0f}ms")
    check(result.item_count == 0, "确实拿不到数据(符合预期)")
    issue = result.access_issue
    check(issue is not None, "抓取结果带上了诊断")
    if issue is None:
        return 1

    print(f"\n  类型      : {issue.issue_type}")
    print(f"  页面标题  : {issue.page_title}")
    print(f"  可见文本  : {issue.visible_text[:120]!r}")
    print(f"  主内容    : {issue.main_text[:120]!r}")
    print(f"  判定依据  :")
    for reason in issue.reasons:
        print(f"    · {reason}")
    print(f"  文本元素({len(issue.text_elements)}):")
    for element in issue.text_elements:
        print(f"    [{element.role}] {element.text[:80]}")
    print(f"  错误码    : {issue.error_codes}")
    print(f"  建议      :")
    for tip in issue.suggestions:
        print(f"    - {tip}")

    check(issue.issue_type == "permission_denied", "抓取路径同样归类为权限不足", issue.issue_type)
    check(bool(issue.visible_text), "提供了页面可见文本")
    check(bool(issue.main_text), "提供了主内容区文本")
    check(bool(issue.text_elements), "提供了关键文本元素", f"{len(issue.text_elements)} 个")
    check(
        any("没有权限" in (e.text or "") for e in issue.text_elements)
        or "没有权限" in issue.visible_text,
        "页面文本里包含了『没有权限』这一关键信息",
    )
    check(
        any("权限" in s or "报名" in s for s in issue.suggestions),
        "建议指向权限方向",
    )
    # 权限问题不该建议去配会话 —— 那是登录问题的处置方式
    check(
        not any("session.json" in s for s in issue.suggestions),
        "没有给出误导性的会话配置建议",
    )

    # ---- 3) 结果可序列化(Web 层要把它发给前端) ----
    try:
        payload = issue.model_dump(mode="json")
        json.dumps(payload, ensure_ascii=False)
        check(True, "诊断结果可 JSON 序列化")
    except (TypeError, ValueError) as exc:
        check(False, "诊断结果可 JSON 序列化", str(exc))

    print("\n" + "=" * 66)
    if failures:
        print(f"洛谷场景回归: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"洛谷场景回归: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
