"""
验收: 「站点返回空结果」(常见于被限流) 能否被诊断出来。

背景: 实测网易云音乐被限流后, **任意**关键词都返回同一句「很抱歉，未能找到相关搜索结果！」
—— 连"周杰伦"都搜不到。此时页面结构完好(内层 frame 203 个元素)、没有验证码、接口全 200,
只有这一句话。重载与换浏览器上下文都无效(限流按出口 IP, 不按上下文累积)。

不识别它的后果: 框架只说"未提取到任何数据, 请尝试放宽规则", 用户会去反复调规则,
而真正该做的是停一会儿或换出口 IP。

本脚本用**离线构造的页面**做判据单测(不依赖目标站点当前是否被限流), 再用真实站点做一次
端到端确认。这样即使站点恢复正常, 判据也一直被守着。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


#: 应当在诊断时被识别为"空结果/限流"的页面文本
EMPTY_RESULT_TEXT = [
    "很抱歉，未能找到相关搜索结果！",
    "很抱歉，未能找到相关搜索结果！ 云村交易所 服务条款",
    "没有找到相关内容",
    "未找到相关结果",
    "无相关内容",
    "搜索结果为空",
    "No results found",
    "Nothing found",
]

#: 正常页面文本, **绝不能**被误判
HEALTHY_TEXT = [
    "东京不太热 封茗囧菌 《东京不太热》 03:44",
    "周杰伦 晴天 叶惠美 04:29",
    "搜索结果 共 301 首单曲",
    "发现音乐 我的音乐 关注 商城 音乐人",
]


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.access_control import detect_access_issue  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        print("=== 1) 正向: 空结果文本必须被判成 rate_limited ===")
        for text in EMPTY_RESULT_TEXT:
            await page.set_content(
                f"<!doctype html><html><body><div id='main'>{text}</div></body></html>")
            issue = await detect_access_issue(page, requested_url="https://example.com/search")
            ok = issue.detected and issue.issue_type == "rate_limited"
            check(ok, f"识别 {text[:22]!r}", f"-> {issue.issue_type!r}")

        print("\n=== 2) 反向: 正常页面文本不能被误判 ===")
        for text in HEALTHY_TEXT:
            await page.set_content(
                f"<!doctype html><html><body><div id='main'>{text}</div></body></html>")
            issue = await detect_access_issue(page, requested_url="https://example.com/search")
            bad = issue.detected and issue.issue_type == "rate_limited"
            check(not bad, f"不误判 {text[:22]!r}", f"-> {issue.issue_type!r}")

        print("\n=== 3) 建议里必须给出可执行动作 ===")
        await page.set_content(
            "<!doctype html><html><body>很抱歉，未能找到相关搜索结果！</body></html>")
        issue = await detect_access_issue(page, requested_url="https://example.com/search")
        sug = " ".join(issue.suggestions or [])
        for kw in ("停", "限速", "热门", "IP"):
            check(kw in sug, f"建议里提到 {kw!r}", sug[:80])

        await browser.close()

    print("\n" + "=" * 66)
    if failures:
        print(f"空结果诊断验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("空结果诊断验收: 通过 ✓")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
