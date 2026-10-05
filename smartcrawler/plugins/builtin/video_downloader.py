"""视频下载器: 下载页面 ``<video>`` 元素或记录字段中的视频。

两种来源:

1. **mp4 / webm 直链** —— 直接用框架的 ``download_many`` 流式下载;
2. **m3u8 / mpd 流** —— 交给 ffmpeg 拉流并转封装(``-c copy``, 不重编码)。

关于第 2 点的取舍: 框架自带的下载器只会"流式 GET 然后写盘", 遇到 m3u8 会把播放
列表(几 KB 文本)当成功结果存下来 —— 这是静默的错误结果。因此这里显式分流:
检测到流地址就走 ffmpeg, 检测不到 ffmpeg 就**明确跳过并告警**, 绝不产出假文件。

落盘后的两重把关(都源于一次真实事故: 512MiB 的残片被当成下载成功):

- **扩展名按文件魔数判定**, 不再回退到 URL 路径 —— 否则 ``/foo.php?id=1`` 这类
  地址会把视频存成 ``.php``, 后续没人认得出来;
- **校验 MP4 完整性**(容器里必须有 ``moov`` 索引原子) —— 被截断的 mp4 文件头
  依然是合法的 ``ftyp``, 只看前几字节会误判成功。

ffmpeg 查找顺序: 插件配置 ``ffmpeg_path`` > 项目内 ``tools/ffmpeg/ffmpeg.exe``
(即启动器放进 PATH 的那份) > 系统 PATH。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from smartcrawler.config import PROJECT_ROOT
from smartcrawler.models import DownloadedFile
from smartcrawler.plugins.base import BasePlugin, PluginContext
from smartcrawler.plugins.builtin._media import (
    absolute_url,
    download_many,
    urls_from_dom,
)

#: 常见的视频字段名(用户规则里的命名各不相同, 这里做一次兜底尝试)
_VIDEO_FIELD_CANDIDATES = ("video", "video_url", "src", "media", "url")

#: 需要 ffmpeg 才能处理的流式容器
_STREAM_SUFFIXES = (".m3u8", ".mpd")

#: 扩展名与 MIME 的映射: 魔数命中后从这里取规范名
_MAGIC_EXT = {
    "mp4": (".mp4", "video/mp4"),
    "matroska": (".webm", "video/webm"),
    "mpegts": (".ts", "video/mp2t"),
    "flv": (".flv", "video/x-flv"),
    "avi": (".avi", "video/x-msvideo"),
    "ogg": (".ogv", "video/ogg"),
    "webp": (".webp", "image/webp"),
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
}

#: 每个文件最多扫多少字节找 ``moov``(先头后尾, 足够覆盖绝大多数 mp4)
_MOOV_SCAN_BUDGET = 6 * 1024 * 1024


def _find_ffmpeg(configured: str = "") -> Optional[str]:
    """定位可用的 ffmpeg 可执行文件。"""
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)
        found = shutil.which(configured)
        if found:
            return found
        return None

    bundled = PROJECT_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)

    return shutil.which("ffmpeg")


def _sniff_kind(path: Path, head: bytes) -> Optional[str]:
    """按文件魔数判断容器类型; 认不出返回 None。

    顺序有讲究: ``ftyp`` 出现在偏移 4(前面是 box size), 必须先于通用判断。
    """
    if len(head) < 12:
        return None
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        # Matroska 与 WebM 同族, 这里统一按 webm 扩展名落盘
        return "matroska"
    if head[0] == 0x47 and len(head) > 188 * 2 and head[188] == 0x47:
        return "mpegts"
    if head[:3] == b"FLV":
        return "flv"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


def _has_moov(path: Path, budget: int = _MOOV_SCAN_BUDGET) -> bool:
    """MP4 里是否存在 ``moov`` 原子(即索引是否写全)。

    先查头部再查尾部 —— ``moov`` 按 faststart 与否位于文件两端之一, 这两处覆盖了
    绝大多数真实文件, 因此不必为每个文件扫描整个 512MiB。
    """
    total = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(min(budget, total))
        if b"moov" in head:
            return True
        tail_size = min(budget, total)
        fh.seek(max(0, total - tail_size))
        return b"moov" in fh.read(tail_size)


def _resolve_renamed(path: Path) -> Path:
    """``_verify_downloaded`` 可能纠正过扩展名, 这里找回改名后的真实路径。

    优先返回原路径(未改名的情况); 找不到时按已知容器后缀逐个试探。
    """
    if path.exists():
        return path
    for ext, _mime in _MAGIC_EXT.values():
        candidate = path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return path


def _verify_downloaded(path: Path, budget: int = _MOOV_SCAN_BUDGET) -> Optional[str]:
    """校验落盘文件; 通过返回 ``None``, 不通过返回原因(并改好扩展名)。

    只做**廉价且高价值**的检查: 魔数是否可辨认、mp4 索引是否完整。
    不调用 ffprobe —— 那需要子进程与完整解码, 代价远高于收益。

    注意: 纠正扩展名后**必须继续做后续校验**, 不能提前返回 —— 否则
    ``/foo.php`` 这类地址会因为"改完名就放行"而跳过 moov 检查。
    """
    if not path.exists() or path.stat().st_size == 0:
        return "文件为空"

    with path.open("rb") as fh:
        head = fh.read(64)

    kind = _sniff_kind(path, head)
    if kind is None:
        return f"无法识别的文件类型(文件头 {head[:8].hex(' ')})"

    ext, _mime = _MAGIC_EXT[kind]

    # 扩展名纠偏: URL 以 .php/.asp 结尾时, 改回真实容器后缀
    if path.suffix.lower() != ext:
        fixed = path.with_suffix(ext)
        if fixed.exists():
            return f"目标扩展名已存在, 未能纠正 {path.name} -> {fixed.name}"
        try:
            path.rename(fixed)
        except OSError as exc:
            return f"扩展名纠正失败: {exc}"
        path = fixed  # 继续校验改名后的文件

    if kind == "mp4" and not _has_moov(path, budget):
        return "MP4 缺少 moov 索引(文件被截断, 无法播放)"

    return None


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
        streams = [u for u in unique if self._is_stream(u)]
        direct = [u for u in unique if u not in streams]

        if direct:
            await self._download_direct(ctx, direct[:limit], subdir)

        if streams:
            await self._handle_streams(ctx, streams[:limit], subdir)

        return items

    # ------------------------------------------------------------------
    @staticmethod
    def _is_stream(url: str) -> bool:
        from urllib.parse import urlparse

        return urlparse(url).path.lower().endswith(_STREAM_SUFFIXES)

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
            try:
                Path(record.path).unlink(missing_ok=True)
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

    async def _handle_streams(
        self, ctx: PluginContext, urls: list[str], subdir: str
    ) -> None:
        """m3u8/mpd: 交给 ffmpeg 拉流并转封装。"""
        if not bool(ctx.config.get("hls_enabled", True)):
            ctx.notify("WARNING", f"视频下载器: 跳过 {len(urls)} 个流式地址(已关闭 ffmpeg 处理)")
            return

        ffmpeg = _find_ffmpeg(str(ctx.config.get("ffmpeg_path") or ""))
        if not ffmpeg:
            ctx.notify(
                "WARNING",
                f"视频下载器: 发现 {len(urls)} 个 m3u8/mpd 流, 但找不到 ffmpeg, 已跳过",
            )
            return

        timeout = int(ctx.config.get("ffmpeg_timeout_s") or 600)
        for index, url in enumerate(urls):
            # 流式地址的扩展名一律用 ffmpeg 的输出容器(.mp4), 不看 URL 后缀
            target = ctx.download_path(subdir, f"stream_{index + 1}.mp4")
            await self._run_ffmpeg(ctx, ffmpeg, url, target, timeout)

    async def _run_ffmpeg(
        self, ctx: PluginContext, ffmpeg: str, url: str, target: Path, timeout: int
    ) -> None:
        """执行一次 ffmpeg 拉流; 成功则登记产物, 失败则清理半成品。"""
        record = DownloadedFile(url=url, plugin_id=self.id)

        # ``-headers`` / ``-user_agent`` 是 **http 协议的私有选项**, 只有当输入是
        # http(s) 时才存在。对本地文件(``file:`` 协议)加上它们会让 ffmpeg 直接报
        # "Option headers not found" 而失败, 所以必须按协议区分。
        input_opts: list[str] = []
        if url.lower().startswith(("http://", "https://")):
            # 流媒体站点普遍校验 Referer/UA, 不带就直接 403
            input_opts += ["-headers", f"Referer: {ctx.url}\r\n"]
            ua = str(ctx.settings.browser.user_agent or "").strip()
            if ua:
                input_opts += ["-user_agent", ua]

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            *input_opts,
            "-i", url,
            "-c", "copy",          # 不重编码, 只换容器
            "-bsf:a", "aac_adtstoasc",  # HLS 的 ADTS AAC 转 mp4 需要
            str(target),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise ValueError(f"ffmpeg 超时({timeout}s)") from None

            if proc.returncode != 0:
                detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
                raise ValueError(f"ffmpeg 退出码 {proc.returncode}: {detail[-1] if detail else '无输出'}")

            if not target.exists() or target.stat().st_size == 0:
                raise ValueError("ffmpeg 未产出文件(可能是加密流或需要鉴权)")

            # 与直链下载同等把关: ffmpeg 退出码为 0 也可能是残片(拉流中断等)
            problem = _verify_downloaded(target)
            if problem is not None:
                raise ValueError(f"完整性校验未通过: {problem}")

            record.path = str(target)
            record.filename = target.name
            record.size = target.stat().st_size
            record.mime_type = "video/mp4"
            record.ok = True
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其他
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            ctx.downloads.append(record)

        if record.ok:
            ctx.notify(
                "INFO",
                f"视频下载器: ffmpeg 合并完成 {record.filename} "
                f"({round((record.size or 0) / 1024 / 1024, 1)} MB)",
            )
        else:
            logger.warning(f"[video-downloader] ffmpeg 失败 {url}: {record.error}")
            ctx.notify("WARNING", f"视频下载器: ffmpeg 处理失败 -> {record.error}")


__all__ = ["VideoDownloaderPlugin"]
