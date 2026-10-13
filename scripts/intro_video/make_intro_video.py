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

import demo_kit as DK  # noqa: E402
import render_kit as rk  # noqa: E402
import scenes as SC  # noqa: E402
import scenes_easy as SE  # noqa: E402
import scenes_extra as SX  # noqa: E402

PROJ = HERE.parent.parent
FFMPEG = PROJ / "tools" / "ffmpeg" / "ffmpeg.exe"

#: 每场景的额外停留时间(秒)。场景表里的 dur 已经包含正文动画, 这里再加"看字幕"的时间。
#: 调这个值就能整体拉长/压缩成片。
#:
#: 从 2.4 收到 1.7: 观众反馈"略显磨叽"。1.7 秒够看完一行字幕, 又不至于让画面停在那儿干等。
HOLD = 1.7

#: 明确要"松一点"的场景(封面与结尾值得多停一拍; 技术细节段可以更快)
HOLD_EXTRA = {"title": 1.2, "end": 1.0, "flow": -0.4, "quality": -0.4,
              "plugins": -0.3, "masonry": -0.3}


def build_all() -> list[rk.Scene]:
    """按**显式顺序**拼出完整场景表, 再做补时。

    顺序按观众的理解曲线排, 不按模块清单:
      问题 -> 说人话解释是什么 -> 能干什么 -> 怎么做(架构/细节) -> 用起来什么样
      -> 边界与合规 -> 三步上手

    早先这函数是"遍历基础表, 遇到某个 key 就 append 一个补充场景"。那种写法有个坑:
    **新 append 的场景自己不会被再遍历到**, 于是挂在它后面的插入条件永远不触发 ——
    `why` 与 `boundary` 就这样静默丢了(总时长只差几秒, 不看顺序表根本发现不了)。
    改成显式列表后, 顺序一眼可读, 名字写错也会当场 KeyError。
    """
    base = {s.key: s for s in SC.build_scenes()}

    def take(key: str) -> rk.Scene:
        if key not in base:
            raise KeyError(f"场景表里没有 {key!r} —— 拼顺序时名字写错了"
                           f"(现有: {sorted(base)})")
        return base[key]

    seq: list[rk.Scene] = [
        # --- 开场: 先建立"这东西对我有什么用" ---
        take("title"),
        take("pain"),
        rk.Scene("whatis", 11.0, SE.scene_whatis,
                 "把网页上要的内容, 自动搬进表格", 3.6, "说人话"),
        rk.Scene("why", 10.2, SE.scene_why,
                 "比价 / 收资料 / 归档收藏 / 盯变化", 3.8, "用途"),
        # --- 怎么做到: 架构与关键机制 ---
        take("pillars"),
        rk.Scene("iframe", 11.4, SX.scene_iframe,
                 "分析与提取必须在同一个 frame 上", 4.6, "IFRAME"),
        take("flow"),
        # --- 用起来什么样: 控制台与抓包 ---
        take("console"),
        rk.Scene("sniff", 11.0, SX.scene_sniff,
                 "捕获 XHR/fetch/WebSocket 并解析 JSON", 3.8, "网络抓包"),
        # --- 具体能力 ---
        take("ai"),
        rk.Scene("selector", 11.4, SX.scene_selector,
                 "覆盖不全才替换, 覆盖满就不动手", 4.8, "选择器"),
        take("scope"),
        rk.Scene("masonry", 10.4, SX.scene_masonry,
                 "并集可被一个更宽的选择器选中, 那就合并", 3.6, "瀑布流"),
        take("media"),
        take("plugins"),
        # --- 工程可信度 ---
        take("robust"),
        rk.Scene("quality", 11.0, SX.scene_quality,
                 "49 个脚本: 43 个 verify + 6 个 check", 4.6, "验收"),
        # --- 边界与合规(诚实交代, 对宣传反而加分) ---
        rk.Scene("boundary", 10.4, SE.scene_boundary,
                 "robots / 限速 / 付费内容 —— 都不绕过", 3.6, "边界"),
        take("compliance"),
        # --- 收尾: 说人话的上手步骤, 再给技术版快速开始 ---
        rk.Scene("steps", 10.6, SE.scene_steps,
                 "会打字就能用", 3.4, "上手"),
        take("start"),
        take("end"),
    ]
    for s in seq:
        if not s.design:
            s.design = s.dur
        s.dur += HOLD + HOLD_EXTRA.get(s.key, 0.0)
    return seq


