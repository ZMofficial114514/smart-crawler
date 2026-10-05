"""视频下载器: 下载页面 ``<video>`` 元素或记录字段中的视频。

两种来源:

1. **mp4 / webm 直链** —— 直接用框架的 ``download_many`` 流式下载;
2. **m3u8 / mpd 流** —— 交给 ffmpeg 拉流并转封装(``-c copy``, 不重编码)。

关于第 2 点的取舍: 框架自带的下载器只会"流式 GET 然后写盘", 遇到 m3u8 会把播放
列表(几 KB 文本)当成功结果存下来 —— 这是静默的错误结果。因此这里显式分流: 检测到
流式地址就走 ffmpeg, 检测不到 ffmpeg 就**明确跳过并告警**, 绝不产出假文件。

落盘后的两重把关(详见 :mod:`._media_stream`, 音频下载器复用同一套):

- **扩展名按文件魔数判定**, 不再回退到 URL 路径 —— 否则 ``/foo.php?id=1`` 这类
  地址会把视频存成 ``.php``, 后续没人认得出来;
- **校验 MP4 完整性**(容器里必须有 ``moov`` 索引原子) —— 被截断的 mp4 文件头
  依然是合法的 ``ftyp``, 只看前几字节会误判成功。

ffmpeg 查找顺序: 插件配置 ``ffmpeg_path`` > 项目内 ``tools/ffmpeg/ffmpeg.exe``
(即启动器放进 PATH 的那份) > 系统 PATH。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ...models import DownloadedFile
from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, urls_from_dom
from ._media_stream import (
    MOOV_SCAN_BUDGET,
    download_streams,
    is_stream_url,
    resolve_renamed,
    verify_media_file,
)
from ._media_stream import find_ffmpeg as _find_ffmpeg_impl

#: 常见的视频字段名(用户规则里的命名各不相同, 这里做一次兜底尝试)
_VIDEO_FIELD_CANDIDATES = ("video", "video_url", "src", "media", "url")

#: 各容器的落盘扩展名与 MIME(视频场景: mp4 容器落 ``.mp4``)
_VIDEO_EXT: dict[str, str] = {
    "mp4": ".mp4",
    "matroska": ".webm",
    "mpegts": ".ts",
    "flv": ".flv",
    "ogg": ".ogv",
}

#: 兼容旧引用/自检脚本: 每个文件最多扫多少字节找 ``moov``
_MOOV_SCAN_BUDGET = MOOV_SCAN_BUDGET


def _find_ffmpeg(configured: str = "") -> Optional[str]:
    """定位可用的 ffmpeg 可执行文件(自检脚本会直接调用这个名字)。"""
    return _find_ffmpeg_impl(configured)


def _resolve_renamed(path: Path) -> Path:
    """``_verify_downloaded`` 可能纠正过扩展名, 这里找回改名后的真实路径。"""
    return resolve_renamed(path)


def _verify_downloaded(path: Path, budget: int = MOOV_SCAN_BUDGET) -> Optional[str]:
    """校验落盘文件; 通过返回 ``None``, 不通过返回原因(并改好扩展名)。"""
    return verify_media_file(path, ext_for_kind=_VIDEO_EXT, budget=budget)


class VideoDownloaderPlugin(BasePlugin):
    """下载记录字段或页面 video 元素中的视频(流式地址走 ffmpeg)。"""

    id = "video-downloader"
    name = "视频下载器"
    description = "下载页面 video 元素或记录字段中的视频; m3u8/mpd 流交给 ffmpeg 合并"
    version = "1.0.0"
    author = ""
    category = "download"
    tags = ["视频", "mp4", "hls"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {
            "key": "item_field",
            "label": "记录中的视频字段",
            "type": "str",
            "default": "video",
            "description": "留空则自动尝试常见字段名(video/video_url/src/media/url)",
        },
        {
            "key": "dom_selector",
            "label": "页面视频选择器",
            "type": "str",
            "default": "video, video source",
            "description": "默认同时匹配 video 与其内部的 source",
        },
        {
            "key": "subdir",
            "label": "保存子目录",
            "type": "str",
            "default": "video",
        },
        {
            "key": "hls_enabled",
            "label": "用 ffmpeg 处理 m3u8/mpd 流",
            "type": "bool",
            "default": True,
            "description": "关闭则遇到流式地址直接跳过(不产出假文件)",
        },
        {
            "key": "ffmpeg_path",
            "label": "ffmpeg 路径(留空自动查找)",
            "type": "str",
            "default": "",
            "description": "留空时依次查找 项目内 tools/ffmpeg 与系统 PATH",
        },
        {
            "key": "max_file_size_mb",
            "label": "单文件大小上限(MB)",
            "type": "int",
            "default": 2048,
            "min": 1,
            "max": 8192,
            "description": (
                "⚠️ 这是截断点而不是护栏: 超过即中断并丢弃半成品。"
                "视频建议直接给足(默认 2048), 设太小会把长视频切成无法播放的残片"
            ),
        },
        {
            "key": "concurrency",
            "label": "并发下载数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 8,
            "description": "视频文件大, 建议不超过 2",
        },
        {
            "key": "max_files",
            "label": "单次任务最多下载",
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 200,
        },
        {
            "key": "ffmpeg_timeout_s",
            "label": "单个流处理超时(秒)",
            "type": "int",
            "default": 600,
            "min": 30,
            "max": 7200,
        },
    ]

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        configured = str(ctx.config.get("item_field") or "").strip()
        fields = [configured] if configured else list(_VIDEO_FIELD_CANDIDATES)
        limit = int(ctx.config.get("max_files") or 10)
        subdir = str(ctx.config.get("subdir") or "video")

        candidates: list[str] = []

        # 来源一: 提取出来的记录字段
        for item in items or []:
            if not isinstance(item, dict):
                continue
            for field in fields:
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    candidates.append(absolute_url(ctx.url, value.strip()))

        # 来源二: 页面上的 video / source 元素
        selector = str(ctx.config.get("dom_selector") or "").strip()
        if selector:
            for raw in await urls_from_dom(ctx.page, selector, "src", limit=limit * 2):
                candidates.append(absolute_url(ctx.url, raw))

        # 去重并保序
        seen: set[str] = set()
        unique: list[str] = []
        for url in candidates:
            if url and url not in seen:
                seen.add(url)
                unique.append(url)

        if not unique:
            ctx.notify("DEBUG", "视频下载器: 本页没有发现视频地址")
            return items

        # 分流: 流式容器 vs 普通直链
        streams = [u for u in unique if is_stream_url(u)]
        direct = [u for u in unique if u not in streams]

        if direct:
            await self._download_direct(ctx, direct[:limit], subdir)

        if streams:
            await download_streams(
                ctx,
                streams[:limit],
                plugin_id=self.id,
                subdir=subdir,
                ext_for_kind=_VIDEO_EXT,
                default_ext=".mp4",
                hls_enabled=bool(ctx.config.get("hls_enabled", True)),
                ffmpeg_path=str(ctx.config.get("ffmpeg_path") or ""),
                timeout=int(ctx.config.get("ffmpeg_timeout_s") or 600),
            )

        return items

    async def _download_direct(
        self, ctx: PluginContext, urls: list[str], subdir: str
    ) -> None:
        """普通直链: 复用框架的并发流式下载(带 Referer), 落盘后校验完整性。"""
        results = await download_many(
            ctx,
            [(u, None) for u in urls],
            plugin_id=self.id,
            subdir=subdir,
            referer=ctx.url,  # 防盗链必需
            max_file_size=int(ctx.config.get("max_file_size_mb") or 2048) * 1024 * 1024,
            concurrency=int(ctx.config.get("concurrency") or 2),
            allowed_types=("video/", "audio/", "application/octet-stream"),
            # 不信 Content-Type, 交给落盘后的魔数校验。理由: `/clip.php?id=1` 这类地址
            # 服务器会报 application/x-httpd-php, 而返回的其实是合法 mp4 —— 只看响应头
            # 会把好文件拒掉。魔数校验本来就是更强的那道关(见 _verify_downloaded)。
            verify_type_by_content=True,
        )

        # 落盘后把关: 框架按 HTTP 状态与字节数判定成功, 但被截断的容器**看起来
        # 也是成功的**(mp4 文件头依然合法)。这里补上魔数与索引校验, 并把不完整
        # 的文件连同记录一起标记为失败, 避免"下载成功却播不了"这种假成功。
        ok = 0
        for record in results:
            if not record.ok or not record.path:
                continue
            problem = _verify_downloaded(Path(record.path))
            if problem is None:
                # 扩展名可能刚被纠正(改名后路径变了), 同步回记录, 否则界面
                # 显示的仍是 URL 推断出来的旧名字(如 .php)
                actual = _resolve_renamed(Path(record.path))
                record.path = str(actual)
                record.filename = actual.name
                record.size = actual.stat().st_size
                ok += 1
                continue
            record.ok = False
            record.error = f"完整性校验未通过: {problem}"
            for candidate in {Path(record.path), _resolve_renamed(Path(record.path))}:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            record.path = ""
            record.size = 0
            logger.warning(f"[{self.id}] 丢弃不完整文件 {record.url}: {problem}")

        if ok:
            ctx.notify("INFO", f"视频下载器: 直链成功 {ok}/{len(results)} 个文件(已校验完整性)")
        elif results:
            first = next((r.error for r in results if not r.ok), "未知")
            ctx.notify("WARNING", f"视频下载器: 直链全部失败, 首个原因: {first}")


__all__ = ["VideoDownloaderPlugin"]
