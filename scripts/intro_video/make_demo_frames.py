"""
剪切演示录屏 —— Pillow 逐帧版(替代 ffmpeg 滤镜链)。

**为什么换成逐帧**: 原本用 ffmpeg 的 trim/setpts/concat + 30 层 overlay 实现变速与鼠标符,
实测**会卡死** —— 25 秒内 CPU 增长为 0, 输出停在 256 KB, 两次尝试分别跑了 32 分钟和 10 分钟
都没出片。30 层 overlay 串起来的滤镜图太重了。

自己逐帧做反而简单可控:
  - 变速 = 按速度计划算"这一输出帧对应源片第几帧", 直接取那一帧;
  - 鼠标符 = 在对应帧上贴一张预渲染的指针图(带淡入淡出);
  - 音效 = 往时间轴上铺, 与画面共用同一套时间映射, 不会错位。
与介绍片用同一条"Pillow -> 管道 -> ffmpeg"的路径, 那条路已经验证可靠。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import demo_kit as dk  # noqa: E402
import demo_plan as dp  # noqa: E402

#: 鼠标符展示时长与淡入淡出(输出时间轴上的秒数)
CUR_IN, CUR_HOLD, CUR_OUT = 0.10, 0.55, 0.28


def find_cursor_pos(centers: list[tuple[float, float, float]], idx: int) -> tuple[float, float]:
    """取 idx 附近变化最集中那一帧的中心, 作为鼠标符位置。"""
    lo, hi = max(0, idx - 3), min(len(centers) - 1, idx + 3)
    best, bp = -1.0, (0.5, 0.5)
    for k in range(lo, hi + 1):
        cx, cy, strength = centers[k]
        if strength > best:
            best, bp = strength, (cx, cy)
    return bp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(dk.OUT_DIR / "_demo_clip.mp4"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="只渲染前 N 帧(调试用, 用来隔离管道问题)")
    args = ap.parse_args()

    dur, diff, times, centers = dp.analyze()
    plan = dk.build_speed_plan(dur, times, diff, dp.AFPS)
    new_dur = dk.plan_duration(plan)
    print("=== 速度计划 ===")
    for a, b, s in plan:
        if s == 1.0:
            continue
        print(f"    {a:>7.2f}s .. {b:>7.2f}s   {b - a:>6.2f}s -> "
              f"{(b - a) / s:>5.2f}s   {s:g}x")
    kept = sum(b - a for a, b, s in plan if s == 1.0)
    print(f"    原始 {dur:.1f}s -> {new_dur:.1f}s  "
          f"(保持原速 {kept:.1f}s, 压缩 {dur - kept:.1f}s)")

    raw_events = dp.find_events(diff, times, centers)
    events = [(dk.map_time(t, plan), cx, cy, kind) for t, cx, cy, kind in raw_events]
    clicks = sum(1 for *_x, k in events if k == "click")
    swipes = sum(1 for *_x, k in events if k == "swipe")
    print(f"\n=== 事件 {len(events)} 个(点击 {clicks} / 滑动 {swipes})===")

    if args.dry_run:
        print("  (--dry-run)")
        return 0

    # ---- 逐帧渲染 ----
    src = dk.ensure_prepped()
    out = Path(args.out)
    dk.OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_out = int(new_dur * dk.OUT_FPS)

    # 预渲染两种鼠标符(贴图也放 .runtmp, 不落在 scripts/ 里)
    workdir = dk.ensure_workdir()
    cur_imgs: dict[str, Image.Image] = {}
    for kind in ("click", "swipe"):
        tmp = workdir / f"cursor_{kind}.png"
        dk.render_cursor(tmp, kind)
        cur_imgs[kind] = Image.open(tmp).convert("RGBA")

    # 输出帧号 -> 源时间(把速度计划展开成逐帧映射)
    src_time_of: list[float] = []
    for a, b, s in plan:
        step = 1.0 / dk.OUT_FPS * s
        t = a
        while t < b - 1e-9:
            src_time_of.append(t)
            t += step
    n_out = min(n_out, len(src_time_of))
    if args.limit:
        n_out = min(n_out, args.limit)
    print(f"\n  渲染 {n_out} 帧(源 {dur:.1f}s -> 输出 {n_out / dk.OUT_FPS:.1f}s)",
          flush=True)

    sfx = dk.build_sfx_track(n_out / dk.OUT_FPS,
                             [(t, k == "swipe") for t, _x, _y, k in events])
    wav = dk.WORKDIR / "sfx.wav"
    dk.write_wav(wav, sfx)

    # 逐帧读源: 用 ffmpeg 按需 seek 太慢, 改成一次性把源解码成 RGB 帧表会占太多内存
    # (1930 帧 x 1080p x 3B ≈ 12GB)。折中: 按"输出顺序"顺序读取源帧, 用 ffmpeg 的
    # select 表达式跳帧 —— 但更简单可靠的是让 ffmpeg 输出**固定帧率**的原始帧, 然后
    # 我们按索引抽样。源已预处理为 30fps, 所以源帧 k 的时间就是 k/30。
    proc_in = subprocess.Popen(
        [str(dk.FFMPEG), "-v", "error", "-i", str(src),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    #: **两遍法**: 先把画面写成无声 MP4, 再单独把音效混进去。
    #:
    #: 为什么不用一条管道直接连到编码器: 那条路在这台机器上**必然死锁** ——
    #: 解码器与编码器同时挂着, Python 夹在中间转发 6MB 帧, 稳定卡在开头两秒内
    #: (合成帧实验 600 帧 3.3 秒跑完, 证明编码器本身没问题; 隔离实验里解码器单独读也有
    #: 180 帧/秒; 但两者一起跑就卡住, 且两个子进程都还活着、CPU 都不再增长)。
    #: 与其继续跟管道调度较劲, 不如让它**完全不并发**: 第一遍只读源 + 写画面文件,
    #: 第二遍只做音频混合。慢几秒, 但确定能跑完。
    silent = dk.WORKDIR / "silent.mp4"
    errlog = dk.WORKDIR / "encode.log"
    err_fp = errlog.open("wb")
    proc_out = subprocess.Popen(
        [str(dk.FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{dk.OUT_W}x{dk.OUT_H}", "-r", str(dk.OUT_FPS), "-i", "pipe:0",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(silent)],
        stdin=subprocess.PIPE, stderr=err_fp)
    assert proc_in.stdout is not None and proc_out.stdin is not None

    fb = dk.OUT_W * dk.OUT_H * 3
    # 事件查表: 输出帧 -> 该帧要画的鼠标符(含该帧的淡入淡出透明度)
    span_in = max(1, int(CUR_IN * dk.OUT_FPS))
    span_hold = max(1, int(CUR_HOLD * dk.OUT_FPS))
    span_out = max(1, int(CUR_OUT * dk.OUT_FPS))
    total_span = span_in + span_hold + span_out
    ev_at: dict[int, list[tuple[str, int, int, float]]] = {}
    for t, cx, cy, kind in events:
        f0 = max(0, int(round((t - CUR_IN) * dk.OUT_FPS)))
        for f in range(f0, min(n_out, f0 + total_span)):
            rel = f - f0
            if rel < span_in:
                alpha = rel / span_in
            elif rel < span_in + span_hold:
                alpha = 1.0
            else:
                alpha = max(0.0, 1.0 - (rel - span_in - span_hold) / span_out)
            ev_at.setdefault(f, []).append(
                (kind, int(cx * dk.OUT_W), int(cy * dk.OUT_H), alpha))

    #: 每个输出帧要用的**源帧号**。
    #:
    #: 性能关键: 上一版写的是"每个输出帧都 while 丢弃到目标帧", 而每次丢弃都是一次 6MB 的
    #: 管道读 —— 1080p 下输入端跑到 601s CPU 而输出端只用了 3s, 慢到不可用。现在源帧整体
    #: 只读一遍(1091 帧), 加速段里"两个输出帧落在同一源帧"时**重复上一帧**即可。
    targets = [int(round(t * dk.OUT_FPS)) for t in src_time_of]

    read = 0
    written = 0
    last_img: Image.Image | None = None
    stall_hint = 0.0
    for i in range(n_out):
        want = targets[i]
        if want < read:
            img = last_img                      # 源帧已读过, 重复上一帧
        else:
            img = None
            while read <= want:
                # 读之前先看一眼子进程是否还活着 —— 卡死时"谁先死"是决定性的线索。
                # (之前连续几次卡在第 120 帧, 就是靠这个把范围缩到输入侧的。)
                if proc_in.poll() is not None:
                    print(f"    输入解码器已退出(码 {proc_in.returncode}) "
                          f"在第 {read} 帧", flush=True)
                    break
                if proc_out.poll() is not None:
                    print(f"    编码器已退出(码 {proc_out.returncode}) "
                          f"在第 {read} 帧", flush=True)
                    break
                buf = proc_in.stdout.read(fb)
                if len(buf) < fb:
                    break
                img = Image.frombytes("RGB", (dk.OUT_W, dk.OUT_H), buf)
                read += 1
        if img is None:
            break
        last_img = img
        if i in ev_at:
            img = img.convert("RGBA")
            for kind, x, y, alpha in ev_at[i]:
                ci = cur_imgs[kind]
                if alpha < 0.999:
                    ci = ci.copy()
                    a = ci.getchannel("A").point(lambda v, k=alpha: int(v * k))
                    ci.putalpha(a)
                img.alpha_composite(ci, (x - dk.CURSOR_W // 2, y - dk.CURSOR_H // 2))
            img = img.convert("RGB")
        proc_out.stdin.write(img.tobytes())
        written += 1
        # 每 60 帧报一次进度并 flush。**这条日志是排查死锁的关键** ——
        # 之前只在 400 帧报一次, 卡住时完全看不出卡在哪一帧。
        if written % 60 == 0 or written == n_out:
            print(f"    {written}/{n_out} 帧  (源帧 {read}/{len(targets)})",
                  flush=True)
    proc_in.stdout.close()
    proc_in.wait()
    proc_out.stdin.close()
    code = proc_out.wait()
    err_fp.close()
    if code != 0:
        msg = errlog.read_text(encoding="utf-8", errors="replace")
        print("  ffmpeg 画面编码失败:")
        print(msg[-1500:])
        return 1
    print(f"    画面完成({written} 帧) -> {silent.name}", flush=True)

    #: 第二遍: 单独混音。此时没有任何并发的读管道, 不可能死锁。
    #:
    #: **音频编码器必须用 libmp3lame, 不能用 aac。** 这个 ffmpeg 构建里的原声 aac 编码器
    #: 会挂死 —— 实测把一段 66 秒的 WAV 单独转 aac(输出到 null)超过 45 秒仍不完成, CPU
    #: 攒到 600s 后彻底停住; 换 libmp3lame 只用 0.5 秒。之前连续三次"管道死锁"其实都是
    #: 它引起的: 编码器卡住 -> 不再读 stdin -> Python 写 stdin 阻塞 -> 两个子进程 CPU 归零。
    print("    混入点击/滑动音效(mp3)…", flush=True)
    r = subprocess.run(
        [str(dk.FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(silent), "-i", str(wav),
         "-c:v", "copy", "-c:a", "libmp3lame", "-b:a", "192k",
         "-movflags", "+faststart", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  混音失败:")
        print((r.stderr or "")[-1200:])
        return 1
    print(f"  ✓ 完成: {out}  ({out.stat().st_size / 1024 / 1024:.1f} MB, "
          f"{written} 帧)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
