"""音乐/音频下载器的流式地址验收(离线可跑, 不需要浏览器/外网)。

**这个脚本针对的原始故障**: 音乐站点(网易云这类)常把音频切成 HLS(``m3u8``)或
``mpd`` 分片。框架默认的下载器只会"流式 GET 然后写盘", 遇到 m3u8 会把播放列表
(几 KB 文本)当成成功结果存下来 —— 产出目录里出现一个**看着像音频、内容是文本**的
文件, 记录里还是"下载成功"。这比失败更糟: 失败会重试, 假成功不会。

验收点:

1. 普通 ``.m4a`` 直链照旧能下(改造不能碰坏原有路径);
2. **带 ID3v2 标签的 mp3 必须被认出来** —— 真实 mp3 绝大多数以 ``ID3`` 开头, 魔数表
   漏了它就会把已经下好的文件判成"无法识别"并删除(音乐站点直链下载的典型形态);
3. ``m3u8`` 地址必须交给 ffmpeg 合并成**可播放的音频容器**(这里用魔数 + ``moov``
   索引判定, 不调用 ffprobe);
4. 产物目录里**不允许出现 .m3u8 / 播放列表文本**;
5. ``hls_enabled=False`` 时明确跳过并告警, 同样不产出假文件。

用法: python scripts/verify_audio_stream.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from smartcrawler.config import get_settings  # noqa: E402
from smartcrawler.models import DownloadedFile  # noqa: E402
from smartcrawler.plugins.base import PluginContext  # noqa: E402
from smartcrawler.plugins.builtin._media_stream import sniff_kind  # noqa: E402
from smartcrawler.plugins.builtin.music_downloader import MusicDownloaderPlugin  # noqa: E402

PORT = 8973
FIX = PROJ / ".runtmp" / "verify_audio"
OUT = PROJ / "data" / "plugin_output"
MUSIC_DIR = OUT / "music"

failures: list[str] = []
total = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def find_ffmpeg() -> str | None:
    bundled = PROJ / "tools" / "ffmpeg" / "ffmpeg.exe"
    return str(bundled) if bundled.is_file() else shutil.which("ffmpeg")


def build_fixtures(ff: str) -> dict[str, Path]:
    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True, exist_ok=True)

    # 直接可下的 m4a 音频(2 秒正弦波)
    direct = FIX / "direct.m4a"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", "-b:a", "96k", str(direct)],
        check=True, timeout=180,
    )

    # mp3 直链: 绝大多数真实 mp3 以 ID3v2 标签开头 —— 魔数判定必须认它,
    # 否则已经下好的文件会被"无法识别"误杀(见文件头注释里的真实故障)。
    tagged_mp3 = FIX / "tagged.mp3"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=523:duration=2",
         "-c:a", "libmp3lame", "-b:a", "128k", str(tagged_mp3)],
        check=True, timeout=180,
    )

    # HLS 音频: 切成 1 秒的分片 + 播放列表(只留音频, 模拟音乐站点)
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
         "-c:a", "aac", "-b:a", "96k",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
         "-hls_segment_filename", str(FIX / "seg%d.ts"),
         str(FIX / "playlist.m3u8")],
        check=True, timeout=180,
    )

    return {
        "direct": direct,
        "tagged_mp3": tagged_mp3,
        "playlist": FIX / "playlist.m3u8",
    }


def serve(directory: Path):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_ctx(config: dict) -> tuple[PluginContext, list[DownloadedFile], list[tuple[str, str]]]:
    downloads: list[DownloadedFile] = []
    messages: list[tuple[str, str]] = []
    ctx = PluginContext(
        settings=get_settings(),
        url=f"http://127.0.0.1:{PORT}/",
        page=None,  # 不依赖浏览器: 只走"记录字段"这条来源
        notify=lambda lv, msg: messages.append((lv, msg)),
        config=config,
        downloads=downloads,
        output_dir=OUT,
    )
    return ctx, downloads, messages


def base_config(**over: object) -> dict:
    cfg = {
        "item_field": "audio",
        "dom_selector": "",
        "subdir": "music",
        "hls_enabled": True,
        "ffmpeg_path": "",
        "concurrency": 2,
        "max_file_size_mb": 40,
        "max_audio": 10,
        "dedupe_by_stem": True,
        "ffmpeg_timeout_s": 120,
    }
    cfg.update(over)
    return cfg


def clean_output() -> None:
    if MUSIC_DIR.exists():
        shutil.rmtree(MUSIC_DIR)


def listing() -> list[str]:
    if not MUSIC_DIR.exists():
        return []
    return sorted(p.name for p in MUSIC_DIR.iterdir() if p.is_file())


def audio_ok(path: Path) -> tuple[bool, str]:
    """产物是不是"能播的音频容器": 魔数可辨认 + mp4 类容器带 moov。"""
    if not path.exists() or path.stat().st_size == 0:
        return False, "文件为空"
    with path.open("rb") as fh:
        head = fh.read(64)
    kind = sniff_kind(head)
    if kind not in ("mp4", "m4a", "mp3", "matroska", "mpegts", "ogg"):
        return False, f"魔数不像音视频容器(识别为 {kind}, 头 {head[:8].hex(' ')})"
    if kind in ("mp4", "m4a"):
        tail = path.read_bytes()[-2 * 1024 * 1024:]
        head_all = path.read_bytes()[: 2 * 1024 * 1024]
        if b"moov" not in head_all and b"moov" not in tail:
            return False, "MP4 缺 moov 索引(被截断)"
    return True, kind


async def main() -> int:
    ff = find_ffmpeg()
    if not ff:
        print("没有 ffmpeg, 无法生成样本")
        return 1

    build_fixtures(ff)
    srv = serve(FIX)
    base = f"http://127.0.0.1:{PORT}"
    plugin = MusicDownloaderPlugin()

    print("=" * 72)
    print("  音乐/音频下载器 · 流式(m3u8)地址验收")
    print("=" * 72)

    try:
        # ==============================================================
        print("\n[1] 直链 .m4a 应照旧下载成功并落成音频")
        clean_output()
        ctx, downloads, msgs = make_ctx(base_config())
        await plugin.after_extract(ctx, [{"audio": f"{base}/direct.m4a"}])
        for lv, msg in msgs:
            print(f"      [{lv}] {msg}")
        files = listing()
        check(len(downloads) == 1 and downloads[0].ok, "直链记录为成功",
              str([(r.ok, r.error) for r in downloads]))
        check(len(files) == 1 and files[0].endswith(".m4a"), "产物是 1 个 .m4a",
              str(files))
        if files:
            ok, detail = audio_ok(MUSIC_DIR / files[0])
            check(ok, "产物是合法音频容器", detail)

        # ==============================================================
        print("\n[2] **带 ID3 标签的 mp3 直链必须放行(魔数识别回归)**")
        clean_output()
        ctx, downloads, msgs = make_ctx(base_config())
        await plugin.after_extract(ctx, [{"audio": f"{base}/tagged.mp3"}])
        for lv, msg in msgs:
            print(f"      [{lv}] {msg}")
        files = listing()
        check(len(downloads) == 1 and downloads[0].ok, "mp3 记录为成功",
              str([(r.ok, r.error) for r in downloads]))
        check(len(files) == 1, "**产物没有被误判删除**", str(files))
        if files:
            check(files[0].endswith(".mp3"), "落成 .mp3", files[0])
            with (MUSIC_DIR / files[0]).open("rb") as fh:
                head = fh.read(8)
            check(head[:3] == b"ID3", "样本确实是 ID3 开头的 mp3", head.hex(" "))
            ok, detail = audio_ok(MUSIC_DIR / files[0])
            check(ok, "**ID3 开头的 mp3 被识别为合法音频容器**", detail)

        # ==============================================================
        print("\n[3] m3u8 流应交给 ffmpeg 合并(而不是存下播放列表)")
        clean_output()
        ctx, downloads, msgs = make_ctx(base_config())
        await plugin.after_extract(ctx, [{"audio": f"{base}/playlist.m3u8"}])
        for lv, msg in msgs:
            print(f"      [{lv}] {msg}")
        files = listing()
        check(len(downloads) == 1 and downloads[0].ok, "流式记录为成功",
              str([(r.ok, r.error) for r in downloads]))
        check(
            not any(f.endswith((".m3u8", ".mpd", ".ts")) for f in files),
            "**产物里没有播放列表/分片文本(.m3u8/.mpd/.ts)**",
            str(files),
        )
        check(len(files) == 1, "只产出 1 个文件", str(files))
        if files:
            ok, detail = audio_ok(MUSIC_DIR / files[0])
            check(ok, "**合并结果能通过魔数 + moov 校验(可播放)**", detail)
            check(files[0].endswith((".m4a", ".mp4")), "落成 mp4 系容器(.m4a)",
                  files[0])

        # ==============================================================
        print("\n[4] hls_enabled=False: 明确跳过, 不产出任何假文件")
        clean_output()
        ctx, downloads, msgs = make_ctx(base_config(hls_enabled=False))
        await plugin.after_extract(ctx, [{"audio": f"{base}/playlist.m3u8"}])
        for lv, msg in msgs:
            print(f"      [{lv}] {msg}")
        check(listing() == [], "**没有产出假音频文件**", str(listing()))
        check(any("跳过" in m for _lv, m in msgs), "给出了跳过告警",
              str([m for _lv, m in msgs]))

        # ==============================================================
        print("\n[5] 播放列表混在记录里时, 直链与流式各走各的路")
        clean_output()
        ctx, downloads, msgs = make_ctx(base_config())
        await plugin.after_extract(
            ctx,
            [{"audio": f"{base}/direct.m4a"}, {"audio": f"{base}/playlist.m3u8"}],
        )
        files = listing()
        oks = [r for r in downloads if r.ok]
        check(len(oks) == 2, "两条地址都成功", f"{len(oks)}/{len(downloads)}")
        check(
            not any(f.endswith((".m3u8", ".mpd")) for f in files),
            "没有任何播放列表被当成音频存下来",
            str(files),
        )
        check(all(audio_ok(MUSIC_DIR / f)[0] for f in files), "两个产物都能播",
              str(files))
    finally:
        srv.shutdown()

    print("\n" + "=" * 72)
    if failures:
        print(f"音频流式验收: 未通过 ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"音频流式验收: 通过 ({total}/{total})")
    print("=" * 72)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
