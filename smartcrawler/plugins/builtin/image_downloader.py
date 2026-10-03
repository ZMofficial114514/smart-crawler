"""
内置插件: 图片下载器(**默认启用, 作为插件系统的参考实现**)。

它演示了一个插件能做的完整事情: 声明配置项 -> 读取提取结果 -> 并发下载 -> 登记产物。
默认开启是为了让用户一装上就能看到插件在起作用(抓取商品页时会顺手把商品图拉下来)。

两种取图方式, 可同时生效:
- ``item_field``: 从提取出的记录里取字段(默认 ``image``)。规则里把图片地址映射到该
  字段即可, 例如 ``{"name": "image", "selector": "img", "attribute": "src", "transform": ["url"]}``;
- ``dom_selector``: 直接在当前页面按 CSS 选择器抓 ``src``/``data-src``。
  适合"只想存图、不关心结构化字段"的场景。

**下载数量**的优先级是: 任务参数(抓取页的"下载数量"或抓取目标里的"前三张")
> 本插件配置 ``max_items`` > 内置默认。任务参数优先是必须的 —— 列表页给的往往只是
缩略图, 用户说"前三张"就是本次任务只要三张。
"""

from __future__ import annotations

from typing import Any, Optional

from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, urls_from_dom, urls_from_items
from ._media_transform import build_rules, resolve_limit, to_original


