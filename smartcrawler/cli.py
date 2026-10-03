"""
SmartCrawler 命令行入口。

用法示例:
    # 自然语言抓取(AI 生成规则), 保存 CSV
    python -m smartcrawler crawl https://books.toscrape.com --goal "抓取所有书籍的名称、价格和链接" --format csv --output books.csv

    # 显式规则抓取(JSON 文件, 结构见 models.ExtractionRule)
    python -m smartcrawler crawl https://books.toscrape.com --rule rule.json --format csv --max-pages 3

    # 仅分析页面结构
    python -m smartcrawler analyze https://books.toscrape.com

    # 查看页面发出的 XHR/fetch 请求
    python -m smartcrawler requests https://example.com --wait 6 --pattern "api"

    # 启动 FastAPI 调试服务
    python -m smartcrawler serve --port 8322

合规提示: 默认遵守 robots.txt, 默认限速 1~3 秒/请求, 仅用于合法授权的采集。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from loguru import logger

from .config import Settings, get_settings
from .utils import force_utf8_output, setup_logging

# 在打印任何内容之前先切到 UTF-8 —— 否则下面的启动横幅会以 OEM 代码页(Windows 简体
# 中文为 GBK)输出, 在 UTF-8 终端里显示成乱码。原先只有 setup_logging() 会做这件事,
# 而横幅在它之前就打印了, 所以横幅一直乱码。
force_utf8_output()

BANNER = r"""
 ____  __  __ ___ _   _ _____ ____      _    _
/ ___||  \/  |_ _| \ | | ____|  _ \    / \  | |
\___ \| |\/| || ||  \| |  _| | |_) |  / _ \ | |
 ___) | |  | || || |\  | |___|  _ <  / ___ \| |___
|____/|_|  |_|___|_| \_|_____|_| \_\/_/   \_\_____|
  智能爬虫框架 | Playwright + AI | 仅限合法授权采集
