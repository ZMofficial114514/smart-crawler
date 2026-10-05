"""
内置插件: 音乐/音频下载器(默认关闭)。

与图片下载器同源, 但多了三件音频场景特有的事:

1. **内联播放器解析**: 很多音乐页把真实音频地址藏在 ``<audio src>`` 或
   ``<source src>`` 里, 甚至写进 JS 变量, 因此默认同时尝试 DOM 选择器与常见字段名;
2. **流式地址分流**: 音乐站点越来越常把音频切成 HLS(``m3u8``)或 ``mpd`` 分片。这类
   地址交给框架的流式下载器只会把播放列表(几 KB 文本)当成功结果存下来 —— 文件名
   像音频, 内容是文本。这里检测到流式地址就走 ffmpeg 转封装, 找不到 ffmpeg 就明确
   跳过并告警, 绝不产出假文件;
3. **同名文件清理**: 同一首歌可能存在多个码率版本, 按 ``dedupe_by_stem`` 只保留
   体积最大的那个(通常就是最高码率), 避免目录里堆一堆重复音频。

落盘后统一把关(与视频下载器共用 :mod:`._media_stream`): 扩展名按**文件魔数**判定,
并校验 MP4 的 ``moov`` 索引原子 —— 被截断的 mp4 文件头依然是合法的 ``ftyp``, 只看
前几字节会误判成功。不通过校验的文件会被删除并标记失败。

⚠️ 合规提醒: 音乐/音频通常受版权保护。请仅在拥有授权(自有内容、已购买下载权、
公开授权素材库等)的前提下使用本插件。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, urls_from_dom, urls_from_items
from ._media_stream import download_streams, is_stream_url, resolve_renamed, verify_media_file
from ._media_transform import resolve_limit

#: 常见的音频字段名(用户规则里的命名各不相同, 这里做一次兜底尝试)
_AUDIO_FIELD_CANDIDATES = ("audio", "audio_url", "music", "music_url", "song", "song_url", "mp3", "media")

#: 各容器的落盘扩展名与 MIME(音频场景: MP4 容器落 ``.m4a`` 而不是 ``.mp4``)
_AUDIO_EXT: dict[str, str] = {
    "mp4": ".m4a",
    "m4a": ".m4a",
    "matroska": ".webm",
    "mpegts": ".ts",
    "ogg": ".ogg",
    "_mime": "audio/mp4",
}


class MusicDownloaderPlugin(BasePlugin):
    """下载记录或页面中的音频文件(默认关闭, 请确认拥有授权)。"""

    id = "music-downloader"
    name = "音乐下载器"
    description = "下载记录字段或页面 audio 元素中的音频文件; m3u8/mpd 流交给 ffmpeg 合并"
    version = "1.1.0"
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
            "key": "hls_enabled",
            "label": "用 ffmpeg 处理 m3u8/mpd 流",
            "type": "bool",
            "default": True,
            "description": (
                "音乐站点常把音频切成 HLS/mpd 分片。关闭则遇到流式地址直接跳过"
                "(不产出假的音频文件)"
            ),
        },
        {
            "key": "ffmpeg_path",
            "label": "ffmpeg 路径(留空自动查找)",
            "type": "str",
            "default": "",
            "description": "留空时依次查找 项目内 tools/ffmpeg 与系统 PATH",
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
            "description": (
                "⚠️ 这是截断点而不是护栏: 超过即中断并丢弃半成品, 不会留下"
                "「看着下好了却播不了」的残片"
            ),
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
        {
            "key": "ffmpeg_timeout_s",
            "label": "单个流处理超时(秒)",
            "type": "int",
            "default": 600,
            "min": 30,
            "max": 7200,
        },
        {
            "key": "sniff_network",
            "label": "从接口响应里嗅探下载地址",
            "type": "bool",
            "default": True,
            "description": (
                "**很多音乐站不把地址放在页面里** —— 页面没有 audio 元素、脚本里也没有可用"
                "地址, 只在某个接口的 JSON 里出现。打开后会在已捕获的网络响应里翻找疑似"
                "媒体地址, 并**逐个试探验证**(按实际返回的魔数判断), 只下载验证通过的。"
                "排除客户端安装包/图片/统计等噪声地址。"
            ),
        },
        {
            "key": "sniff_fields",
            "label": "嗅探关注字段",
            "type": "str",
            "default": "",
            "description": (
                "只在这些 JSON 字段名里找地址, 逗号分隔。留空 = 自动(按 url/song/album/"
                "author/name/play/download 等提示词加权, 并对 pic/lyric/comment 等降权)。"
                "例: url,playUrl,audioUrl,downloadUrl"
            ),
        },
        {
            "key": "sniff_max_probe",
            "label": "最多试探几个地址",
            "type": "int",
            "default": 12,
            "min": 1,
            "max": 100,
            "description": "接口噪声多时全试一遍既慢又像攻击, 默认只试分数最高的前 12 个",
        },
    ]

    async def _sniff_audio_urls(self, ctx: PluginContext, limit: int) -> list[str]:
        """从已捕获的接口响应里翻出并**验证**可用的音频地址。

        返回验证通过的地址列表(已去重、已按 limit 截断)。任何异常都吞掉 —— 嗅探是
        "锦上添花"的路径, 不能因为它失败而让本来能跑的页面提取挂掉。
        """
        crawler = getattr(ctx, "crawler", None)
        recorder = getattr(crawler, "recorder", None)
        records = list(getattr(recorder, "records", None) or [])
        if not records:
            ctx.notify("DEBUG", "音乐下载器: 没有网络记录可供嗅探(需先抓取页面)")
            return []

        fields = [s.strip() for s in str(ctx.config.get("sniff_fields") or "").split(",")
                  if s.strip()]
        try:
            import httpx  # noqa: PLC0415

            from ._media_sniff import find_candidates, probe_url  # noqa: PLC0415

            found = find_candidates(records, max_candidates=max(limit * 6, 24))
            # 用户点了名就只在这些字段里找
            if fields:
                lowered = [f.lower() for f in fields]
                found = [c for c in found
                         if any(f in (c.field or "").lower() for f in lowered)]
            if not found:
                ctx.notify("DEBUG", "音乐下载器: 接口响应里没有发现疑似音频地址")
                return []

            max_probe = int(ctx.config.get("sniff_max_probe") or 12)
            ctx.notify("INFO", f"音乐下载器: 接口里找到 {len(found)} 个疑似地址, "
                               f"开始试探前 {min(len(found), max_probe)} 个")
            verified: list[str] = []
            rejected = 0
            async with httpx.AsyncClient(follow_redirects=True,
                                         headers={"User-Agent": ctx.settings.browser.user_agent
                                                  or "Mozilla/5.0"}) as client:
                for cand in found[:max_probe]:
                    if len(verified) >= limit:
                        break
                    ok, why = await probe_url(client, cand.url, referer=ctx.url)
                    if ok:
                        verified.append(cand.url)
                        ctx.notify("INFO", f"音乐下载器: 验证通过 {cand.url[:80]} ({why})")
                    else:
                        rejected += 1
                        logger.debug(f"嗅探排除 {cand.url[:80]}: {why}")
            ctx.notify(
                "INFO" if verified else "WARNING",
                f"音乐下载器: 嗅探结果 —— 验证通过 {len(verified)} 个, 排除 {rejected} 个"
                + ("" if verified else "(站点的音频地址可能需登录/付费, 或不在这些接口里)"),
            )
            return verified
        except Exception as exc:  # noqa: BLE001 - 嗅探失败不影响其它取址路径
            logger.warning(f"网络嗅探失败({type(exc).__name__}): {exc}")
            ctx.notify("WARNING", f"音乐下载器: 网络嗅探失败({type(exc).__name__}), 已跳过")
            return []

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        configured = str(ctx.config.get("item_field") or "").strip()
        fields = [configured] if configured else list(_AUDIO_FIELD_CANDIDATES)
        # 任务参数优先: 抓取页的"下载数量"或抓取目标里的"前三首"是本次任务的意图。
        # 专用键 max_audio 排在通用键之前, 否则用户填了"最多 5 首"会被通用上限顶掉。
        limit = resolve_limit(ctx, 50, dedicated_keys=("max_audio",))
        subdir = str(ctx.config.get("subdir") or "music")
        max_bytes = int(ctx.config.get("max_file_size_mb") or 80) * 1024 * 1024

        collected: list[tuple[str, Optional[int]]] = []
        if items:
            for field in fields:
                for url, index in urls_from_items(items, field):
                    collected.append((absolute_url(ctx.url, url), index))

        selector = str(ctx.config.get("dom_selector") or "").strip()
        if selector:
            for raw in await urls_from_dom(ctx.page, selector, "src", limit=limit):
                collected.append((absolute_url(ctx.url, raw), None))

        # 内嵌网络抓包: 页面里找不到地址时, 从捕获的接口响应里翻。
        #
        # **很多音乐站就是这样**: 实测酷我音乐的搜索接口只返回 rid、播放页只请求歌词、
        # 点播放也不产生地址请求 —— 页面上根本没有可下的地址。这种站点只能靠
        # "翻接口 + 逐个试探验证"。详见 `._media_sniff` 的模块说明。
        if bool(ctx.config.get("sniff_network", True)):
            sniffed = await self._sniff_audio_urls(ctx, limit)
            collected.extend((u, None) for u in sniffed)

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

        # 分流: 流式播放列表必须走 ffmpeg, 否则只会存下一个文本清单
        streams = [url for url, _index in deduped if is_stream_url(url)]
        direct = [(url, index) for url, index in deduped if url not in streams]

        if direct:
            await self._download_direct(ctx, direct, subdir, max_bytes)

        if streams:
            await download_streams(
                ctx,
                streams,
                plugin_id=self.id,
                subdir=subdir,
                ext_for_kind=_AUDIO_EXT,
                default_ext=".m4a",
                hls_enabled=bool(ctx.config.get("hls_enabled", True)),
                ffmpeg_path=str(ctx.config.get("ffmpeg_path") or ""),
                timeout=int(ctx.config.get("ffmpeg_timeout_s") or 600),
                max_bytes=max_bytes,
            )

        if bool(ctx.config.get("dedupe_by_stem", True)):
            self._drop_smaller_duplicates(ctx, ctx.downloads)

        return items

    async def _download_direct(
        self,
        ctx: PluginContext,
        urls: list[tuple[str, Optional[int]]],
        subdir: str,
        max_bytes: int,
    ) -> None:
        """普通直链: 框架的并发流式下载 + 落盘后的容器/完整性校验。"""
        results = await download_many(
            ctx,
            urls,
            plugin_id=self.id,
            subdir=subdir,
            referer=ctx.url,
            max_file_size=max_bytes,
            concurrency=int(ctx.config.get("concurrency") or 3),
            allowed_types=("audio/", "video/mp4", "application/ogg", "application/octet-stream"),
            # 类型以落盘后的魔数为准: 服务端的 Content-Type 常与真实内容不符,
            # 只看响应头会把合法音频拒掉(详见 _media.download_many 的说明)
            verify_type_by_content=True,
        )

        ok = 0
        for record in results:
            if not record.ok or not record.path:
                continue
            problem = verify_media_file(Path(record.path), ext_for_kind=_AUDIO_EXT)
            if problem is None:
                actual = resolve_renamed(Path(record.path))
                record.path = str(actual)
                record.filename = actual.name
                record.size = actual.stat().st_size
                ok += 1
                continue
            record.ok = False
            record.error = f"完整性校验未通过: {problem}"
            # 扩展名可能被纠正过, 原路径与改名后的路径都要清
            for candidate in {Path(record.path), resolve_renamed(Path(record.path))}:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            record.path = ""
            record.size = 0
            logger.warning(f"[{self.id}] 丢弃不完整音频 {record.url}: {problem}")

        if ok:
            ctx.notify("INFO", f"音乐下载器: 直链成功 {ok}/{len(results)} 个文件(已校验完整性)")
        elif results:
            first = next((r.error for r in results if not r.ok), "未知")
            ctx.notify("WARNING", f"音乐下载器: 直链全部失败, 首个原因: {first}")

    @staticmethod
    def _drop_smaller_duplicates(ctx: PluginContext, results: list) -> None:
        """同名(去掉哈希后缀)文件只保留体积最大的那个。

        文件名形如 ``song_ab12cd34ef.mp3``, 这里按"去掉尾部哈希"的 stem 分组。
        """
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
