"""端到端: 网易云插件的实测(抓列表 -> 换音频直链 -> 下载 -> 校验可播放)。"""

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8322"
URL = "https://music.163.com/#/search/m/?s=on%20my%20way&type=1"
OUT = pathlib.Path("data/plugin_output/music")
FFPROBE = pathlib.Path("tools/ffmpeg/ffprobe.exe")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def api(path: str, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    import shutil

    shutil.rmtree(OUT, ignore_errors=True)
    print("=== 提交抓取任务(目标: 抓取歌曲名称与链接) ===")
    tid = (api("/api/crawl", {"url": URL, "goal": "抓取歌曲名称与链接",
                              "max_pages": 1}, "POST").get("task") or {}).get("id")
    print(f"    task = {tid}")
    for _ in range(200):
        time.sleep(2)
        d = api(f"/api/tasks/{tid}")
        if d.get("status") in ("success", "failed", "cancelled"):
            break
    res = d.get("result") or {}
    print(f"    状态={d.get('status')}  条数={res.get('item_count')}  message={d.get('message')}")
    check((res.get("item_count") or 0) > 0, "抓到了歌曲列表", f"{res.get('item_count')} 条")

    print("\n=== 产物 ===")
    # 产物登记在任务结果里(界面「插件下载产物」卡片读的就是它)。
    # **不要只扫 data/plugin_output/music/** —— 那是硬编码路径, 用户改了 subdir
    # 或 output_dir 就会扫空, 于是"明明下到了却报 0 个"。
    downloads = res.get("downloads") or []
    all_files: list[pathlib.Path] = []
    for d in downloads:
        p = pathlib.Path(str(d.get("path") or ""))
        if p.is_file():
            all_files.append(p)
            print(f"    {p.name}  {p.stat().st_size} 字节  ok={d.get('ok')} err={d.get('error')}")
    if not all_files and OUT.exists():
        all_files = sorted(p for p in OUT.rglob("*") if p.is_file())
        for p in all_files:
            print(f"    (扫描目录) {p.name}  {p.stat().st_size} 字节")

    # **只挑音频**: 页面上还有封面图, 图片下载器会把它们也登记进产物。
    # 一开始没区分, 结果把 .jpg 当成"下载到的音频"去用 ffprobe 校验, 得出荒谬结论。
    audio = [p for p in all_files if p.suffix.lower() in (".mp3", ".m4a", ".flac", ".ogg", ".mka")]
    others = [p for p in all_files if p not in audio]
    if others:
        print(f"    (非音频产物 {len(others)} 个: {[p.suffix for p in others]} —— 多为封面图)")
    check(bool(audio), "**下载到了音频文件**", f"{len(audio)} 个")
    check(not any(p.suffix.lower() in (".m3u8", ".mpd", ".ts") for p in audio),
          "产物不是播放列表/分片文本", f"后缀={[p.suffix for p in audio]}")
    files = audio

    print("\n=== 用 ffprobe 复核每个产物真的能播放 ===")
    for p in files:
        if not FFPROBE.exists():
            print("    (没有 ffprobe, 跳过)")
            break
        r = subprocess.run(
            [str(FFPROBE), "-hide_banner", "-loglevel", "error", "-show_entries",
             "format=format_name,duration,bit_rate", "-of", "json", str(p)],
            capture_output=True, timeout=60)
        info = r.stdout.decode("utf-8", "replace")
        try:
            fmt = json.loads(info).get("format") or {}
        except Exception:  # noqa: BLE001
            fmt = {}
        ok = bool(fmt.get("duration"))
        print(f"    {p.name}: format={fmt.get('format_name')!r} "
              f"duration={fmt.get('duration')!r} bit_rate={fmt.get('bit_rate')!r}")
        check(ok, f"**{p.name} 是可解析的音频**(ffprobe 有时长)")

    print("\n" + "=" * 68)
    if failures:
        print(f"网易云插件端到端: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("网易云插件端到端: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
