"""
SmartCrawler 介绍动画渲染器。

风格对齐参考视频: 深灰细纹理背景 + 白色超粗中文大字 + 斜体英文副题 + 黄色强调词/箭头/字幕,
全屏图形页(不含任何录屏画面)。

做法: Pillow 逐帧绘制 -> 原始 RGB 经 stdout 管道喂给 ffmpeg。
不落盘中转帧(D4 素材 + 7200 帧会占几个 GB), 也不引入需要联网的库。

用法:
    python make_intro_video.py [输出路径] [--fps 30] [--size 1920x1080]
"""

from __future__ import annotations

import math
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 画布与调色
# ---------------------------------------------------------------------------
W, H = 1920, 1080
FPS = 30


def set_size(w: int, h: int) -> None:
    """改画布尺寸。

    **必须在 import 场景模块之前调用。** 场景模块写的是 `from render_kit import W, H`,
    那会把当时的值**绑进它们的模块命名空间** —— 之后改 `render_kit.W` 对它们毫无影响,
    画面会按旧尺寸绘制、在画布上错位, 而且不报任何错。`assert_size_matches` 就是用来
    把这种"静默画错"变成一开机就炸。
    """
    global W, H, _bg_cache
    W, H = int(w), int(h)
    _bg_cache = None


def assert_size_matches(w: int, h: int) -> None:
    """确认场景模块里绑定的尺寸与本模块一致。"""
    import scenes as _sc
    import scenes_extra as _sx

    bad = []
    for mod in (_sc, _sx):
        if (getattr(mod, "W", w), getattr(mod, "H", h)) != (w, h):
            bad.append(f"{mod.__name__}: {getattr(mod, 'W', '?')}x"
                       f"{getattr(mod, 'H', '?')}")
    if bad:
        raise RuntimeError(
            "场景模块绑定了旧的画布尺寸(它们用 `from render_kit import W, H`)。"
            f"期望 {w}x{h}, 实际 {'; '.join(bad)}。"
            "请先 set_size() 再 import 场景模块, 或改用 render_kit.W 形式引用。")

BG = (58, 58, 58)            # 深灰底(与参考视频一致的炭灰)
BG_NOISE = 8                 # 背景噪点强度, 制造"细纹理"而不是纯色块
WHITE = (255, 255, 255)
YELLOW = (245, 214, 62)      # 强调黄
YELLOW_DIM = (196, 172, 60)
GREY = (150, 150, 150)
GREY_DIM = (104, 104, 104)
LINE = (78, 78, 78)

FONT_DIR = pathlib.Path("C:/Windows/Fonts")
F_BOLD = FONT_DIR / "msyhbd.ttc"      # 微软雅黑 Bold
F_REG = FONT_DIR / "msyh.ttc"         # 微软雅黑 Regular
F_MONO = FONT_DIR / "consolab.ttf"    # Consolas Bold
F_LATIN = FONT_DIR / "seguisb.ttf"    # Segoe UI Semibold

if not F_BOLD.exists():
    F_BOLD = FONT_DIR / "simhei.ttf"
if not F_REG.exists():
    F_REG = F_BOLD

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: pathlib.Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def cn(size: int) -> ImageFont.FreeTypeFont:
    return font(F_BOLD, size)


def cn_light(size: int) -> ImageFont.FreeTypeFont:
    return font(F_REG, size)


def latin(size: int) -> ImageFont.FreeTypeFont:
    return font(F_LATIN, size)


def mono(size: int) -> ImageFont.FreeTypeFont:
    return font(F_MONO, size)


#: Consolas **不含中文字形** —— 拿它写中文会渲染成一排豆腐块(实测踩到:
#: "样本字段 title / price / link" 里混的中文全成了方框)。
#: 凡是可能混中文的地方一律用这个: 等宽的感觉保留不了, 但至少字是对的。
def mono_cjk(size: int) -> ImageFont.FreeTypeFont:
    return font(F_BOLD, size)


