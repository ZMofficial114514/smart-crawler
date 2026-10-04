"""
内置插件: 音乐/音频下载器(默认关闭)。

与图片下载器同源, 但多了两件音频场景特有的事:

1. **内联播放器解析**: 很多音乐页把真实音频地址藏在 ``<audio src>`` 或
   ``<source src>`` 里, 甚至写进 JS 变量, 因此默认同时尝试 DOM 选择器与常见字段名;
2. **同名文件清理**: 同一首歌可能存在多个码率版本, 按 ``dedupe_by_stem`` 只保留
   体积最大的那个(通常就是最高码率), 避免目录里堆一堆重复音频。

⚠️ 合规提醒: 音乐/音频通常受版权保护。请仅在拥有授权(自有内容、已购买下载权、
公开授权素材库等)的前提下使用本插件。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, urls_from_dom, urls_from_items
from ._media_transform import resolve_limit

#: 常见的音频字段名(用户规则里的命名各不相同, 这里做一次兜底尝试)
_AUDIO_FIELD_CANDIDATES = ("audio", "audio_url", "music", "music_url", "song", "song_url", "mp3", "media")


class MusicDownloaderPlugin(BasePlugin):
    """下载记录或页面中的音频文件(默认关闭, 请确认拥有授权)。"""

    id = "music-downloader"
    name = "音乐下载器"
    description = "下载记录字段或页面 audio 元素中的音频文件(使用前请确认版权授权)"
    version = "1.0.0"
    author = "SmartCrawler"
    category = "download"
    tags = ["音频", "音乐", "需授权"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {
            "key": "item_field",
            "label": "记录中的音频字段",
            "type": "str",
            "default": "audio",
            "description": "留空则自动尝试常见字段名(audio/music/song/mp3 等)",
        },
        {
            "key": "dom_selector",
            "label": "页面音频选择器",
            "type": "str",
            "default": "audio, audio source",
            "description": "默认同时匹配 audio 与其内部的 source",
        },
        {
            "key": "subdir",
            "label": "保存子目录",
            "type": "str",
            "default": "music",
        },
        {
            "key": "concurrency",
            "label": "并发下载数",
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 16,
            "description": "音频文件较大, 建议不超过 4",
        },
        {
            "key": "max_file_size_mb",
            "label": "单文件大小上限(MB)",
            "type": "int",
            "default": 80,
            "min": 1,
            "max": 2048,
        },
        {
            "key": "max_audio",
            "label": "最多下载音频数",
            "type": "int",
            "default": 50,
            "min": 1,
            "max": 1000,
            "description": (
                "本次任务最多下载多少个音频文件。优先级: 抓取页的「下载数量」> 抓取目标里的"
                "数量说法(如「前 3 首」)> 本项 > 通用上限。"
            ),
        },
        {
            "key": "dedupe_by_stem",
            "label": "同名只留最大文件",
            "type": "bool",
            "default": True,
            "description": "同一首歌常有多个码率版本, 开启后只保留体积最大的",
        },
    ]

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        configured = str(ctx.config.get("item_field") or "").strip()
        fields = [configured] if configured else list(_AUDIO_FIELD_CANDIDATES)
        # 任务参数优先: 抓取页的"下载数量"或抓取目标里的"前三首"是本次任务的意图。
        # 专用键 max_audio 排在通用键之前, 否则用户填了"最多 5 首"会被通用上限顶掉。
        limit = resolve_limit(ctx, 50, dedicated_keys=("max_audio",))
        subdir = str(ctx.config.get("subdir") or "music")

        collected: list[tuple[str, Optional[int]]] = []
        if items:
            for field in fields:
                for url, index in urls_from_items(items, field):
                    collected.append((absolute_url(ctx.url, url), index))

        selector = str(ctx.config.get("dom_selector") or "").strip()
        if selector:
            for raw in await urls_from_dom(ctx.page, selector, "src", limit=limit):
                collected.append((absolute_url(ctx.url, raw), None))

        deduped: list[tuple[str, Optional[int]]] = []
        seen: set[str] = set()
        for url, index in collected:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append((url, index))
            if len(deduped) >= limit:
                break

        if not deduped:
            ctx.notify("DEBUG", "音乐下载器: 本页没有发现音频地址")
            return items

        results = await download_many(
            ctx,
            deduped,
            plugin_id=self.id,
            subdir=subdir,
            referer=ctx.url,
            max_file_size=int(ctx.config.get("max_file_size_mb") or 80) * 1024 * 1024,
            concurrency=int(ctx.config.get("concurrency") or 3),
            allowed_types=("audio/", "video/mp4", "application/ogg", "application/octet-stream"),
        )

        if bool(ctx.config.get("dedupe_by_stem", True)):
            self._drop_smaller_duplicates(ctx, results)

        return items

    @staticmethod
    def _drop_smaller_duplicates(ctx: PluginContext, results: list) -> None:
        """同名(去掉哈希后缀)文件只保留体积最大的那个。

        文件名形如 ``song_ab12cd34ef.mp3``, 这里按"去掉尾部哈希"的 stem 分组。
        """
        from pathlib import Path

        groups: dict[str, list] = defaultdict(list)
        for record in results:
            if not record.ok or not record.filename:
                continue
            stem = Path(record.filename).stem
            # 去掉 ``_xxxxxxxxxx`` 形式的去重后缀
            base = stem.rsplit("_", 1)[0] if "_" in stem else stem
            groups[base].append(record)

        removed = 0
        for records in groups.values():
            if len(records) < 2:
                continue
            records.sort(key=lambda r: r.size, reverse=True)
            for record in records[1:]:
                try:
                    Path(record.path).unlink(missing_ok=True)
                    record.ok = False
                    record.error = f"同名文件中已有更大版本({records[0].size} 字节), 已清理"
                    removed += 1
                except OSError:
                    pass
        if removed:
            ctx.notify("INFO", f"音乐下载器: 清理了 {removed} 个同名较小文件")


__all__ = ["MusicDownloaderPlugin"]
