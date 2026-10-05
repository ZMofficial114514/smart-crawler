"""验证视频下载插件的三项改进(以及框架 side 的半成品清理 bug 修复)。

覆盖真实事故场景:
  1. 正常 mp4            -> 通过完整性校验
  2. URL 以 .php 结尾的合法 mp4 -> 扩展名按魔数纠正为 .mp4 并通过
  3. 被截断的 mp4(无 moov) -> 校验拦下并删除, 不留残片
  4. 撞大小上限           -> 中断且**不留半成品**(这正是 512MiB 残片的成因)

不依赖外网: 用本地 HTTP 服务 + ffmpeg 生成的样本。
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
from smartcrawler.plugins.manager import PluginManager  # noqa: E402

PORT = 8971
FIX = PROJ / ".runtmp" / "verify_video"
OUT = PROJ / "data" / "plugin_output"
VIDEO_DIR = OUT / "video"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
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

    good = FIX / "good.mp4"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-c:a", "aac", "-t", "3", str(good)],
        check=True, timeout=180,
    )

    # 场景 2: 同一份合法视频, 但 URL 以 .php 结尾
    php_named = FIX / "clip.php"
    shutil.copy2(good, php_named)

    # 场景 3: 截断版(砍掉尾部 moov), 但文件头仍是合法 ftyp
    data = good.read_bytes()
    truncated = FIX / "truncated.mp4"
    truncated.write_bytes(data[: int(len(data) * 0.6)])

    return {"good": good, "php": php_named, "truncated": truncated}


def serve(directory: Path):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def run_case(plugin, settings, name: str, url: str, config: dict) -> list[DownloadedFile]:
    downloads: list[DownloadedFile] = []
    messages: list[tuple[str, str]] = []
    ctx = PluginContext(
        settings=settings,
        url=f"http://127.0.0.1:{PORT}/",
        page=None,
        notify=lambda lv, msg: messages.append((lv, msg)),
        config=config,
        downloads=downloads,
        output_dir=OUT,
    )
    await plugin.after_extract(ctx, [{"video": url}])
    print(f"  [{name}] 播报:")
    for lv, msg in messages:
        print(f"      [{lv}] {msg}")
    return downloads


def main() -> int:
    ff = find_ffmpeg()
    if not ff:
        print("没有 ffmpeg, 无法生成样本")
        return 1

    print("=" * 72)
    print("  视频下载插件 · 完整性校验验收")
    print("=" * 72)

    fx = build_fixtures(ff)
    print("\n  样本:")
    for k, v in fx.items():
        print(f"    {k:<10} {v.name:<16} {v.stat().st_size} 字节")

    srv = serve(FIX)
    settings = get_settings()
    mgr = PluginManager(settings)
    mgr.refresh()
    plugin = mgr.get("video-downloader")
    if plugin is None:
        print("找不到 video-downloader 插件")
        return 1

    base_config = {
        "item_field": "video",
        "dom_selector": "",          # 只用记录字段, 避免走 DOM
        "subdir": "video",
        "hls_enabled": False,
        "ffmpeg_path": "",
        "concurrency": 1,
        "max_files": 5,
        "ffmpeg_timeout_s": 120,
    }

    # ---------- 1. 正常 mp4 ----------
    print("\n[1] 正常 mp4 应通过校验")
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)
    cfg = dict(base_config, max_file_size_mb=64)
    ds = asyncio.run(run_case(plugin, settings, "good", f"http://127.0.0.1:{PORT}/good.mp4", cfg))
    ok = len(ds) == 1 and ds[0].ok and ds[0].size > 0
    check(ok, "下载成功且登记产物", f"{ds[0].filename if ds else '无'} / {ds[0].size if ds else 0} 字节")

    # ---------- 2. URL 以 .php 结尾 ----------
    print("\n[2] URL 以 .php 结尾的合法 mp4: 扩展名应按魔数纠正")
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)
    ds = asyncio.run(run_case(plugin, settings, "php", f"http://127.0.0.1:{PORT}/clip.php", cfg))
    if ds and ds[0].ok:
        ext = Path(ds[0].path).suffix.lower()
        check(ext == ".mp4", "扩展名被纠正为 .mp4(不是 .php)", ext)
        check(Path(ds[0].path).name == ds[0].filename, "记录里的文件名已同步", ds[0].filename)
    else:
        check(False, "扩展名被纠正为 .mp4(不是 .php)", f"下载未成功: {ds[0].error if ds else '无记录'}")

    # ---------- 3. 截断的 mp4 ----------
    print("\n[3] 被截断的 mp4(无 moov)应被拦下并删除")
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)
    ds = asyncio.run(run_case(plugin, settings, "truncated", f"http://127.0.0.1:{PORT}/truncated.mp4", cfg))
    if ds:
        check(not ds[0].ok, "被判定为失败", (ds[0].error or "")[:70])
        check("moov" in (ds[0].error or ""), "原因指明缺少 moov")
    else:
        check(False, "被判定为失败", "没有记录")
    left = list(VIDEO_DIR.glob("*")) if VIDEO_DIR.exists() else []
    check(not left, "磁盘上不留残片", f"残留 {[p.name for p in left]}" if left else "已清理")

    # ---------- 4. 撞大小上限: 直接验框架的半成品清理 ----------
    print("\n[4] 撞大小上限: 应中断且不留半成品(512MiB 残片的成因)")
    print("      直接以字节为单位调用 download_many, 精准复现旧 bug")
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)

    from smartcrawler.plugins.builtin._media import download_many  # noqa: PLC0415

    limit_bytes = 20000  # 远小于样本的 43798 字节
    dl: list[DownloadedFile] = []
    ctx = PluginContext(
        settings=settings,
        url=f"http://127.0.0.1:{PORT}/",
        page=None,
        notify=lambda lv, msg: print(f"      [{lv}] {msg}"),
        config={},
        downloads=dl,
        output_dir=OUT,
    )
    results = asyncio.run(
        download_many(
            ctx,
            [(f"http://127.0.0.1:{PORT}/good.mp4", None)],
            plugin_id="verify-toolarge",
            subdir="video",
            referer=f"http://127.0.0.1:{PORT}/",
            max_file_size=limit_bytes,
            concurrency=1,
            allowed_types=("video/", "application/octet-stream"),
        )
    )
    if results:
        check(not results[0].ok, "被判定为失败", (results[0].error or "")[:70])
        check("上限" in (results[0].error or ""), "原因指明撞了大小上限")
    left = list(VIDEO_DIR.glob("*")) if VIDEO_DIR.exists() else []
    check(not left, ">磁盘上没有留下半成品<", f"残留 {[(p.name, p.stat().st_size) for p in left]}" if left else "已清理")

    # 额外: 服务端声明 Content-Length 超限时应零字节落盘
    print("\n[5] 服务端 Content-Length 已超限: 应一个字节都不写")
    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)
    dl2: list[DownloadedFile] = []
    ctx2 = PluginContext(
        settings=settings, url=f"http://127.0.0.1:{PORT}/", page=None,
        notify=lambda lv, msg: None, config={}, downloads=dl2, output_dir=OUT,
    )
    res2 = asyncio.run(
        download_many(
            ctx2, [(f"http://127.0.0.1:{PORT}/good.mp4", None)],
            plugin_id="verify-precheck", subdir="video",
            referer=f"http://127.0.0.1:{PORT}/", max_file_size=10000,
            concurrency=1, allowed_types=("video/", "application/octet-stream"),
        )
    )
    if res2:
        check(not res2[0].ok, "被判定为失败", (res2[0].error or "")[:70])
        check("未开始下载" in (res2[0].error or ""), "走了 Content-Length 预检(未下载)", (res2[0].error or "")[:60])
    left2 = list(VIDEO_DIR.glob("*")) if VIDEO_DIR.exists() else []
    check(not left2, "磁盘上没有文件", f"残留 {[p.name for p in left2]}" if left2 else "无落盘")

    srv.shutdown()

    print("\n" + "=" * 72)
    if failures:
        print(f"验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("验收: 全部通过 ✓")
    print("=" * 72)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
