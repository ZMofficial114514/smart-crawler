"""
使用示例 2: 自然语言指令驱动抓取。

演示 "抓取所有商品名称和价格" 这类自然语言目标如何被 AI 转换成可执行的提取规则:
    1. 打开页面, 监听 XHR/fetch 请求;
    2. 分析 DOM 结构, 找出候选列表区;
    3. 把【结构报告 + 你的自然语言目标】交给大模型, 产出 ExtractionRule(JSON);
    4. 按规则提取数据, 全程打印生成的规则便于检查。

AI 不可用(未配 Key / --offline)时自动降级为规则引擎:
    直接使用结构分析识别出的候选列表 + 字段启发式命名, 同样能跑通全流程。

运行(项目根目录):
    python examples/nl_goal_demo.py --url http://books.toscrape.com --goal "抓取所有书籍的名称和价格"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler import SmartCrawler  # noqa: E402


async def run(url: str, goal: str, offline: bool) -> None:
    async with SmartCrawler() as crawler:
        if offline:
            crawler.ai.config.offline = True  # 强制规则引擎, 不发起 AI 调用
        print(f"目标: {goal}\nURL : {url}")
        print(f"AI 可用: {crawler.ai.available}\n")

        result = await crawler.crawl(url, goal=goal, format="json", output="data/nl_result.json")

    print("\n========== AI 生成的提取规则 ==========")
    if result.rule:
        print(json.dumps(result.rule.model_dump(), ensure_ascii=False, indent=2))
    else:
        print("(未生成规则)")

    print("\n========== 抓取结果 ==========")
    print(f"成功: {result.success} | 条数: {result.item_count} | 页数: {result.pages_crawled}")
    print(f"规则来源: {result.rule.source if result.rule else '-'}")
    if result.errors:
        print(f"错误: {result.errors}")
    for item in result.items[:8]:
        print("  ", json.dumps(item, ensure_ascii=False))
    if result.saved_to:
        print(f"\n已保存: {result.saved_to}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartCrawler 自然语言抓取示例")
    parser.add_argument("--url", default="http://books.toscrape.com", help="目标页面")
    parser.add_argument("--goal", default="抓取所有书籍的名称、价格和详情链接",
                        help="自然语言抓取目标")
    parser.add_argument("--offline", action="store_true", help="离线模式(不调用 AI, 用规则引擎)")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.goal, args.offline))


if __name__ == "__main__":
    main()
