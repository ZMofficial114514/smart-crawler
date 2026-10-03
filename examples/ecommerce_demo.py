"""
使用示例 1: 抓取电商商品列表, 提取名称/价格/链接, 保存为 CSV。

演示站点: http://books.toscrape.com —— 一个专为爬虫练习搭建的图书商店
(合规、允许抓取), 页面结构与真实电商(商品卡片 = 图片 + 标题 + 价格 + 链接)一致。
要换真实电商, 只需替换 URL 与选择器(或直接用 AI 目标模式)。

运行(项目根目录):
    python examples/ecommerce_demo.py                 # 手写规则模式(无需 AI)
    python examples/ecommerce_demo.py --ai            # AI 目标模式(需配置 API Key)
    python examples/ecommerce_demo.py --pages 3       # 抓取前 3 页
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 保证从任意目录运行都能 import 项目包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler import SmartCrawler  # noqa: E402
from smartcrawler.models import ExtractionRule, FieldSpec, ListRule  # noqa: E402

BOOKS_URL = "http://books.toscrape.com"

# 该站页面的固定 class 无随机后缀, 可直接手写稳定规则
# (AI 模式无需此规则, 由模型根据结构报告自动生成)
MANUAL_RULE = ExtractionRule(
    mode="dom",
    list_rule=ListRule(
        item_selector="article.product_pod",
        fields=[
            FieldSpec(name="title", selector="h3 a", attribute=None,
                      transform=["strip"], required=True),
            FieldSpec(name="price", selector="p.price_color", attribute=None,
                      transform=["price"], required=True),
            FieldSpec(name="link", selector="h3 a", attribute="href",
                      transform=["url"], required=True),   # url 变换: 相对链接补全域名
            FieldSpec(name="stock", selector=".instock.availability",
                      transform=["strip"]),
        ],
    ),
    source="manual",
    notes="books.toscrape.com 手写规则示例",
)


async def run(mode: str, pages: int) -> None:
    async with SmartCrawler() as crawler:   # 自动启动/关闭浏览器
        if mode == "ai":
            # 自然语言目标 -> AI 生成规则 -> 提取 -> CSV
            result = await crawler.crawl(
                BOOKS_URL,
                goal="抓取所有书籍的名称(title)、价格(price, 数字)和详情链接(link)",
                format="csv",
                output="data/books_ai.csv",
                max_pages=pages,
            )
        else:
            # 显式规则 -> 提取 -> CSV(不依赖 AI)
            result = await crawler.crawl(
                BOOKS_URL,
                rule=MANUAL_RULE,
                format="csv",
                output="data/books.csv",
                max_pages=pages,
            )

    print("\n========== 结果 ==========")
    print(f"成功        : {result.success}")
    print(f"条数        : {result.item_count}")
    print(f"页数        : {result.pages_crawled}")
    print(f"规则来源    : {result.rule.source if result.rule else None}")
    if result.rule and result.rule.notes:
        print(f"规则说明    : {result.rule.notes}")
    print(f"网络请求捕获: {result.network_record_count} 条")
    print(f"保存位置    : {result.saved_to}")
    if result.errors:
        print(f"错误        : {result.errors}")
    for item in result.items[:5]:
        print("  示例:", item)


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartCrawler 电商列表示例")
    parser.add_argument("--ai", action="store_true", help="使用 AI 目标模式(需配置 API Key)")
    parser.add_argument("--pages", type=int, default=1, help="抓取页数(默认 1)")
    args = parser.parse_args()
    asyncio.run(run("ai" if args.ai else "manual", args.pages))


if __name__ == "__main__":
    main()
