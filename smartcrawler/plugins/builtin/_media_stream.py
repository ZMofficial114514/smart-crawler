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
    # 绝大多数真实 mp3 以 ID3v2 标签开头; 无标签的裸帧也要认(见 sniff_kind)
    "mp3": (".mp3", "audio/mpeg"),
    "matroska": (".webm", "video/webm"),
    "mpegts": (".ts", "video/mp2t"),
    "flv": (".flv", "video/x-flv"),
    "avi": (".avi", "video/x-msvideo"),
    "ogg": (".ogg", "audio/ogg"),
    "webp": (".webp", "image/webp"),
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
}

#: 允许"直接改名放行"的容器。**故意收窄**: 图片魔数出现在音频/视频插件里说明下载到的
#: 根本不是媒体(常见于防盗链返回的占位图), 应当判失败而不是改名收下。
PLAYABLE_KINDS: frozenset[str] = frozenset(
    {"mp4", "m4a", "mp3", "matroska", "mpegts", "flv", "ogg"}
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
def sniff_kind(head: bytes) -> Optional[str]:
    """按文件魔数判断容器类型; 认不出返回 ``None``。

    顺序有讲究: ``ftyp`` 出现在偏移 4(前面是 box size), 必须先于通用判断。

    mp3 有两种开头, 必须都认 —— 漏掉 ID3 那条会把**绝大多数真实 mp3** 判成
    "无法识别", 于是完整性校验把已经下好的文件删掉(音乐站点直链下载的典型形态
    就是带 ID3 标签的 mp3)。
    """
    # ID3v2 标签: 'ID3' + 版本 + 标志 + 同步安全长度。真实发行的 mp3 基本都带。
    if head[:3] == b"ID3":
        return "mp3"
    # 裸 MPEG 音频帧同步字: 11 个 1(0xFF 后高 3 位全 1)后跟层/版本位
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
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
        head = fh.read(64)

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
    if kind == "mp3":
        # 裸 MPEG 音频流常挂在 .bin / .php / 无扩展名 的地址上, 内容本身就是 mp3,
        # 这类应当改名放行 —— 与"MKV 却叫 .m4a"那种表里不一是两回事。
        return True
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