def _glyph_pixels(f: ImageFont.FreeTypeFont, ch: str, box: int = 64) -> bytes:
    """把一个字符画到固定画布上, 返回像素字节(用作字形指纹)。"""
    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((box // 2, box // 2), ch, font=f, fill=255, anchor="mm")
    return img.tobytes()


def has_glyphs(f: ImageFont.FreeTypeFont, s: str) -> bool:
    """字符串里的字符是否都能被这个字体画出来(用来防豆腐块)。

    走过的弯路, 记下来免得再踩:
      1. "`getmask(ch).getbbox()` 非空" —— **错的**。字体没有该字形时 Pillow 会去画
         `.notdef`(通常是个方框), getbbox() 照样非空, 于是"画不出"被判成"画得出"。
      2. "`getmask(ch).tobytes()` 与 `.notdef` 比对" —— **直接抛异常**。Pillow 里
         `getmask()` 返回的是 `ImagingCore`, 没有 `tobytes()`; 而我当时把它包在
         `except` 里默认返回 True, 于是**所有字符都被判成有字形**, 检查形同虚设。

    现在的做法: 把字符画到固定画布上取像素指纹, 与一个**确定未分配**的码位
    (U+10FFFF)比对 —— 位图一致就说明它也是 .notdef。
    """
    try:
        notdef = _glyph_pixels(f, "\U0010ffff")
    except Exception:  # noqa: BLE001
        return True
    for ch in s:
        if ch.isspace():
            continue
        try:
            if _glyph_pixels(f, ch) == notdef:
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


# ---------------------------------------------------------------------------
# 缓动
# ---------------------------------------------------------------------------
def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def seg(t: float, a: float, b: float) -> float:
    """把绝对时间 t 映射成 [a,b] 区间内的 0..1 进度。"""
    if b <= a:
        return 1.0 if t >= b else 0.0
    return clamp01((t - a) / (b - a))


def ease_out(x: float) -> float:
    return 1 - (1 - clamp01(x)) ** 3


def ease_in_out(x: float) -> float:
    x = clamp01(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ease_out_back(x: float) -> float:
    x = clamp01(x)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


#: 淡入淡出**恒定**占用的时间(秒)。
#:
#: 为什么需要它: 场景会因为"让观众看完字幕"而加长(见 `Scene.design`), 绘制函数里的绝对
#: 时刻保持原样、只把整体拉长。于是入场淡入也跟着被拉长 —— 实测拉长到 1.39 倍时, 一个
#: 0.8 秒的淡入变成 1.1 秒, 画面上出现大量"内容还没出来/已经淡掉"的空档。
#: 这里把淡入淡出归一化: 不管场景多长, 过渡都只花这么多时间, 只有**停留**被拉长。
FADE_IN = 0.6
FADE_OUT = 0.45


def fade(t: float, a: float, b: float, hold_to: float | None = None,
         fade_out: float = FADE_OUT, tmax: float = 0.0) -> int:
    """返回 0..255 的透明度: a..b 淡入, 可选在 hold_to 之后淡出。

    ``tmax`` 非 0 时启用**时间轴归一化**。绘制函数里的时刻(a/b/hold_to)是按"设计时长"写的,
    而实际场景可能更长或更短, 于是先把 t 从实际轴线性映射回设计轴:

        实际 [0, tmax]  ->  设计 [0, design]

    映射后所有写死的时刻含义不变, 场景整体被等比拉伸/压缩。**但淡入淡出必须例外** ——
    映射会把 0.8 秒的淡入也拉成 1.1 秒, 画面上于是出现大量"内容还没出来"的空档(实测
    拉到 1.39 倍时 t=96s 那帧几乎全空)。所以这里只让**停留**跟着拉伸, 过渡恒定为
    FADE_IN / FADE_OUT 秒。

    ``design`` 通过 tmax 与场景时长一起由渲染器注入。
    """
    if tmax > 0:
        # 设计轴上的关键位置换算到实际轴: 实际 = 设计 * (实际时长 / 设计时长)
        scale = tmax / max(1e-6, _DESIGN[0]) if _DESIGN[0] else 1.0
        at = a * scale
        if hold_to is not None:
            ht = hold_to * scale
            if t < at:
                alpha = 0.0
            elif t < at + FADE_IN:
                alpha = ease_out((t - at) / FADE_IN)
            elif t <= ht:
                alpha = 1.0
            else:
                alpha = 1.0 - ease_out(clamp01((t - ht) / FADE_OUT))
            return int(255 * clamp01(alpha))
        alpha = ease_out(clamp01((t - at) / FADE_IN))
        return int(255 * clamp01(alpha))

    alpha = ease_out(seg(t, a, b))
    if hold_to is not None and t > hold_to:
        alpha *= 1.0 - ease_out(seg(t, hold_to, hold_to + fade_out))
    return int(255 * clamp01(alpha))


#: 当前场景的时间轴信息, 由渲染器注入(与 DUR/K 一起)。fade 的归一化靠它。
#: `design` 是绘制函数里那些绝对时刻所依据的时长; `actual` 是加了补时之后的真实时长。
_DESIGN = [0.0, 0.0]


def set_design(design: float, actual: float = 0.0) -> None:
    """注入设计时长与实际时长。

    ``fade(t, a, hold_to, tmax=实际时长)`` 内部按 ``scale = 实际 / 设计`` 把设计轴上的
    ``a`` / ``hold_to`` 换算到实际轴。**只传设计时长会让 scale 恒为 1**, 停留期消失、
    画面提前清空 —— 所以两个都要传。
    """
    _DESIGN[0] = float(design)
    _DESIGN[1] = float(actual)


# ---------------------------------------------------------------------------
# 背景: 细纹理噪点(一次生成, 全片复用)
# ---------------------------------------------------------------------------
_bg_cache: Image.Image | None = None


def background() -> Image.Image:
    global _bg_cache
    if _bg_cache is not None:
        return _bg_cache
    import numpy as np

    rng = np.random.default_rng(20261013)
    noise = rng.integers(-BG_NOISE, BG_NOISE + 1, size=(H // 2, W // 2), dtype=np.int16)
    small = np.clip(np.array(BG, dtype=np.int16)[None, None, :] + noise[:, :, None],
                    0, 255).astype("uint8")
    img = Image.fromarray(small, "RGB").resize((W, H), Image.BILINEAR)
    _bg_cache = img
    return img


# ---------------------------------------------------------------------------
# 绘图原语
# ---------------------------------------------------------------------------
def text_center(d: ImageDraw.ImageDraw, y: int, s: str, f: ImageFont.FreeTypeFont,
                fill=WHITE, alpha: int = 255, xy: tuple[int, int] | None = None,
                anchor: str = "mm") -> None:
    x = W // 2 if xy is None else xy[0]
    yy = y if xy is None else xy[1]
    if alpha <= 0:
        return
    if alpha >= 255:
        d.text((x, yy), s, font=f, fill=fill, anchor=anchor)
    else:
        d.text((x, yy), s, font=f, fill=fill + (alpha,), anchor=anchor)


def text_at(d: ImageDraw.ImageDraw, x: int, y: int, s: str,
            f: ImageFont.FreeTypeFont, fill=WHITE, alpha: int = 255,
            anchor: str = "la") -> None:
    if alpha <= 0:
        return
    if alpha >= 255:
        d.text((x, y), s, font=f, fill=fill, anchor=anchor)
    else:
        d.text((x, y), s, font=f, fill=fill + (alpha,), anchor=anchor)


def measure(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = d.textbbox((0, 0), s, font=f)
    return box[2] - box[0], box[3] - box[1]


def arrow(d: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int,
          color=YELLOW, width: int = 5, alpha: int = 255, head: int = 22) -> None:
    """带箭头的直线。alpha 通过叠加层实现。"""
    if alpha <= 0:
        return
    c = color if alpha >= 255 else color + (alpha,)
    d.line([(x1, y1), (x2, y2)], fill=c, width=width)
    ang = math.atan2(y2 - y1, x2 - x1)
    for sgn in (0.5, -0.5):
        d.line([(x2, y2),
                (int(x2 - head * math.cos(ang + sgn)),
                 int(y2 - head * math.sin(ang + sgn)))], fill=c, width=width)


def rounded(d: ImageDraw.ImageDraw, box, r: int = 14, fill=None, outline=None,
            width: int = 2, alpha: int = 255) -> None:
    if alpha <= 0:
        return
    f = fill if (fill is None or alpha >= 255) else fill + (alpha,)
    o = outline if (outline is None or alpha >= 255) else outline + (alpha,)
    d.rounded_rectangle(box, radius=r, fill=f, outline=o, width=width)


_NOISE_WORDS = ["Playwright", "LLM", "DOM", "XHR", "WebSocket", "iframe", "JSON",
                "selector", "Pagination", "robots.txt", "CSV", "SQLite",
                "candidate", "unwrap", "dedupe", "webhook", "MCP", "Agent",
                "Prompt", "Tool", "Skill", "async", "regex", "JSONPath"]


def noise_layer(seed: int, n: int, f: ImageFont.FreeTypeFont,
                color=WHITE, alpha: int = 38) -> Image.Image:
    """生成一层"漂浮关键词"贴图(参考视频背景里那些散落的 MCP / Agent / Tool)。

    做成整层贴图而不是逐个文本: 这样可以一次性旋转/模糊, 而且每帧只要有位移就能产生
    "缓慢漂移"的动感, 成本远低于逐词重画。

    **中间区域会被压暗**: 参考视频里这些词只出现在边缘, 正文区是干净的。不这么做的话,
    它们会正好压在标题和卡片上(实测封面那帧 "Playwright" 糊在 SmartCrawler 上), 可读性
    直接崩掉。压暗用椭圆遮罩 + 高斯羽化, 过渡自然。
    """
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = seed
    for i in range(n):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        px = 90 + (rng >> 8) % (W - 180)
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        py = 90 + (rng >> 8) % (H - 180)
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        ang = (rng >> 8) % 360
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        size = 34 + (rng >> 8) % 46
        word = _NOISE_WORDS[(seed + i * 7) % len(_NOISE_WORDS)]

        tmp = Image.new("RGBA", (720, 200), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        td.text((360, 100), word, font=latin(size), fill=color + (alpha,), anchor="mm")
        tmp = tmp.rotate(ang, resample=Image.BICUBIC, expand=False)
        layer.alpha_composite(tmp, (px - 360, py - 100))

    # 中央遮罩: 正文区留白。椭圆比矩形自然, 再羽化让边缘没有硬边。
    mask = Image.new("L", (W, H), 255)
    md = ImageDraw.Draw(mask)
    mx, my = int(W * 0.40), int(H * 0.37)
    md.ellipse([W // 2 - mx, H // 2 - my, W // 2 + mx, H // 2 + my], fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(90))
    layer.putalpha(Image.fromarray(
        (np.asarray(layer.getchannel("A"), dtype=np.float32)
         * (np.asarray(mask, dtype=np.float32) / 255.0)).astype("uint8")))
    return layer


# ---------------------------------------------------------------------------
# 场景数据模型
# ---------------------------------------------------------------------------
@dataclass
class Scene:
    """一个场景 = 一段时长 + 一个逐帧绘制函数。"""

    key: str
    dur: float
    draw: Callable[[ImageDraw.ImageDraw, float], None]
    caption: str = ""
    caption_from: float = 0.6
    tag: str = ""          # 右上角标签
    notes: list[str] = field(default_factory=list)
    #: 动画的**设计时长**(秒) —— 绘制函数里所有写死的时刻都是按它排的。
    #: 实际 dur 可能因"看完字幕的补时"而更长; 渲染器按 `dur / design` 算出拉伸比例 K 注入,
    #: 场景内 `t = t / K` 后, 入场/关键帧/淡出就会等比铺满整段, 而不是挤在开头留一尾巴空屏。
    #: 为 0 时视为不拉伸(design = dur)。
    design: float = 0.0


# ---------------------------------------------------------------------------
# 通用页头: 右上角标签(参考视频一直挂着 "FPS | GPU | CPU")
# ---------------------------------------------------------------------------
def draw_tag(d: ImageDraw.ImageDraw, s: str, alpha: int = 90) -> None:
    if not s:
        return
    # 用 mono_cjk: 标签里可能混中文(如 "选择器"), Consolas 会画成豆腐块
    text_at(d, W - 60, 46, s, mono_cjk(20), GREY, alpha, anchor="ra")


# ---------------------------------------------------------------------------
# 字幕: 底部居中黄字(参考视频的做法)
# ---------------------------------------------------------------------------
def draw_caption(d: ImageDraw.ImageDraw, s: str, alpha: int) -> None:
    if not s or alpha <= 0:
        return
    text_center(d, H - 92, s, cn(38), YELLOW, alpha)


__all__ = [
    "W", "H", "FPS", "BG", "WHITE", "YELLOW", "GREY", "LINE",
    "set_size", "assert_size_matches",
    "cn", "cn_light", "latin", "mono", "mono_cjk", "has_glyphs",
    "seg", "clamp01", "ease_out", "ease_in_out", "ease_out_back", "fade",
    "background", "text_center", "text_at", "measure", "arrow", "rounded",
    "draw_tag", "draw_caption", "noise_layer", "Scene",
]
