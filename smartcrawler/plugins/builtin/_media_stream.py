"""
流式视频/音频地址(m3u8 / mpd)的公共实现 —— 视频下载器与音乐下载器共用。

**为什么需要单独一条路径**: 框架默认的下载器只会"流式 GET 然后写盘", 遇到 m3u8 会把
播放列表(几 KB 文本)当成成功结果存下来 —— 文件名看着像音频/视频, 内容是文本。
这是静默的错误结果, 比失败更糟, 所以这里显式分流: 检测到流式地址就走 ffmpeg,
检测不到 ffmpeg 就**明确跳过并告警**, 绝不产出假文件。

**落盘后的把关**(都源于一次真实事故: 512MiB 的残片被当成下载成功):

- **扩展名按文件魔数判定**, 不按 URL 后缀 —— 否则 ``/clip.php?id=1`` 这类地址会把
  媒体存成 ``.php``, 此后没人认得出来; 容器与目标扩展名不符时(如实际是 MKV 却按
  ``.m4a`` 落盘)也算失败, 因为播放器会照着扩展名去解析;
- **校验 MP4 的 ``moov`` 索引原子** —— 被截断的 mp4 文件头依然是合法的 ``ftyp``,
  只看前几字节会误判成功。

ffmpeg 查找顺序: 插件配置 ``ffmpeg_path`` > 项目内 ``tools/ffmpeg/ffmpeg.exe``
> 系统 PATH。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from loguru import logger

from ...config import PROJECT_ROOT
from ...models import DownloadedFile
from ..base import PluginContext

#: 需要 ffmpeg 才能处理的流式容器
STREAM_SUFFIXES = (".m3u8", ".mpd")

#: 魔数 -> (落盘扩展名, MIME)。扩展名一律按实际容器取, 不按 URL 猜。
MAGIC_EXT: dict[str, tuple[str, str]] = {
    "mp4": (".mp4", "video/mp4"),
    "m4a": (".m4a", "audio/mp4"),
    "mp3": (".mp3", "audio/mpeg"),
    "matroska": (".webm", "video/webm"),
    "mpegts": (".ts", "video/mp2t"),
    "flv": (".flv", "video/x-flv"),
    "avi": (".avi", "video/x-msvideo"),
    "ogg": (".ogg", "audio/ogg"),
    "flac": (".flac", "audio/flac"),
    "wav": (".wav", "audio/wav"),
    "webp": (".webp", "image/webp"),
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
}

#: 允许"直接改名放行"的容器。**故意收窄**: 图片魔数出现在音频/视频插件里说明下载到的
#: 根本不是媒体(常见于防盗链返回的占位图), 应当判失败而不是改名收下。
PLAYABLE_KINDS: frozenset[str] = frozenset(
    {"mp4", "m4a", "mp3", "matroska", "mpegts", "flv", "ogg", "flac", "wav"}
)

#: 每个文件最多扫多少字节找 ``moov``(先头后尾, 足够覆盖绝大多数 mp4)
MOOV_SCAN_BUDGET = 6 * 1024 * 1024


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
def find_ffmpeg(configured: str = "") -> Optional[str]:
    """定位可用的 ffmpeg 可执行文件。"""
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)
        found = shutil.which(configured)
        return found or None

    bundled = PROJECT_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)

    return shutil.which("ffmpeg")


def is_stream_url(url: str) -> bool:
    """URL 是否指向流式播放列表(看路径后缀, 忽略 query)。"""
    from urllib.parse import urlparse

    return urlparse(url).path.lower().endswith(STREAM_SUFFIXES)


# ---------------------------------------------------------------------------
# 落盘后的把关
# ---------------------------------------------------------------------------
def _id3_payload_size(head: bytes) -> int:
    """ID3v2 头里声明的标签体长度; 头不完整或非法时返回 0。

    ID3v2 的头是 10 字节, 其中第 6..9 字节是 **syncsafe** 长度 —— 每个字节只用低 7 位
    (最高位固定为 0), 因此 4 字节最多表示 2^28-1。按普通整数解析会把长度算错。
    """
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = 0
    for b in head[6:10]:
        if b & 0x80:
            return 0
        size = (size << 7) | b
    return size


def _mpeg_audio_frame_offset(head: bytes) -> int:
    """在头部里找 MPEG 音频帧同步字(``0xFF Ex/Fx``); 找不到返回 -1。

    为什么需要: **MP3 文件通常以 ID3v2 标签开头**, 所以第 0 字节不是帧同步字。
    网易云给的音频正是这种(实测文件头 ``49 44 33 04 00 00 00 00`` = ``ID3\\x04``)。
    只看前几字节会认不出来, 于是合法音频被当成"无法识别的文件类型"删掉。
    """
    start = 10 + _id3_payload_size(head) if head[:3] == b"ID3" else 0
    for i in range(start, max(start, len(head) - 1)):
        if head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0:
            # 排除保留值: MPEG1/2 Layer I/II/III 都符合, layer=00 不是有效帧
            layer = (head[i + 1] >> 1) & 0x03
            if layer != 0:
                return i
    return -1


def _is_riff(head: bytes, kind: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == kind


def sniff_kind(head: bytes) -> Optional[str]:
    """按文件魔数判断容器类型; 认不出返回 ``None``。

    顺序有讲究: ``ftyp`` 出现在偏移 4(前面是 box size), 必须先于通用判断。
    """
    if len(head) < 12:
        return None
    if head[4:8] == b"ftyp":
        # fMP4(音频/视频通用)带 m4a/mp4 brand; 老式 .m4a 是裸 MP4 容器 + ftpy, 此处
        # 统一交给调用方按"容器"判定, 不在魔数层区分音视频。
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        # Matroska 与 WebM 同族, 统一按 webm 扩展名落盘
        return "matroska"
    if head[0] == 0x47 and len(head) > 188 * 2 and head[188] == 0x47:
        return "mpegts"
    if head[:3] == b"FLV":
        return "flv"
    if _is_riff(head, b"AVI "):
        return "avi"
    if _is_riff(head, b"WAVE"):
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if _is_riff(head, b"WEBP"):
        return "webp"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    # MP3 放最后: 它的判据最宽松(只是帧同步字), 让前面那些确定性更高的先匹配,
    # 避免把别的格式误认成 mp3。
    if _mpeg_audio_frame_offset(head) >= 0:
        return "mp3"
    return None


def has_moov(path: Path, budget: int = MOOV_SCAN_BUDGET) -> bool:
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


def resolve_renamed(path: Path) -> Path:
    """校验过程可能纠正过扩展名, 这里找回改名后的真实路径。"""
    if path.exists():
        return path
    for ext, _mime in MAGIC_EXT.values():
        candidate = path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return path


def verify_media_file(
    path: Path,
    *,
    ext_for_kind: Optional[dict[str, str]] = None,
    budget: int = MOOV_SCAN_BUDGET,
) -> Optional[str]:
    """校验落盘文件; 通过返回 ``None``, 不通过返回原因(可能顺手改好扩展名)。

    只做**廉价且高价值**的检查: 魔数是否可辨认、容器是否在允许集合里、mp4 索引是否
    完整。不调用 ffprobe —— 那需要子进程与完整解码, 代价远高于收益。

    注意: 纠正扩展名后**必须继续做后续校验**, 不能提前返回 —— 否则 ``/foo.php``
    这类地址会因为"改完名就放行"而跳过 moov 检查。
    """
    if not path.exists() or path.stat().st_size == 0:
        return "文件为空"

    with path.open("rb") as fh:
        # 读 4KB 而不是 64 字节: MP3 往往以 ID3v2 标签开头, 而标签里可能带整张封面图
        # (几十到几百 KB), 帧同步字会落在前 64 字节之外。窗口太小会导致合法 mp3 被
        # 判成"无法识别的文件类型"。
        head = fh.read(4096)

    kind = sniff_kind(head)
    if kind is None and head[:3] == b"ID3":
        # MP3 的 ID3v2 标签可能带整张封面图(几十 KB 到几 MB), 帧同步字会落在首次读取的
        # 4KB 之外。只有"看起来是 ID3 但没找到帧同步字"时才按标签声明的长度补读一次 ——
        # 避免对每个文件都多读一遍。
        want = 10 + _id3_payload_size(head) + 8192
        if want > len(head):
            with path.open("rb") as fh:
                head = fh.read(min(want, 4 * 1024 * 1024))
            kind = sniff_kind(head)

    if kind is None:
        return f"无法识别的文件类型(文件头 {head[:8].hex(' ')})"

    if kind not in PLAYABLE_KINDS:
        # 常见于防盗链返回的占位图: 名字像媒体, 实际是图片
        return f"下载到的不是音视频容器(识别为 {kind})"

    # MP4 容器在音频场景落 .m4a、视频场景落 .mp4, 由调用方指定
    wanted = (ext_for_kind or {}).get(kind) or MAGIC_EXT[kind][0]

    if path.suffix.lower() != wanted:
        # 容器与扩展名不符时**不能只改名放行**: 实际是 MKV 却叫 .m4a 的话播放器会
        # 直接失败。只有"目标扩展名 + 内容"自洽才允许纠正。
        if not _ext_matches_kind(wanted, kind):
            return f"容器({kind})与扩展名({path.suffix or '无'})不符, 且无法纠正为 {wanted}"
        fixed = path.with_suffix(wanted)
        if fixed.exists():
            return f"目标扩展名已存在, 未能纠正 {path.name} -> {fixed.name}"
        try:
            path.rename(fixed)
        except OSError as exc:
            return f"扩展名纠正失败: {exc}"
        path = fixed  # 继续校验改名后的文件

    if kind in ("mp4", "m4a") and not has_moov(path, budget):
        return "MP4 缺少 moov 索引(文件被截断, 无法播放)"

    return None


def _ext_matches_kind(ext: str, kind: str) -> bool:
    """扩展名与容器是否自洽(允许 .m4a 承载 mp4 容器这类合理换名)。"""
    known = {e for e, _m in MAGIC_EXT.values()} | {".mka", ".m4a"}
    if ext not in known:
        return False
    if kind == "mp4":
        return ext in (".mp4", ".m4a")
    if kind == "matroska":
        return ext in (".webm", ".mka", ".mkv")
    return True


# ---------------------------------------------------------------------------
# 用 ffmpeg 拉流
# ---------------------------------------------------------------------------
async def download_streams(
    ctx: PluginContext,
    urls: list[str],
    *,
    plugin_id: str,
    subdir: str,
    ext_for_kind: Optional[dict[str, str]] = None,
    default_ext: str = ".mp4",
    hls_enabled: bool = True,
    ffmpeg_path: str = "",
    timeout: int = 600,
    max_bytes: Optional[int] = None,
) -> list[DownloadedFile]:
    """把 m3u8/mpd 流交给 ffmpeg 拉流并转封装(``-c copy``, 不重编码)。

    :param ext_for_kind: 各容器应有的落盘扩展名(音频把 mp4 容器落成 ``.m4a``)
    :param max_bytes: 超过即判失败并删除(``None`` 表示不设限)
    :returns: 每个流的下载记录(同时写入 ``ctx.downloads``)
    """
    if not urls:
        return []

    if not hls_enabled:
        ctx.notify("WARNING", f"发现 {len(urls)} 个流式地址, 但已关闭 ffmpeg 处理, 已跳过")
        return []

    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        ctx.notify(
            "WARNING",
            f"发现 {len(urls)} 个 m3u8/mpd 流, 但找不到 ffmpeg, 已跳过(不会产出假文件)",
        )
        return []

    ext_for_kind = dict(ext_for_kind or {})
    records: list[DownloadedFile] = []

    for index, url in enumerate(urls):
        # 流式地址的扩展名不看 URL 后缀, 用 ffmpeg 的输出容器
        target = ctx.download_path(subdir, f"stream_{index + 1}{default_ext}")
        record = DownloadedFile(url=url, plugin_id=plugin_id)
        try:
            await _run_ffmpeg(ctx, ffmpeg, url, target, timeout)

            if not target.exists() or target.stat().st_size == 0:
                raise ValueError("ffmpeg 未产出文件(可能是加密流或需要鉴权)")

            if max_bytes is not None and target.stat().st_size > max_bytes:
                raise ValueError(
                    f"文件超过大小上限(产物 {target.stat().st_size} 字节 > 上限 {max_bytes} 字节)"
                )

            # ffmpeg 退出码为 0 也可能是残片(拉流中断等), 与直链同等把关
            problem = verify_media_file(target, ext_for_kind=ext_for_kind)
            if problem is not None:
                raise ValueError(f"完整性校验未通过: {problem}")

            actual = resolve_renamed(target)
            record.path = str(actual)
            record.filename = actual.name
            record.size = actual.stat().st_size
            record.mime_type = (ext_for_kind or {}).get("_mime", "") or "video/mp4"
            record.ok = True
        except Exception as exc:  # noqa: BLE001 - 单个流失败不影响其他
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            for candidate in {target, resolve_renamed(target)}:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            record.path = ""
            record.size = 0
            logger.warning(f"[{plugin_id}] ffmpeg 失败 {url}: {record.error}")
        finally:
            records.append(record)
            ctx.downloads.append(record)

        if record.ok:
            ctx.notify(
                "INFO",
                f"ffmpeg 合并完成 {record.filename} "
                f"({round((record.size or 0) / 1024 / 1024, 1)} MB)",
            )
        else:
            ctx.notify("WARNING", f"ffmpeg 处理失败 -> {record.error}")

    return records


async def _run_ffmpeg(
    ctx: PluginContext, ffmpeg: str, url: str, target: Path, timeout: int
) -> None:
    """执行一次 ffmpeg 拉流并转封装; 失败抛异常(由调用方负责清理与记录)。"""
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
        "-c", "copy",  # 不重编码, 只换容器
        "-bsf:a", "aac_adtstoasc",  # HLS 的 ADTS AAC 转 mp4 需要
        str(target),
    ]
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
        raise ValueError(
            f"ffmpeg 退出码 {proc.returncode}: {detail[-1] if detail else '无输出'}"
        )
