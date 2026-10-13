"""
把演示录屏剪成可用于宣传的片段。

做三件事:
  1. **按活动度加速**: 实测 62% 的时长画面几乎不动(等抓取跑完 18.5s、等分析结果 9.8s),
     这些段落按倍数压缩; 有操作的段落保持原速。
  2. **叠加大鼠标符**: 原录屏里的鼠标很小, 观众注意不到点击。在突变时刻叠加一个高亮的
     光圈脉冲 + 一个放大的指针, 明确指示"这里点了"。
  3. **合成点击/滑动音效**: 音效直接对齐到检测出的操作时刻, 不依赖原声。

原声**直接丢弃**: 录屏里的浏览器/键盘噪声对宣传片是干扰, 且音效位置由我们控制更准。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = Path(__file__).resolve().parents[2]
FFMPEG = PROJ / "tools" / "ffmpeg" / "ffmpeg.exe"
FFPROBE = PROJ / "tools" / "ffmpeg" / "ffprobe.exe"
DEMO = Path(r"C:\Users\zm112\Videos\Captures\抓取任务 · SmartCrawler 控制台 - Google Chrome 2026-10-13 13-29-06.mp4")

#: 预处理后的演示源。
#:
#: **必须先预处理再剪**: 原片是 2560×1380 / 54fps / 830MB。我第一版把 fps/scale/pad 放进
#: "每个 trim 段各做一次", 结果 21 段各缩放一遍, ffmpeg 跑了 32 分钟没出片。
#: 现在改成: 先一次快速转码把它压到 1920×1080 / 30fps(实测 18 秒), 剪切时只做 trim +
#: setpts + concat, 快得多。
#:
#: 所有中间产物都放 ``.runtmp/``(已被 .gitignore 排除)—— 早先它们直接写在 scripts/ 里,
#: 结果是 `git status` 里冒出 14MB 的 mp4 和 5.7MB 的 wav 等着被误提交。
WORKDIR = PROJ / ".runtmp" / "intro_video"
PREPPED = WORKDIR / "demo_prepped.mp4"


def ensure_workdir() -> Path:
    """确保中间产物目录存在。所有中间文件都写在这里(已被 .gitignore 排除)。"""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    return WORKDIR
OUT_DIR = PROJ / "dist"

SR = 44100

#: 输出画布(与介绍片一致, 便于拼接)
OUT_W, OUT_H = 1920, 1080
OUT_FPS = 30


def probe_dur(path: Path) -> float:
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float(json.loads(out.stdout)["format"]["duration"])


def ensure_prepped() -> Path:
    """确保预处理产物存在(不存在就现做一次)。返回它的路径。

    预处理 = 一次性降到 1920×1080 / 30fps / 无音轨。理由见 PREPPED 的注释:
    把缩放放进"每个 trim 段"会让 21 段各缩放一遍, 慢到不可用。
    """
    if PREPPED.exists():
        return PREPPED
    ensure_workdir()
    vf = (f"fps={OUT_FPS},scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
          f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=0x1e1e1e")
    print(f"  预处理演示源(一次性降帧降分辨率)…")
    r = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(DEMO), "-vf", vf, "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(PREPPED)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"预处理失败: {(r.stderr or '')[-800:]}")
    print(f"  预处理完成: {PREPPED.stat().st_size / 1024 / 1024:.1f} MB")
    return PREPPED


def read_gray(path: Path, gw: int, gh: int, fps: int) -> np.ndarray:
    """把视频读成降采样灰度帧序列, 返回 (n, gh*gw) 的 int16 数组。"""
    proc = subprocess.Popen(
        [str(FFMPEG), "-v", "error", "-i", str(path),
         "-vf", f"fps={fps},scale={gw}:{gh}", "-f", "rawvideo",
         "-pix_fmt", "gray", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    fb = gw * gh
    frames = []
    while True:
        buf = proc.stdout.read(fb)
        if len(buf) < fb:
            break
        frames.append(np.frombuffer(buf, dtype=np.uint8))
    proc.stdout.close()
    proc.wait()
    return np.stack(frames).astype(np.int16)


def read_gray_big(path: Path, gw: int, gh: int, fps: int) -> np.ndarray:
    """同上, 但保留二维形状, 用于定位"变化发生在画面哪个位置"。"""
    flat = read_gray(path, gw, gh, fps)
    return flat.reshape(len(flat), gh, gw)


def change_centroid(prev: np.ndarray, cur: np.ndarray, gw: int, gh: int,
                    block: int = 8) -> tuple[float, float, float]:
    """算出两帧之间"变化最集中的区域"的中心, 返回 (x 比例, y 比例, 强度)。

    做法: 先算逐像素差, 再按 block×block 分块求均值, 取最强块的加权质心。
    比"整帧平均"有用得多 —— 整帧平均只能说明"有变化", 定位不到**哪里**变了;
    而观众需要的正是"这里被点了"。缩放到 0..1 便于直接映射到输出画布。
    """
    d = np.abs(cur - prev).astype(np.float32)
    bh, bw = gh // block, gw // block
    if bh == 0 or bw == 0:
        return 0.5, 0.5, float(d.mean())
    blk = d[: bh * block, : bw * block].reshape(bh, block, bw, block).mean(axis=(1, 3))
    peak = float(blk.max())
    if peak <= 0:
        return 0.5, 0.5, 0.0
    # 只保留接近最强的块, 避免把分散的小变化也拉进质心
    mask = blk >= peak * 0.55
    ys, xs = np.nonzero(mask)
    w = blk[ys, xs]
    cy = float((ys * w).sum() / w.sum()) + 0.5
    cx = float((xs * w).sum() / w.sum()) + 0.5
    return cx / bw, cy / bh, peak


# ---------------------------------------------------------------------------
# 速度计划
# ---------------------------------------------------------------------------
def build_speed_plan(dur: float, diff_t: np.ndarray, diff: np.ndarray,
                     fps: int) -> list[tuple[float, float, float]]:
    """返回 [(起, 止, 倍速), ...], 覆盖整段; 有操作的区间保持 1x。

    思路: 只有在"画面长时间不动"的区间才加速。阈值放宽一点, 避免把细碎停顿也切进去
    (切太碎会让画面一跳一跳, 观感更差)。
    """
    # 有操作的时刻: 帧差显著
    med = float(np.median(diff))
    active = diff >= max(med * 2.0, 0.8)

    # 把"非活动"的连续帧并成段, 只保留足够长的
    still: list[tuple[float, float]] = []
    start = None
    for i, a in enumerate(active):
        if not a and start is None:
            start = i
        elif a and start is not None:
            if (i - start) / fps >= 2.0:
                still.append((start / fps, i / fps))
            start = None
    if start is not None and (len(active) - start) / fps >= 2.0:
        still.append((start / fps, len(active) / fps))

    # 倍速按静止时长递增: 停得越久压得越狠
    plan: list[tuple[float, float, float]] = []
    cursor = 0.0
    for a, b in still:
        span = b - a
        if span >= 12:
            speed = 6.0
        elif span >= 6:
            speed = 4.0
        elif span >= 3.5:
            speed = 2.6
        else:
            speed = 1.8
        if a > cursor + 0.01:
            plan.append((cursor, a, 1.0))
        plan.append((a, b, speed))
        cursor = b
    if cursor < dur - 0.01:
        plan.append((cursor, dur, 1.0))
    return plan


def plan_duration(plan: list[tuple[float, float, float]]) -> float:
    return sum((b - a) / s for a, b, s in plan)


def map_time(t: float, plan: list[tuple[float, float, float]]) -> float:
    """原始时间 -> 压缩后时间(音效定位要用)。"""
    out = 0.0
    for a, b, s in plan:
        if t <= a:
            return out
        if t <= b:
            return out + (t - a) / s
        out += (b - a) / s
    return out


# ---------------------------------------------------------------------------
# 音效合成
# ---------------------------------------------------------------------------
def sfx_click() -> np.ndarray:
    """鼠标点击: 极短的宽频脉冲 + 快速衰减。"""
    n = int(0.055 * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 90)
    body = (np.sin(2 * np.pi * 2400 * t) * 0.5
            + np.sin(2 * np.pi * 1150 * t) * 0.3)
    rng = np.random.default_rng(7)
    transient = rng.normal(0, 0.5, n) * np.exp(-t * 400)
    out = (body + transient) * env
    return (out / (np.abs(out).max() or 1) * 0.55).astype(np.float32)


def sfx_swipe() -> np.ndarray:
    """滚动/滑动: 带通噪声做一次短促的"呼"声。"""
    n = int(0.30 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(19)
    noise = rng.normal(0, 1, n)
    # 简易带通: 一阶差分(高通) + 滑动平均(低通) 组合
    hp = np.diff(noise, prepend=noise[0])
    k = 24
    lp = np.convolve(hp, np.ones(k) / k, mode="same")
    env = np.concatenate([
        np.linspace(0, 1, int(0.05 * SR)),
        np.linspace(1, 0, n - int(0.05 * SR)),
    ])[:n]
    out = lp * env
    return (out / (np.abs(out).max() or 1) * 0.30).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
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


def build_sfx_track(total: float, events: list[tuple[float, bool]]) -> np.ndarray:
    """把音效铺到时间轴上。events = [(时间, 是否滑动), ...]"""
    buf = np.zeros(int(total * SR) + SR, dtype=np.float32)
    click, swipe = sfx_click(), sfx_swipe()
    for t, is_swipe in events:
        sig = swipe if is_swipe else click
        i = int(t * SR)
        if i < 0 or i >= len(buf):
            continue
        j = min(len(buf), i + len(sig))
        buf[i:j] += sig[: j - i]
    peak = float(np.abs(buf).max())
    if peak > 0.95:
        buf *= 0.95 / peak
    return buf


# ---------------------------------------------------------------------------
# 大鼠标符(点击提示)
# ---------------------------------------------------------------------------
CURSOR_W, CURSOR_H = 200, 200


def render_cursor(path: Path, kind: str = "click") -> None:
    """画一个放大的指针 + 扩散光圈, 用来提示"这里被点了"。

    为什么要自己画: 原录屏里的系统鼠标只有十几像素, 缩到演示片里几乎看不见 ——
    观众看不出操作发生在哪里, 而这正是演示最需要传达的信息。

    做法: 一个白色描边的实心三角指针(带黑色外描边保证在任何底色上都可见)
    + 外圈光环。点击用黄色光环, 滑动用青色, 两种动作在视觉上也能区分。
    """
    from PIL import Image, ImageDraw, ImageFilter

    size = CURSOR_W
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    ring = (245, 214, 62) if kind == "click" else (96, 214, 232)
    cx, cy = size // 2, size // 2

    # 扩散光圈: 两层 —— 实心细环 + 外层柔光
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.ellipse([cx - 62, cy - 62, cx + 62, cy + 62], outline=ring + (210,), width=7)
    hd.ellipse([cx - 44, cy - 44, cx + 44, cy + 44], outline=ring + (120,), width=3)
    halo = halo.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(halo)

    # 指针: 画一个标准的箭头形状, 黑色描边 + 白色填充
    tip = (cx - 8, cy - 10)
    pts = [
        tip,
        (tip[0] + 4, tip[1] + 34),
        (tip[0] + 12, tip[1] + 26),
        (tip[0] + 20, tip[1] + 42),
        (tip[0] + 28, tip[1] + 38),
        (tip[0] + 20, tip[1] + 22),
        (tip[0] + 30, tip[1] + 20),
    ]
    d.polygon(pts, fill=(255, 255, 255, 245), outline=(20, 20, 20, 255))
    d.line(pts + [pts[0]], fill=(20, 20, 20, 255), width=4, joint="curve")
    img.save(path)
