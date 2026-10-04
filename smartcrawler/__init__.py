"""
SmartCrawler —— 智能爬虫框架。

核心能力:
- Playwright 浏览器自动化(Chromium/Firefox/WebKit, 无头可配, 指纹伪装, 并发控制)
- XHR/fetch/WebSocket 全量监听与 JSON 自动解析
- DOM 结构自动分析(列表/分页/元数据)与唯一选择器生成
- AI 辅助: 自然语言 -> 提取规则 / JSON 字段映射 / 选择器自愈 / 数据清洗
  (OpenAI 兼容接口: DeepSeek/通义/智谱/Moonshot 等, 或本地 Ollama; 离线降级到规则引擎)
- 生产级: 代理池 / 随机 UA / 限速 / 重试 / 增量去重 / 多格式存储 / FastAPI 调试接口

⚠️ 合规: 默认遵守 robots.txt、默认限速, 仅限用于合法授权的数据采集场景。

快速上手:
    import asyncio
    from smartcrawler import SmartCrawler

    async def main():
        async with SmartCrawler() as crawler:
            result = await crawler.crawl(
                "https://books.toscrape.com",
                goal="抓取所有书籍的名称、价格和链接",
                format="csv",
                output="books.csv",
            )
            print(result.item_count, result.saved_to)

    asyncio.run(main())
"""

from typing import Any

#: **唯一的版本号来源**。其它地方(Web 接口、健康检查、User-Agent)一律从这里读,
#: 避免出现"包说 0.1.0、健康检查说 0.2.0"这种自相矛盾的情况 —— 这真的发生过。
#: 改动版本时只改这一行, 见 CHANGELOG.md。
__version__ = "1.0.0"
__all__ = [
    "SmartCrawler",
    "Settings",
    "get_settings",
    "ExtractionRule",
    "FieldSpec",
    "ListRule",
    "PaginationRule",
    "PageStructureReport",
    "TaskResult",
    "Storage",
]

# 惰性导入: 避免在只用到轻量模块(如 config/models/ai)时强制加载 playwright
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "SmartCrawler": ("smartcrawler.crawler", "SmartCrawler"),
    "run_crawl": ("smartcrawler.crawler", "run_crawl"),
    "Settings": ("smartcrawler.config", "Settings"),
    "get_settings": ("smartcrawler.config", "get_settings"),
    "Storage": ("smartcrawler.storage", "Storage"),
    "ExtractionRule": ("smartcrawler.models", "ExtractionRule"),
    "FieldSpec": ("smartcrawler.models", "FieldSpec"),
    "ListRule": ("smartcrawler.models", "ListRule"),
    "PaginationRule": ("smartcrawler.models", "PaginationRule"),
    "PageStructureReport": ("smartcrawler.models", "PageStructureReport"),
    "TaskResult": ("smartcrawler.models", "TaskResult"),
    "NetworkRecord": ("smartcrawler.models", "NetworkRecord"),
}


def __getattr__(name: str) -> Any:  # PEP 562 模块级 __getattr__
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    raise AttributeError(f"module 'smartcrawler' has no attribute {name!r}")