class ImageDownloaderPlugin(BasePlugin):
    """把页面/记录里的图片下载到本地, 适合做图库归档与离线预览。"""

    id = "image-downloader"
    name = "图片下载器"
    description = "下载记录字段或页面元素中的图片(默认插件, 可直接用来验证插件系统)"
    version = "1.1.0"
    author = "SmartCrawler"
    category = "download"
    tags = ["图片", "下载", "默认启用"]
    default_enabled = True

    config_schema: list[dict[str, Any]] = [
        {
            "key": "item_field",
            "label": "记录中的图片字段",
            "type": "str",
            "default": "image",
            "description": "从提取结果里取哪个字段当作图片地址; 留空则不按字段下载",
        },
        {
            "key": "dom_selector",
            "label": "页面图片选择器",
            "type": "str",
            "default": "img",
            "description": "可选。直接按 CSS 选择器抓取图片; 默认 img 覆盖页面主要图片",
        },
        {
            "key": "prefer_original",
            "label": "尽量下载原图",
            "type": "bool",
            "default": True,
            "description": (
                "列表页给的往往是缩略图。开启后按下面的规则把地址换成原图再下载"
            ),
        },
        {
            "key": "original_pairs",
            "label": "缩略图|原图 对照",
            "type": "text",
            "default": "",
            "description": (
                "每行一组『缩略图URL|原图URL』, 程序自动推出替换规则。"
                "例: .../a.thumb.400_0.jpeg|.../a.thumb.1000_0.jpeg"
            ),
        },
        {
            "key": "original_rules",
            "label": "原图替换规则(JSON)",
            "type": "text",
            "default": "",
            "description": (
                '可选, 直接写正则替换: [{"name":"堆糖","pattern":"400_0","replacement":"1000_0"}]'
            ),
        },
        {
            "key": "subdir",
            "label": "保存子目录",
            "type": "str",
            "default": "images",
            "description": "相对 data/plugin_output/ 的目录名",
        },
        {
            "key": "concurrency",
            "label": "并发下载数",
            "type": "int",
            "default": 6,
            "min": 1,
            "max": 32,
            "description": "过高容易被目标站点限速, 建议 4~8",
        },
        {
            "key": "max_file_size_mb",
            "label": "单文件大小上限(MB)",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 500,
        },
        {
            "key": "min_width",
            "label": "最小宽度(像素)",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 10000,
            "description": "0 表示不过滤。用于跳过 1x1 埋点/占位图",
        },
        {
            "key": "max_items",
            "label": "单次任务最多下载",
            "type": "int",
            "default": 200,
            "min": 1,
            "max": 5000,
            "description": "抓取页里的『下载数量』会覆盖这里的设置",
        },
    ]

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        """从记录字段与页面 DOM 两处收集图片地址并下载。"""
        field = str(ctx.config.get("item_field") or "").strip()
        selector = str(ctx.config.get("dom_selector") or "").strip()
        # 任务参数优先于插件配置: 用户在抓取页填的数量/目标里的"前三张"是本次任务意图
        limit = resolve_limit(ctx, 200)
        min_width = int(ctx.config.get("min_width") or 0)

        collected: list[tuple[str, Optional[int]]] = []

        if field:
            for url, index in urls_from_items(items or [], field):
                collected.append((absolute_url(ctx.url, url), index))

        if selector:
            for raw in await urls_from_dom(ctx.page, selector, "src", limit=limit):
                collected.append((absolute_url(ctx.url, raw), None))

        # 按 URL 去重并截断(同一张图可能在多个条目里重复出现)
        deduped: list[tuple[str, Optional[int]]] = []
        seen: set[str] = set()
        for url, index in collected:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append((url, index))
            if len(deduped) >= limit:
                break

        if min_width > 0:
            deduped = await self._filter_by_width(ctx, deduped, min_width)

        if not deduped:
            ctx.notify(
                "INFO",
                "图片下载器: 本页没有发现可下载的图片"
                + (f"(字段 {field!r} 为空" if field else "(")
                + (f", 选择器 {selector!r} 也没命中)" if selector else ")"),
            )
            return items

        # 缩略图 -> 原图
        deduped = self._prefer_original(ctx, deduped)

        results = await download_many(
            ctx,
            deduped,
            plugin_id=self.id,
            subdir=str(ctx.config.get("subdir") or "images"),
            referer=ctx.url,
            max_file_size=int(ctx.config.get("max_file_size_mb") or 20) * 1024 * 1024,
            concurrency=int(ctx.config.get("concurrency") or 6),
            allowed_types=("image/",),
        )
        failed = [r for r in results if not r.ok]
        if failed and len(failed) == len(results):
            # 全部失败是很强的信号(多半是防盗链/需要 Referer/被限速), 值得提示
            ctx.notify(
                "WARNING",
                f"图片下载器: {len(results)} 个图片全部下载失败, 首个原因: {failed[0].error}",
            )
        return items

    def _prefer_original(
        self, ctx: PluginContext, urls: list[tuple[str, Optional[int]]]
    ) -> list[tuple[str, Optional[int]]]:
        """把缩略图地址尽量换成原图地址。规则无效时原样保留。"""
        if not bool(ctx.config.get("prefer_original", True)):
            return urls

        rules = build_rules(ctx.config.get("original_rules"))
        # 用户填的"缩略图|原图"对照行
        pairs_raw = ctx.config.get("original_pairs") or ""
        if isinstance(pairs_raw, str) and pairs_raw.strip():
            pairs = []
            for line in pairs_raw.splitlines():
                line = line.strip()
                if "|" in line:
                    thumb, _, full = line.partition("|")
                    pairs.append((thumb.strip(), full.strip()))
            rules.extend(build_rules(None, auto_pairs=pairs))

        if not rules:
            return urls

        converted: list[tuple[str, Optional[int]]] = []
        hit_names: set[str] = set()
        changed = 0
        seen: set[str] = set()
        for url, index in urls:
            new_url, hit = to_original(url, rules)
            if hit and new_url != url:
                changed += 1
                hit_names.add(hit)
            if new_url in seen:  # 换过之后可能和别的条目撞车, 再去一次重
                continue
            seen.add(new_url)
            converted.append((new_url, index))

        if changed:
            ctx.notify(
                "INFO",
                f"图片下载器: 已把 {changed} 个缩略图地址换成原图"
                f"(规则: {', '.join(sorted(hit_names))})",
            )
        return converted

    @staticmethod
    async def _filter_by_width(
        ctx: PluginContext, urls: list[tuple[str, Optional[int]]], min_width: int
    ) -> list[tuple[str, Optional[int]]]:
        """按图片自然宽度过滤。

        只对**页面上已经存在的** img 有效(能直接从 DOM 读到 naturalWidth); 对纯 URL
        列表无法预检尺寸, 因此这些一律保留 —— 宁可多下几张, 也不要静默丢数据。
        """
        if ctx.page is None:
            return urls
        try:
            sizes = await ctx.page.evaluate(
                """() => {
                    const map = {};
                    for (const img of document.images) {
                        const src = img.currentSrc || img.src;
                        if (src) map[src] = Math.max(img.naturalWidth || 0, img.width || 0);
                    }
                    return map;
                }"""
            )
        except Exception:  # noqa: BLE001
            return urls

        kept: list[tuple[str, Optional[int]]] = []
        skipped = 0
        for url, index in urls:
            width = sizes.get(url)
            if width is None or width == 0 or width >= min_width:
                kept.append((url, index))
            else:
                skipped += 1
        if skipped:
            ctx.notify("INFO", f"图片下载器: 按最小宽度 {min_width}px 跳过了 {skipped} 张小图")
        return kept


__all__ = ["ImageDownloaderPlugin"]
