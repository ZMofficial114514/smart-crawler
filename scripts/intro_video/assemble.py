"""
拼接成片: 片头 + 演示片段 + 片尾, 并留出配乐轨。

三个片段的音轨情况不同: 片头/片尾**无声**, 演示片段带动效音效。用 concat 滤镜统一
(先把无音轨的补成静音), 再输出一条完整音轨; 用户之后把自己选的配乐叠上来即可。

输出两级:
  - ``SmartCrawler-介绍.mp4``      —— 无配乐(用户自己配)
  - 音效单独留一条音轨, 方便叠配乐时保留点击声
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = Path(__file__).resolve().parents[2]
FFMPEG = PROJ / "tools" / "ffmpeg" / "ffmpeg.exe"
FFPROBE = PROJ / "tools" / "ffmpeg" / "ffprobe.exe"


def dur(p: Path) -> float:
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float((out.stdout or "0").strip() or 0)


def has_audio(p: Path) -> bool:
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "audio" in (out.stdout or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", default=str(PROJ / ".runtmp" / "head.mp4"))
    ap.add_argument("--demo", default=str(PROJ / "dist" / "_demo_clip.mp4"))
    ap.add_argument("--tail", default=str(PROJ / ".runtmp" / "tail.mp4"))
    ap.add_argument("--out", default=str(PROJ / "dist" / "SmartCrawler-介绍.mp4"))
    args = ap.parse_args()

    parts = [Path(args.head), Path(args.demo), Path(args.tail)]
    for p in parts:
        if not p.exists():
            print(f"  缺少片段: {p}")
            return 1
    total = 0.0
    print("=== 片段 ===")
    for p in parts:
        d = dur(p)
        total += d
        print(f"    {p.name:<18} {d:>7.2f}s  "
              f"{'有音轨' if has_audio(p) else '无音轨'}")
    print(f"    合计 {total:.1f}s = {total / 60:.2f} 分钟")

    # concat 要求每段都有同构的音轨; 无音轨的补一条静音输入。
    #
    # **索引必须显式算清楚**: 静音输入是**追加在三个片段之后**的, 所以"片段 i 的音轨在
    # 第几个输入"取决于前面已经追加了几个静音 —— 我第一次直接写 [len(parts)+i:a] 就错了,
    # ffmpeg 报 "Invalid file index 5"(实际只到 4)。
    n = len(parts)
    audio_idx: list[int] = []
    extra_silence = 0
    for i, p in enumerate(parts):
        if has_audio(p):
            audio_idx.append(i)
        else:
            audio_idx.append(n + extra_silence)
            extra_silence += 1

    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    for p in parts:
        if not has_audio(p):
            inputs += ["-f", "lavfi", "-t", f"{dur(p):.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    chain = "".join(f"[{i}:v][{audio_idx[i]}:a]" for i in range(n))
    concat = chain + f"concat=n={n}:v=1:a=1[v][a]"

    out = Path(args.out)
    print(f"\n  拼接 -> {out.name}")
    r = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", concat,
         "-map", "[v]", "-map", "[a]",
         # 音频同样避开 aac 编码器(它在这个构建里会挂死), 用 mp3
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "libmp3lame", "-b:a", "192k",
         "-movflags", "+faststart", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  拼接失败:")
        print((r.stderr or "")[-2000:])
        return 1
    d = dur(out)
    print(f"  ✓ 完成: {out.name}  {out.stat().st_size / 1024 / 1024:.1f} MB  "
          f"{d:.1f}s = {d / 60:.2f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
