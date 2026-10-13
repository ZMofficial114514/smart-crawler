"""
把场景渲染成 MP4, 并合成一段简单配乐。

- 画面: Pillow 逐帧绘制, 原始 RGB 直接经 stdin 管道喂给 ffmpeg。
  不落盘中转帧(7200 帧 1080p 的 PNG 会占几个 GB), 也不需要联网装库。
- 配乐: 用 numpy 合成。参考视频是"讲解向"的, 所以取**安静不抢戏**的路子:
  低音铺底 + 缓慢的四音和弦进行 + 极轻的脉冲, 没有人声。
- 字幕: 每场景一条底部黄字(与参考视频一致), 由场景表给出。

用法:
    python make_intro_video.py                      # 输出到 dist/SmartCrawler-介绍.mp4
    python make_intro_video.py --fps 30 --size 1920x1080
    python make_intro_video.py --frames-only 0 90   # 只导出第 0..90 帧为 PNG(调试用)
"""

from __future__ import annotations

import argparse
import math
import pathlib
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import render_kit as rk  # noqa: E402
import scenes as SC  # noqa: E402
import scenes_extra as SX  # noqa: E402

PROJ = HERE.parent.parent
FFMPEG = PROJ / "tools" / "ffmpeg" / "ffmpeg.exe"

#: 每场景的额外停留时间(秒)。场景表里的 dur 已经包含正文动画, 这里再加"看完字幕"的时间。
#: 调这个值就能整体拉长/压缩成片 —— 18 个场景 x 此值 = 总增加量。
HOLD = 2.4


def build_all() -> list[rk.Scene]:
    """场景表 + 补时。顺序即成片顺序。"""
    base = SC.build_scenes()
    # 把补充场景插进合适的位置: 结构分析之后讲 iframe/选择器/瀑布流, 控制台之后讲抓包
    order_keys = [s.key for s in base]
    extra = [
        SX.scene_iframe, SX.scene_selector, SX.scene_sniff, SX.scene_masonry,
        SX.scene_quality,
    ]
    out: list[rk.Scene] = []
    for s in base:
        out.append(s)
        if s.key == "pillars":
            out.append(rk.Scene("iframe", 12.4, SX.scene_iframe,
                                "分析与提取必须在同一个 frame 上", 5.2, "IFRAME"))
        if s.key == "ai":
            out.append(rk.Scene("selector", 12.4, SX.scene_selector,
                                "覆盖不全才替换, 覆盖满就不动手", 5.4, "选择器"))
        if s.key == "console":
            out.append(rk.Scene("sniff", 12.0, SX.scene_sniff,
                                "捕获 XHR/fetch/WebSocket 并解析 JSON", 4.4, "网络抓包"))
        if s.key == "scope":
            out.append(rk.Scene("masonry", 11.2, SX.scene_masonry,
                                "并集可被一个更宽的选择器选中, 那就合并", 4.2, "瀑布流"))
        if s.key == "robust":
            out.append(rk.Scene("quality", 12.0, SX.scene_quality,
                                "49 个脚本: 43 个 verify + 6 个 check", 5.4, "验收"))
    assert order_keys, "场景表为空"
    for s in out:
        # design 记下"动画本来多长" —— 绘制函数里的绝对时刻都是按它写的。
        # dur 再补上 HOLD, 让观众有时间看完字幕。
        if not s.design:
            s.design = s.dur
        s.dur += HOLD
    return out


def inject_time(sc: rk.Scene) -> float:
    """把本场景的时长与拉伸比例注入场景模块与 render_kit, 返回 K。

    fade 的归一化要两个量: **设计时长**(把设计轴上的 a/hold_to 换算到实际轴)与
    **实际时长**(换算的分母, 即 scale = 实际/设计)。传错会让停留期直接消失 ——
    单测抓到过一次(淡出提前到 13.5s 就把画面清空)。
    """
    k = (sc.dur / sc.design) if sc.design else 1.0
    rk.set_design(sc.design or sc.dur, sc.dur)
    SC.DUR = sc.dur
    SX.DUR = sc.dur
    SC.K = k
    SX.K = k
    return k


# ---------------------------------------------------------------------------
# 配乐合成
# ---------------------------------------------------------------------------
SR = 44100