def inject_time(sc: rk.Scene) -> float:
    """把本场景的时长与拉伸比例注入场景模块与 render_kit, 返回 K。

    fade 的归一化要两个量: **设计时长**(把设计轴上的 a/hold_to 换算到实际轴)与
    **实际时长**(换算的分母, 即 scale = 实际/设计)。传错会让停留期直接消失 ——
    单测抓到过一次(淡出提前到 13.5s 就把画面清空)。
    """
    k = (sc.dur / sc.design) if sc.design else 1.0
    rk.set_design(sc.design or sc.dur, sc.dur)
    for mod in (SC, SX, SE):
        mod.DUR = sc.dur
        mod.K = k
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
           with_audio: bool = True, keep: list[str] | None = None) -> int:
    # 尺寸必须在场景模块 import **之前**设定 —— 它们用 `from render_kit import W, H`,
    # 会把当时的值绑进自己的命名空间。render_kit.set_size 会清掉背景缓存。
    rk.set_size(*size)
    rk.assert_size_matches(*size)
    scenes = build_all()
    if keep:
        scenes = [s for s in scenes if s.key in keep]
        if not scenes:
            print(f"  --scenes 没匹配到任何场景")
            return 2
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
        # 中间产物统一放 .runtmp/(已被 .gitignore 排除), 别散在 scripts/ 里 ——
        # 之前就因为这个, git status 里冒出过 14MB 的 mp4 和 5.7MB 的 wav 等着被误提交。
        wav = DK.ensure_workdir() / "music.wav"
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
    ap.add_argument("--scenes", default="",
                    help="只渲染这些场景, 逗号分隔的段(如 'title:sniff' 或 "
                         "'boundary,end'), 便于把介绍片拆成片头/片尾夹住演示片段")
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
    try:
        keep = parse_scene_filter(args.scenes)
    except (KeyError, ValueError) as exc:
        print(f"  --scenes 参数有问题: {exc}")
        return 2
    return render(out, args.fps, (w, h), with_audio=not args.no_audio,
                  keep=keep)


def parse_scene_filter(spec: str) -> list[str] | None:
    """把 ``--scenes`` 解析成要保留的 key 列表; 空串返回 None(表示全部)。

    支持两种写法, 可混用逗号分隔:
      - 单个 key:  ``title``
      - 闭区间:    ``title:sniff``  —— 按 build_all 的实际顺序取这一段(含两端)

    存在的意义: 用户要把演示录屏夹在介绍片**中间**, 那就需要把介绍片拆成"片头"和"片尾"
    两次渲染。用区间写法比手敲二十几个 key 靠谱得多。
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    order = [s.key for s in build_all()]
    picked: list[str] = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        if ":" in part:
            a, _, b = part.partition(":")
            a, b = a.strip(), b.strip()
            for name in (a, b):
                if name not in order:
                    raise KeyError(f"没有场景 {name!r}(现有: {order})")
            i, j = order.index(a), order.index(b)
            if i > j:
                raise ValueError(f"区间起点 {a!r} 在终点 {b!r} 之后")
            picked.extend(order[i:j + 1])
        else:
            if part not in order:
                raise KeyError(f"没有场景 {part!r}(现有: {order})")
            picked.append(part)
    # 去重但保持顺序
    seen: set[str] = set()
    out = [k for k in picked if not (k in seen or seen.add(k))]
    return out


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
