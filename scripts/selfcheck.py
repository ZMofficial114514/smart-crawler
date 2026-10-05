"""安装自检 + 插件端到端验证。

用法:
    python selfcheck.py          # 只做环境/依赖/插件发现自检
    python selfcheck.py --e2e    # 额外跑一次真实的 HLS 合并(不依赖外网)
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import traceback
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

E2E = "--e2e" in sys.argv

print("=" * 62)
print("Python:", sys.version.split()[0], "| exe:", sys.executable)
print("项目根:", PROJ)
print("=" * 62)

# ---------------------------------------------------------------- 1 依赖
print("\n[1] 依赖导入")
for mod in ("playwright", "pydantic", "pydantic_settings", "fastapi",
            "uvicorn", "httpx", "loguru", "yaml", "dotenv"):
    try:
        m = __import__(mod)
        print(f"  OK   {mod:<18} {getattr(m, '__version__', '?')}")
    except Exception as exc:
        print(f"  FAIL {mod:<18} {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 2 框架
print("\n[2] 框架导入")
from smartcrawler.config import PROJECT_ROOT, get_settings          # noqa: E402
from smartcrawler import SmartCrawler                                # noqa: E402

settings = get_settings()
print(f"  OK   SmartCrawler 已导入")
print(f"  OK   PROJECT_ROOT = {PROJECT_ROOT}")
print(f"  OK   Settings(headless={settings.browser.headless}, "
      f"respect_robots={settings.anti_spider.respect_robots})")

# ---------------------------------------------------------------- 3 插件
print("\n[3] 插件发现")
from smartcrawler.plugins.manager import PluginManager, USER_PLUGIN_DIR  # noqa: E402

print(f"  用户插件目录: {USER_PLUGIN_DIR}")
print(f"  目录下 .py  : {[f.name for f in sorted(USER_PLUGIN_DIR.glob('*.py'))]}")

mgr = PluginManager(settings)
infos = mgr.refresh()
print(f"\n  共发现 {len(infos)} 个插件:")
for i in infos:
    tag = "启用" if i.enabled else "关闭"
    extra = f"   <== 加载失败: {i.load_error}" if i.load_error else ""
    print(f"    [{tag}] {i.name:<18} id={i.id:<20} source={i.source}{extra}")

target = mgr.get_info("video-downloader")
if not target:
    print("\n  !! 没找到 video-downloader")
    sys.exit(1)

print(f"\n  视频插件:")
print(f"    version  = {target.version}")
print(f"    source   = {target.source}")
print(f"    hooks    = {target.hooks}")
print(f"    load_error = {target.load_error}")
print(f"    配置项   = {[f.key for f in target.config_schema]}")

# ---------------------------------------------------------------- 4 浏览器
print("\n[4] 浏览器内核")
try:
    from smartcrawler.web.__main__ import ensure_browsers_path, prepare_temp_dir
    prepare_temp_dir()
    print(f"  OK   PLAYWRIGHT_BROWSERS_PATH = {ensure_browsers_path()}")
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.launch(headless=True)
    print(f"  OK   Chromium 启动成功, 版本 {b.version}")
    b.close()
    p.stop()
except Exception:
    print("  FAIL 浏览器自检失败:")
    traceback.print_exc()

# ---------------------------------------------------------------- 5 ffmpeg
print("\n[5] ffmpeg")
from smartcrawler.plugins.builtin.video_downloader import _find_ffmpeg  # noqa: E402

ff = _find_ffmpeg()
if ff:
    out = subprocess.run([ff, "-hide_banner", "-version"],
                         capture_output=True, text=True, timeout=30)
    print(f"  OK   找到: {ff}")
    print(f"       {out.stdout.splitlines()[0] if out.stdout else '?'}")
else:
    print("  WARN 没找到 ffmpeg —— m3u8/mpd 流将无法处理")

# ---------------------------------------------------------------- 6 e2e
if E2E:
    print("\n[6] 端到端: 插件真实合并一个 HLS 流")
    if not ff:
        print("  跳过(没有 ffmpeg)")
        sys.exit(1)

    tmp = PROJECT_ROOT / ".runtmp" / "plugin_e2e"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "in.mp4"
    m3u8 = tmp / "play.m3u8"

    # 用 ffmpeg 造一个 3 秒测试视频并切成 HLS 分片
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-c:a", "aac", "-t", "3", str(src)],
                   check=True, timeout=120)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(src), "-c", "copy", "-f", "hls",
                    "-hls_time", "1", "-hls_list_size", "0", str(m3u8)],
                   check=True, timeout=120)
    print(f"  已生成测试流: {m3u8.name} ({len(list(tmp.glob('*.ts')))} 个分片)")

    from smartcrawler.plugins.base import PluginContext
    from smartcrawler.models import DownloadedFile

    messages: list[tuple[str, str]] = []
    downloads: list[DownloadedFile] = []
    out_dir = PROJECT_ROOT / "data" / "plugin_output"

    ctx = PluginContext(
        settings=settings,
        url="http://localhost/fake",
        page=None,
        notify=lambda level, msg: messages.append((level, msg)),
        config=dict(target.config),
        downloads=downloads,
        output_dir=out_dir,
    )

    plugin = mgr.get("video-downloader")
    # 通过记录字段传入 m3u8 地址, 走插件的流式分支
    items = [{"video": str(m3u8)}]
    asyncio.run(plugin.after_extract(ctx, items))

    print("  插件播报:")
    for lv, msg in messages:
        print(f"    [{lv}] {msg}")
    print(f"  登记产物: {len(downloads)} 个")
    ok = False
    for d in downloads:
        print(f"    ok={d.ok} size={d.size} file={d.filename} err={d.error}")
        if d.ok and d.size and d.size > 0:
            ok = True
    print()
    print("  >>> 端到端结果:", "通过 —— 插件成功把 m3u8 合并成 mp4" if ok else "失败 <<<")

print("\n" + "=" * 62)
print("自检结束")