def _env(n: int, attack: float, release: float) -> np.ndarray:
    """起音/释放包络。"""
    a = max(1, int(attack * SR))
    r = max(1, int(release * SR))
    e = np.ones(n, dtype=np.float64)
    a = min(a, n)
    e[:a] = np.linspace(0.0, 1.0, a) ** 1.5
    r = min(r, n)
    e[n - r:] = np.linspace(1.0, 0.0, r) ** 1.5
    return e


def _tone(freq: float, dur: float, amp: float, detune: float = 0.0,
          odd_only: bool = False) -> np.ndarray:
    """加性合成的柔和音色。odd_only 时只有奇次谐波(接近方波/单簧管音色)。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    harmonics = (1, 3, 5, 7) if odd_only else (1, 2, 3, 4, 5, 6)
    for h in harmonics:
        w = 1.0 / (h ** 1.8)
        out += w * np.sin(2 * np.pi * freq * h * t)
    if detune:
        out += 0.5 * np.sin(2 * np.pi * (freq * (1 + detune)) * t)
    out /= (np.abs(out).max() or 1.0)
    return out * amp * _env(n, 0.35, 1.1)


def make_music(dur: float) -> np.ndarray:
    """低音铺底 + 缓慢和弦进行 + 极轻脉冲。整体偏安静, 不与人声/字幕抢注意力。"""
    n = int(dur * SR) + SR
    buf = np.zeros(n)

    def add(sig: np.ndarray, at: float) -> None:
        i = int(at * SR)
        if i >= n:
            return
        seg = sig[: n - i]
        buf[i: i + len(seg)] += seg

    # 和弦进行: Am - F - C - G(常用且温和), 每 8 秒一个
    chords = [
        (220.00, [220.00, 261.63, 329.63]),   # A minor
        (174.61, [174.61, 220.00, 261.63]),   # F major
        (261.63, [261.63, 329.63, 392.00]),   # C major
        (196.00, [196.00, 246.94, 293.66]),   # G major
    ]
    bar = 8.0
    k = 0
    at = 0.0
    while at < dur:
        root, notes = chords[k % len(chords)]
        # 低音: 两个八度下沉, 长音铺底
        add(_tone(root / 2, bar * 1.02, 0.16), at)
        # 和弦: 三个音, 轻微 detune 制造厚度
        for j, f in enumerate(notes):
            add(_tone(f, bar * 0.98, 0.055 - j * 0.008, detune=0.0016), at + j * 0.12)
        # 极轻的高八度脉冲, 每 2 秒一次, 提供时间的"呼吸"
        for b in range(4):
            add(_tone(notes[0] * 4, 1.1, 0.016, odd_only=True), at + b * 2.0)
        at += bar
        k += 1

    # 首尾淡入淡出
    fin = int(3.0 * SR)
    buf[:fin] *= np.linspace(0, 1, fin)
    buf[-int(5.0 * SR):] *= np.linspace(1, 0, int(5.0 * SR))

    peak = np.abs(buf).max() or 1.0
    return (buf / peak * 0.30).astype(np.float32)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(out_path: pathlib.Path, fps: int, size: tuple[int, int],
           with_audio: bool = True) -> int:
    # 尺寸必须在场景模块 import **之前**设定 —— 它们用 `from render_kit import W, H`,
    # 会把当时的值绑进自己的命名空间。render_kit.set_size 会清掉背景缓存。
    rk.set_size(*size)
    rk.assert_size_matches(*size)
    scenes = build_all()
    total = sum(s.dur for s in scenes)
    nframes = int(total * fps)

    print(f"  场景 {len(scenes)} 个, 总时长 {total:.1f}s, {nframes} 帧 @ {fps}fps, "
          f"{size[0]}x{size[1]}")

    bg = rk.background()
    # 每场景一层漂浮关键词(缓存, 避免逐帧重建)
    noise_by_scene: dict[str, Image.Image] = {}

    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "pipe:0",
    ]
    if with_audio:
        wav = HERE / "_music.wav"
        write_wav(wav, make_music(total))
        cmd += ["-i", str(wav)]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += [str(out_path)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    cap_font = rk.cn(38)
    tag_font = rk.mono(20)

    frame_i = 0
    for si, sc in enumerate(scenes):
        # 把本场景时长注入场景模块, 让 fade(hold_to=DUR - 余量) 能算出正确的淡出时机。
        # 不注入的话 DUR 恒为 0, 所有内容会在 t≈0 就淡出, 整片变空屏。
        inject_time(sc)
        if sc.key not in noise_by_scene:
            noise_by_scene[sc.key] = rk.noise_layer(si * 977 + 13, 14,
                                                   rk.latin(48), rk.WHITE, 30)
        noise = noise_by_scene[sc.key]
        steps = int(sc.dur * fps)
        for k in range(steps):
            t = k / fps
            # 背景 + 缓慢漂移的漂浮词(整体平移, 成本极低)
            img = bg.copy()
            dx = int(18 * math.sin((t + si) * 0.16))
            dy = int(12 * math.cos((t + si) * 0.11))
            img.paste(noise, (dx, dy), noise)

            layer = Image.new("RGBA", (size[0], size[1]), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            sc.draw(d, t)

            # 底部字幕(与参考视频一致: 居中黄字)
            if sc.caption:
                cap_a = rk.fade(t, sc.caption_from, sc.caption_from + 0.7)
                if cap_a > 0:
                    rk.draw_caption(d, sc.caption, cap_a)
            # 右上角标签
            if sc.tag:
                rk.draw_tag(d, sc.tag, 96)

            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
            proc.stdin.write(img.tobytes())
            frame_i += 1
        pct = frame_i / nframes * 100
        print(f"    [{si + 1:>2}/{len(scenes)}] {sc.key:<12} {sc.dur:>5.1f}s  "
              f"({pct:5.1f}%)")

    proc.stdin.close()
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    code = proc.wait()
    if code != 0:
        print(f"  ffmpeg 失败 ({code}):\n{err[:1500]}")
    return code


def write_wav(path: pathlib.Path, samples: np.ndarray) -> None:
    """写 16bit 单声道 WAV。手写头部, 避免依赖 scipy/wave 之外的库。"""
    import struct

    data = np.clip(samples, -1.0, 1.0)
    ints = (data * 32767).astype("<i2")
    raw = ints.tobytes()
    with path.open("wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(raw)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(raw)))
        f.write(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(PROJ / "dist" / "SmartCrawler-介绍.mp4"))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--frames-only", nargs=2, type=int, metavar=("A", "B"),
                    help="只导出第 A..B 帧为 PNG(调试用, 不调用 ffmpeg)")
    args = ap.parse_args()

    w, h = (int(x) for x in args.size.lower().split("x"))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.frames_only:
        return dump_frames(out.parent, (w, h), args.frames_only[0], args.frames_only[1])

    if not FFMPEG.exists():
        print(f"  找不到 ffmpeg: {FFMPEG}")
        return 1
    return render(out, args.fps, (w, h), with_audio=not args.no_audio)


def dump_frames(outdir: pathlib.Path, size: tuple[int, int], a: int, b: int) -> int:
    """把指定帧号导出成 PNG, 用于快速检查画面(不编码视频)。"""
    rk.set_size(*size)
    rk.assert_size_matches(*size)
    scenes = build_all()
    bg = rk.background()
    # 帧号 -> (场景, 场景内时间)
    marks: list[tuple[int, rk.Scene, float]] = []
    fi = 0
    for si, sc in enumerate(scenes):
        for k in range(int(sc.dur * FPS_LOCAL)):
            marks.append((fi, sc, k / FPS_LOCAL))
            fi += 1
    print(f"  总帧数 {fi}, 导出 {a}..{b}")
    for want in range(a, min(b, fi - 1) + 1):
        _, sc, t = marks[want]
        si = [s.key for s in scenes].index(sc.key)
        inject_time(sc)
        img = bg.copy()
        noise = rk.noise_layer(si * 977 + 13, 14, rk.latin(48), rk.WHITE, 30)
        img.paste(noise, (0, 0), noise)
        layer = Image.new("RGBA", (size[0], size[1]), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        sc.draw(d, t)
        if sc.caption:
            ca = rk.fade(t, sc.caption_from, sc.caption_from + 0.7)
            if ca > 0:
                rk.draw_caption(d, sc.caption, ca)
        if sc.tag:
            rk.draw_tag(d, sc.tag, 96)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        p = outdir / f"frame_{want:05d}_{sc.key}.png"
        img.save(p)
        print(f"    {p.name}")
    return 0


FPS_LOCAL = SC.__dict__.get("FPS", 30)

if __name__ == "__main__":
    sys.exit(main())