"""


def _print_json(obj: object, limit: int = 0) -> None:
    """人类友好的 JSON 输出。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if limit and len(text) > limit:
        text = text[:limit] + "\n...[输出截断]"
    print(text)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_crawl(args: argparse.Namespace) -> int:
    from .crawler import SmartCrawler
    from .models import ExtractionRule
    from .utils import safe_json_loads

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    if args.no_ai:
        settings.ai.offline = True

    rule: ExtractionRule | None = None
    if args.rule:
        data = safe_json_loads(open(args.rule, encoding="utf-8").read())
        if not isinstance(data, dict):
            logger.error(f"规则文件解析失败: {args.rule}")
            return 2
        rule = ExtractionRule.model_validate(data)

    async def _run():
        async with SmartCrawler(settings) as crawler:
            return await crawler.crawl(
                url=args.url,
                goal=args.goal,
                rule=rule,
                format=args.format,
                output=args.output,
                max_pages=args.max_pages,
                extra_wait=args.wait,
            )

    result = asyncio.run(_run())
    summary = {
        "success": result.success,
        "url": result.url,
        "goal": result.goal,
        "item_count": result.item_count,
        "pages_crawled": result.pages_crawled,
        "network_records": result.network_record_count,
        "rule_source": result.rule.source if result.rule else None,
        "rule_notes": result.rule.notes if result.rule else None,
        "errors": result.errors,
        "saved_to": result.saved_to,
        "duration_ms": result.duration_ms,
    }
    print("\n===== 抓取结果摘要 =====")
    _print_json(summary)
    if args.show and result.items:
        print("\n===== 前 10 条数据 =====")
        _print_json(result.items[:10])
    elif not result.items:
        print("\n未提取到数据。调试建议:")
        print("  1) python -m smartcrawler analyze <url> 查看候选列表结构")
        print("  2) python -m smartcrawler requests <url> 查看是否有可用的 XHR JSON")
    return 0 if result.success else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    from .crawler import SmartCrawler

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)

    async def _run():
        async with SmartCrawler(settings) as crawler:
            report, stats = await crawler.analyze_only(args.url)
            return report, stats

    report, stats = asyncio.run(_run())
    if report is None:
        logger.error("分析失败")
        return 1
    out = {
        "url": report.url,
        "title": report.title,
        "dom_stats": report.dom_stats,
        "pagination": report.pagination.model_dump() if report.pagination else None,
        "candidate_lists": [c.model_dump() for c in report.candidate_lists],
        "metadata_keys": list(report.metadata.keys()),
        "network_stats": stats,
    }
    if args.full:
        out["simplified_tree"] = report.simplified_tree  # type: ignore[assignment]
        out["metadata"] = report.metadata  # type: ignore[assignment]
    print("\n===== 页面结构报告 =====")
    _print_json(out, limit=20000)
    if report.candidate_lists:
        best = report.candidate_lists[0]
        print(f"\n>>> 最可能的列表项选择器: {best.item_selector} (重复 {best.count} 项)")
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    from .crawler import SmartCrawler

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)

    async def _run():
        async with SmartCrawler(settings) as crawler:
            return await crawler.capture_requests(args.url, wait_seconds=args.wait)

    records = asyncio.run(_run())
    matched = [r for r in records if not args.pattern or __import__("re").search(args.pattern, r.url)]
    print(f"\n共捕获 {len(records)} 条请求, 匹配 {len(matched)} 条\n")
    for r in matched[: args.limit]:
        body_flag = "JSON" if r.body_json is not None else ("text" if r.body_text else "-")
        print(f"[{r.method} {r.status}] ({r.resource_type}/{body_flag}) {r.url}")
    if args.dump:
        from .models import NetworkRecord

        with open(args.dump, "w", encoding="utf-8") as f:
            for r in matched:
                f.write(r.model_dump_json(exclude_none=True) + "\n")
        print(f"\n完整记录已写入 {args.dump}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        logger.error("未安装 fastapi/uvicorn: pip install fastapi uvicorn")
        return 2
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    host = args.host or settings.api.host
    port = args.port or settings.api.port
    print(f"SmartCrawler 调试接口: http://{host}:{port}/api/docs")
    uvicorn.run("smartcrawler.web.api:app", host=host, port=port, log_level="info")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """启动图形化控制台(推荐入口)。"""
    from .web.__main__ import serve

    setup_logging(get_settings().log_level, get_settings().log_file)
    return serve(host=args.host, port=args.port, reload=args.reload)


# ---------------------------------------------------------------------------
# argparse 组装
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartcrawler",
        description="SmartCrawler 智能爬虫框架(Playwright + AI)。仅限合法授权的数据采集。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=BANNER,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # web
    p = sub.add_parser("web", help="启动图形化控制台(推荐)")
    p.add_argument("--host", default=None, help="监听地址(默认取配置 api.host)")
    p.add_argument("--port", type=int, default=None, help="监听端口(默认取配置 api.port)")
    p.add_argument("--reload", action="store_true", help="代码变更自动重载(开发用)")
    p.set_defaults(func=cmd_web)

    # crawl
    p = sub.add_parser("crawl", help="抓取页面(支持自然语言目标/显式规则)")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--goal", "-g", default=None, help="自然语言目标, 如: 抓取所有商品名称和价格")
    p.add_argument("--rule", default=None, help="显式规则 JSON 文件路径(优先于 goal)")
    p.add_argument("--format", "-f", default=None, choices=["json", "jsonl", "csv", "sqlite"], help="输出格式")
    p.add_argument("--output", "-o", default=None, help="输出路径(默认按时间命名到 data/)")
    p.add_argument("--max-pages", type=int, default=None, help="最大翻页数")
    p.add_argument("--wait", type=float, default=0.0, help="页面加载后额外等待秒数")
    p.add_argument("--no-ai", action="store_true", help="禁用 AI(强制规则引擎)")
    p.add_argument("--show", action="store_true", help="打印前 10 条结果")
    p.set_defaults(func=cmd_crawl)

    # analyze
    p = sub.add_parser("analyze", help="分析页面结构(候选列表/分页/元数据)")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--full", action="store_true", help="输出完整简化 DOM 树与元数据")
    p.set_defaults(func=cmd_analyze)

    # requests
    p = sub.add_parser("requests", help="捕获页面发出的 XHR/fetch/WebSocket 请求")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--wait", type=float, default=5.0, help="捕获等待秒数")
    p.add_argument("--pattern", default=None, help="URL 正则过滤")
    p.add_argument("--limit", type=int, default=30, help="最多显示条数")
    p.add_argument("--dump", default=None, help="完整记录写入 JSONL 文件")
    p.set_defaults(func=cmd_requests)

    # serve
    p = sub.add_parser("serve", help="启动 FastAPI 调试服务")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    print(BANNER)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"执行失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
