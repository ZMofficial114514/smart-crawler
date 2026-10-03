"""
插件系统验收: 真实下载图片 + 校验插件生命周期。

覆盖:
1. **图片下载器(默认插件)** —— 真实抓取 books.toscrape.com 并把商品图下到本地,
   校验文件确实存在、大小合理、是合法图片;
2. **示例用户插件** —— 校验 ``plugins/`` 目录下的用户插件被自动发现, 且
   ``after_extract`` 真的改写了数据;
3. **失败隔离** —— 故意注入一个会抛异常的插件, 确认抓取仍然成功、错误被记录;
4. **声明式插件** —— 校验 add_fields 与额外导出生效。

用法: python scripts/verify_plugins.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from smartcrawler.config import PROJECT_ROOT, get_settings  # noqa: E402
from smartcrawler.crawler import SmartCrawler  # noqa: E402
from smartcrawler.models import ExtractionRule, FieldSpec, ListRule, PaginationRule  # noqa: E402
from smartcrawler.plugins.base import BasePlugin, PluginContext  # noqa: E402
from smartcrawler.plugins.manager import PluginManager  # noqa: E402

TARGET = "https://books.toscrape.com"
OUTPUT_DIR = PROJECT_ROOT / "data" / "plugin_output"

# books.toscrape.com 的商品卡片: 图片在 .image_container img 的 src 上
RULE = ExtractionRule(
    mode="dom",
    list_rule=ListRule(
        item_selector="article.product_pod",
        fields=[
            FieldSpec(name="title", selector="h3 a", attribute="title", transform=["strip"]),
            FieldSpec(name="price", selector=".price_color", transform=["price"]),
            FieldSpec(
                name="image",
                selector=".image_container img",
                attribute="src",
                transform=["url"],
            ),
        ],
    ),
    pagination=PaginationRule(next_selector="li.next a", max_pages=1),
    source="manual",
    notes="插件验收用规则",
)

failures: list[str] = []
checks: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    checks.append(label)
    if not condition:
        failures.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# 测试用插件
# ---------------------------------------------------------------------------
class BoomPlugin(BasePlugin):
    """故意抛异常的插件, 用于验证失败隔离。"""

    id = "test-boom"
    name = "测试: 必崩插件"
    description = "永远抛异常, 用于验证插件失败不会拖垮抓取"
    category = "other"

    def after_extract(self, ctx: PluginContext, items):
        raise RuntimeError("这个插件故意崩溃了")


class MarkerPlugin(BasePlugin):
    """写入标记字段的插件, 用于验证 after_extract 的数据改写能力。"""

    id = "test-marker"
    name = "测试: 标记插件"
    description = "给每条记录写入一个标记字段"
    category = "cleanup"

    config_schema = [
        {"key": "value", "label": "标记值", "type": "str", "default": "marked"}
    ]

    def after_extract(self, ctx: PluginContext, items):
        value = ctx.config.get("value", "marked")
        for item in items or []:
            if isinstance(item, dict):
                item["plugin_marker"] = value
        return items


async def main() -> int:
    settings = get_settings()

    # ==================================================================
    print("=== 1) 插件发现 ===")
    manager = PluginManager(settings)
    found = {info.id: info for info in manager.list()}
    for info in manager.list():
        print(
            f"  · {info.id:<18} {info.name:<14} source={info.source:<8} "
            f"enabled={str(info.enabled):<5} hooks={','.join(info.hooks) or '-'}"
        )
    check("image-downloader" in found, "内置图片下载器已注册")
    check(found["image-downloader"].enabled, "图片下载器默认启用")
    check(found["image-downloader"].default_enabled, "图片下载器声明了 default_enabled")
    check("example-enrich" in found, "用户插件被自动发现(plugins/ 目录)")
    check(
        found.get("example-enrich", None) is not None and found["example-enrich"].source == "user",
        "示例插件来源标记为 user",
    )
    check("after_extract" in found["image-downloader"].hooks, "图片下载器实现了 after_extract")

    # ==================================================================
    print("\n=== 2) 真实图片下载(默认插件) ===")
    print(f"  目标: {TARGET}  输出: {OUTPUT_DIR / 'images'}")
    crawler = SmartCrawler(settings)
    progress: list[str] = []
    try:
        result = await crawler.crawl(
            TARGET,
            rule=RULE,
            max_pages=1,
            plugin_ids=["image-downloader"],
            on_progress=lambda level, msg: progress.append(f"[{level}] {msg}"),
        )
    finally:
        await crawler.close()

    print(f"  抓取: {result.item_count} 条, 耗时 {result.duration_ms:.0f}ms")
    for line in progress[:8]:
        print(f"    {line}")

    check(result.item_count > 0, "原本的数据提取仍然正常", f"{result.item_count} 条")
    check(bool(result.downloads), "插件产生了下载产物", f"{len(result.downloads)} 个")
    check("image-downloader" in result.plugins_used, "图片下载器被实际执行")

    files = [d for d in result.downloads if d.ok and d.path and Path(d.path).exists()]
    check(bool(files), "下载的文件确实落到了磁盘", f"{len(files)} 个")

    if files:
        sizes = [Path(d.path).stat().st_size for d in files]
        check(all(s > 100 for s in sizes), "文件大小合理(非空文件)", f"最小 {min(sizes)} 字节")
        # 校验文件头确实是图片
        sample = Path(files[0].path)
        head = sample.read_bytes()[:12]
        is_image = (
            head.startswith(b"\xff\xd8")  # JPEG
            or head.startswith(b"\x89PNG")  # PNG
            or head.startswith(b"GIF8")  # GIF
            or head[:4] == b"RIFF"  # WEBP
        )
        check(is_image, "文件内容是真实图片(魔数校验)", f"{sample.name} 头部={head[:4]!r}")
        check(all(d.mime_type.startswith("image/") for d in files), "MIME 类型为 image/*")

        total = sum(sizes)
        print(f"  下载明细(前 3 个):")
        for record in files[:3]:
            print(f"    {Path(record.path).name}  {record.size} 字节  {record.mime_type}")

    check(not result.plugin_errors, "图片下载过程没有插件错误", "; ".join(result.plugin_errors[:2]))

    # ==================================================================
    print("\n=== 3) 插件失败隔离 ===")
    crawler = SmartCrawler(settings)
    # 先让管理器完成一次发现, 否则随后的 list()/run_async() 会触发惰性加载,
    # 把这里手工注入的测试插件一并清掉(发现会重建整个注册表)。
    # 真实用户插件是磁盘上的文件, 不受影响; 这里注入的是内存对象, 所以必须先加载。
    crawler.plugins.ensure_loaded()
    for plugin in (BoomPlugin(), MarkerPlugin()):
        info = plugin.info()
        info.enabled = True
        info.config = {"value": "injected"} if info.id == "test-marker" else {}
        crawler.plugins._plugins[info.id] = plugin  # noqa: SLF001
        crawler.plugins._infos[info.id] = info  # noqa: SLF001

    print(
        "  已注册测试插件: "
        + ", ".join(f"{i.id}(enabled={i.enabled})" for i in crawler.plugins.list() if i.id.startswith("test-"))
    )
    try:
        result2 = await crawler.crawl(
            TARGET,
            rule=RULE,
            max_pages=1,
            plugin_ids=["test-boom", "test-marker"],
        )
    finally:
        await crawler.close()

    print(f"  抓取: {result2.item_count} 条; plugins_used={result2.plugins_used}")
    print(f"  plugin_errors({len(result2.plugin_errors)}): {result2.plugin_errors[:1]}")

    check(result2.item_count > 0, "有插件崩溃时抓取仍然成功", f"{result2.item_count} 条")
    check(bool(result2.plugin_errors), "崩溃被记录进 plugin_errors")
    if result2.plugin_errors:
        print(f"    记录: {result2.plugin_errors[0][:100]}")
    marked = [i for i in result2.items if i.get("plugin_marker") == "injected"]
    check(len(marked) == len(result2.items), "崩溃插件之后的插件仍被执行", f"{len(marked)}/{len(result2.items)}")

    # ==================================================================
    print("\n=== 4) 声明式插件(免代码扩展) ===")
    manager = PluginManager(settings)
    manager.set_config(
        "declarative",
        {
            "add_fields": json.dumps({"batch": "verify", "source": "books"}, ensure_ascii=False),
            "export_format": "jsonl",
        },
    )
    manager.set_enabled("declarative", True)

    crawler = SmartCrawler(settings)
    try:
        result3 = await crawler.crawl(
            TARGET, rule=RULE, max_pages=1, plugin_ids=["declarative"]
        )
    finally:
        await crawler.close()

    check(result3.item_count > 0, "声明式插件下抓取正常", f"{result3.item_count} 条")
    has_fields = all(
        i.get("batch") == "verify" and i.get("source") == "books" for i in result3.items
    )
    check(has_fields, "add_fields 已补充到每条记录")
    exports = list((OUTPUT_DIR / "exports").glob("export_*.jsonl")) if (OUTPUT_DIR / "exports").exists() else []
    check(bool(exports), "额外导出 jsonl 已生成", f"{len(exports)} 个文件")

    # 复原声明式插件状态, 避免影响后续使用
    manager.set_enabled("declarative", False)
    manager.reset_config("declarative")

    # ==================================================================
    print("\n=== 5) 用户插件(示例 enrich) ===")
    manager = PluginManager(settings)
    manager.set_enabled("example-enrich", True)
    crawler = SmartCrawler(settings)
    try:
        result4 = await crawler.crawl(
            TARGET, rule=RULE, max_pages=1, plugin_ids=["example-enrich"]
        )
    finally:
        await crawler.close()
    enriched = [i for i in result4.items if i.get("source_domain") == "books.toscrape.com"]
    check(len(enriched) == len(result4.items), "用户插件改写了数据", f"{len(enriched)}/{len(result4.items)}")
    check(all("crawled_at" in i for i in result4.items), "用户插件补充了时间戳")
    manager.set_enabled("example-enrich", False)

    # ==================================================================
    print("\n" + "=" * 64)
    print(f"共 {len(checks)} 项检查")
    if failures:
        print(f"插件验收: 未通过 ✗ ({len(failures)} 项)")
        for item in failures:
            print(f"  - {item}")
    else:
        print("插件验收: 通过 ✓")
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
