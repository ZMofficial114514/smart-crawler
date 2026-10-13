"""
补充场景 —— 把总时长做到 4 分钟左右。

内容同样取自仓库真实实现(iframe 下探 / 选择器生成 / 网络抓包 / 瀑布流合并 / 验收脚本),
数字都经实测核对: 控制台 7 个页面、57 项配置、43 个 verify 脚本、6 个内置插件。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from render_kit import (  # noqa: E402
    GREY, GREY_DIM, H, LINE, W, WHITE, YELLOW, YELLOW_DIM,
    arrow, cn, cn_light, ease_out, fade, latin, mono, mono_cjk, rounded, seg,
    text_at, text_center,
)

#: 时间轴拉伸比例 = 实际时长 / 设计时长。**由渲染器逐场景注入**。
#: 1.0 表示不拉伸(设计时长即实际时长)。
K = 1.0

#: 当前场景的总时长(秒)。**由渲染器在每个场景绘制前注入** —— 补时(HOLD)会
#: 改变时长, 而淡出时机必须跟着走, 否则内容早早淡掉、剩一段空屏。
DUR = 0.0


def scene_iframe(d: ImageDraw.ImageDraw, t: float) -> None:
    """iframe 下探 —— 外壳 + 内嵌 frame 的站点。"""
    """iframe 下探 —— 外壳 + 内嵌 frame 的站点。"""
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 160, "内容藏在内层 iframe 里", cn(60), WHITE, a)
    text_at(d, 200, 240, "outer shell + contentFrame", latin(28), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 外层 shell
    pa = fade(t, 1.0, 1.8, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 320, 1160, 880), 16, fill=(46, 46, 46), outline=LINE,
            width=2, alpha=pa)
    text_at(d, 232, 344, "主文档 (outer shell)", cn(28), GREY, pa)
    for i in range(5):
        d.rectangle([232 + i * 130, 396, 336 + i * 130, 428],
                    fill=(72, 72, 72) if pa >= 255 else (72, 72, 72, pa))
    text_at(d, 232, 470, "导航与播放条 —— 没有目标列表", cn_light(26), GREY, pa)
    text_at(d, 232, 516, "候选列表 0 个", cn(30), YELLOW_DIM, pa)

    # 内层 frame(高亮)
    pb = fade(t, 2.2, 3.2, hold_to=DUR - 0.9, tmax=DUR)
    d.rectangle([300, 580, 1060, 840],
                fill=(70, 66, 40) if pb >= 255 else (70, 66, 40, pb),
                outline=YELLOW if pb >= 255 else YELLOW + (pb,), width=4)
    text_at(d, 324, 604, "contentFrame  (同源内层)", cn(26), YELLOW, pb)
    for i in range(3):
        d.rectangle([324, 664 + i * 56, 1036, 704 + i * 56],
                    fill=(92, 86, 52) if pb >= 255 else (92, 86, 52, pb))
    text_at(d, 324, 742, "150 条记录 · 955 个元素", cn_light(24), WHITE, pb)

    # 右侧要点
    pc = fade(t, 4.0, 5.0, hold_to=DUR - 0.9, tmax=DUR)
    bullets = [
        "分析器遍历同源 frame, 取内容最丰富的一个",
        "提取阶段用同一个 frame 取数",
        "否则分析与提取看的是两个文档",
        "内层未就绪时重载页面再分析一次",
    ]
    for i, s in enumerate(bullets):
        text_at(d, 1240, 340 + i * 92, "·", cn(34), YELLOW,
                fade(t, 4.2 + i * 0.35, 5.1 + i * 0.35, hold_to=DUR - 0.9, tmax=DUR))
        # 每行最多 18 字, 超出折行
        if len(s) <= 18:
            text_at(d, 1284, 340 + i * 92, s, cn_light(27), WHITE,
                    fade(t, 4.2 + i * 0.35, 5.1 + i * 0.35, hold_to=DUR - 0.9, tmax=DUR))
        else:
            text_at(d, 1284, 336 + i * 92, s[:18], cn_light(27), WHITE,
                    fade(t, 4.2 + i * 0.35, 5.1 + i * 0.35, hold_to=DUR - 0.9, tmax=DUR))
            text_at(d, 1284, 372 + i * 92, s[18:], cn_light(27), WHITE,
                    fade(t, 4.2 + i * 0.35, 5.1 + i * 0.35, hold_to=DUR - 0.9, tmax=DUR))


def scene_selector(d: ImageDraw.ImageDraw, t: float) -> None:
    """选择器生成 —— 唯一选择器 + 覆盖度修正。"""
    """选择器生成 —— 唯一选择器 + 覆盖度修正。"""
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 160, "唯一选择器生成", cn(60), WHITE, a)
    text_at(d, 200, 240, "id > data-* > 稳定 class > 层级路径", mono_cjk(28), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 三段式: 候选规则 -> 实测 -> 修正
    cols = [
        ("生成", ["优先最短的唯一选择器", "逐级缩短作为兜底"]),
        ("实测", ["拿候选去每个列表项验证", "覆盖不全才替换该字段"]),
        ("修正", [":is(div,a).info_value", "一类多标签写法一并命中"]),
    ]
    for i, (head, lines) in enumerate(cols):
        pa = fade(t, 1.2 + i * 0.7, 2.0 + i * 0.7, hold_to=DUR - 0.9, tmax=DUR)
        x = 200 + i * 520
        rounded(d, (x, 340, x + 460, 640), 16, fill=(48, 48, 48), outline=LINE,
                width=2, alpha=pa)
        d.line([(x + 28, 380), (x + 108, 380)], fill=YELLOW, width=5)
        text_at(d, x + 28, 402, head, cn(34), WHITE, pa)
        for j, s in enumerate(lines):
            text_at(d, x + 28, 470 + j * 52, s, cn_light(25), GREY, pa)

    pb = fade(t, 4.0, 5.0, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 700, 1720, 900), 16, fill=(44, 44, 44), outline=LINE,
            width=2, alpha=pb)
    text_at(d, 236, 724, "实测", cn(28), YELLOW, pb)
    text_at(d, 320, 722, "网易云专辑列  11/30 行 → 30/30 行", cn(30), WHITE, pb)
    text_at(d, 320, 774, "kivo 信息值列  1/8 行 → 8/8 行", cn(30), WHITE, pb)
    text_at(d, 320, 826, "网易云 / 酷我其余字段行为逐字节不变", cn_light(26), GREY, pb)


def scene_sniff(d: ImageDraw.ImageDraw, t: float) -> None:
    """网络抓包 —— 接口里找数据。"""
    """网络抓包 —— 接口里找数据。"""
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 160, "数据在接口里, 不在 DOM 里", cn(60), WHITE, a)
    text_at(d, 200, 240, "XHR · fetch · WebSocket", latin(28), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 请求列表
    pa = fade(t, 1.0, 1.8, hold_to=DUR - 0.9, tmax=DUR)
    rows = [
        ("GET", "/api/search?key=...", "JSON", 200),
        ("GET", "/api/detail/114", "JSON", 200),
        ("POST", "/api/collect", "plain", 200),
        ("GET", "/static/app.js", "js", 200),
    ]
    for i, (m, url, kind, code) in enumerate(rows):
        pr = fade(t, 1.2 + i * 0.4, 1.9 + i * 0.4, hold_to=DUR - 0.9, tmax=DUR)
        y = 340 + i * 76
        rounded(d, (200, y, 1180, y + 60), 10, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pr)
        text_at(d, 224, y + 16, m, mono(24), YELLOW_DIM, pr)
        text_at(d, 300, y + 16, url, mono(24), WHITE, pr)
        text_at(d, 1060, y + 16, kind, mono(22), GREY, pr)
        text_at(d, 1150, y + 16, str(code), mono(22), GREY, pr)

    # 要点
    pb = fade(t, 3.4, 4.4, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 700, "两条用法", cn(32), YELLOW, pb)
    for i, s in enumerate(["由接口响应生成 mode=\"json\" 的规则, 直接抓 JSON",
                           "嗅探器从已捕获响应里翻找媒体地址, 逐个试探验证"]):
        text_at(d, 236, 764 + i * 56, "· " + s, cn_light(27), WHITE,
                fade(t, 3.8 + i * 0.3, 4.6 + i * 0.3, hold_to=DUR - 0.9, tmax=DUR))

    pc = fade(t, 6.0, 7.0, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 894, 1720, 966), 12, fill=(46, 46, 46), alpha=pc)
    text_at(d, 236, 912, "截断的地址不用猜 —— 试探请求就是最好的判据",
            cn(28), YELLOW, pc)


def scene_masonry(d: ImageDraw.ImageDraw, t: float) -> None:
    """瀑布流合并 —— 多列被拆成多组时的处理。"""
    """瀑布流合并 —— 多列被拆成多组时的处理。"""
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 160, "瀑布流不会被拆成三份", cn(60), WHITE, a)
    text_at(d, 200, 240, "masonry merge", latin(28), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 左: 被拆开的三列
    pa = fade(t, 1.0, 1.8, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 330, "合并前", cn(32), GREY, pa)
    for c in range(3):
        x = 200 + c * 200
        for r in range(3):
            d.rectangle([x, 400 + r * 120, x + 170, 500 + r * 120],
                        fill=(62, 62, 62) if pa >= 255 else (62, 62, 62, pa),
                        outline=LINE if pa >= 255 else LINE + (pa,), width=2)
    text_at(d, 200, 790, "class 带“列”信息 → 被拆成 3 组", cn_light(25), GREY, pa)
    text_at(d, 200, 836, "规则只覆盖一列, 另外两列静默丢失", cn_light(25), YELLOW_DIM, pa)

    # 中标
    arrow(d, 830, 520, 950, 520, YELLOW, 6, fade(t, 2.2, 3.0, hold_to=DUR - 0.9, tmax=DUR), 24)
    text_center(d, 460, "并集可被一个\n更宽的选择器选中", cn(28), YELLOW,
                fade(t, 2.2, 3.0, hold_to=DUR - 0.9, tmax=DUR), xy=(890, 470))

    # 右: 合并后
    pb = fade(t, 3.2, 4.2, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 1010, 330, "合并后", cn(32), YELLOW, pb)
    for c in range(3):
        x = 1010 + c * 200
        for r in range(3):
            d.rectangle([x, 400 + r * 120, x + 170, 500 + r * 120],
                        fill=(70, 66, 40) if pb >= 255 else (70, 66, 40, pb),
                        outline=YELLOW if pb >= 255 else YELLOW + (pb,), width=2)
    text_at(d, 1010, 790, "24 张卡片合并为一个列表", cn_light(25), WHITE, pb)
    text_at(d, 1010, 836, "规则覆盖全部条目", cn(30), YELLOW, pb)


def scene_quality(d: ImageDraw.ImageDraw, t: float) -> None:
    """工程化: 43 个验收脚本。"""
    """工程化: 43 个验收脚本。"""
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 160, "每个修复都有脚本守着", cn(60), WHITE, a)
    text_at(d, 200, 240, "acceptance scripts", latin(28), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 大字计数
    pn = ease_out(seg(t, 1.0, 2.4))
    num = int(43 * pn)
    text_at(d, 200, 340, str(num), cn(150), YELLOW, 255)
    text_at(d, 200, 520, "个 verify 脚本", cn(34), WHITE,
            fade(t, 1.4, 2.2, hold_to=DUR - 0.9, tmax=DUR))

    groups = [
        ("结构与规则", "选择器生成 / 专辑列 / 信息值列 / iframe 下探"),
        ("媒体与插件", "字段识别 / 数量上限 / 嗅探 / 音频流 / 视频完整性"),
        ("站点回归", "网易云 / 酷我 / 洛谷 / 堆糖场景"),
        ("UI 与接口", "沙箱 / 布局 / 告警 / URL 归类 / 访问诊断"),
    ]
    for i, (head, sub) in enumerate(groups):
        pa = fade(t, 2.4 + i * 0.45, 3.1 + i * 0.45, hold_to=DUR - 0.9, tmax=DUR)
        y = 340 + i * 130
        rounded(d, (760, y, 1720, y + 108), 14, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pa)
        d.line([(786, y + 30), (846, y + 30)], fill=YELLOW, width=5)
        text_at(d, 786, y + 46, head, cn(32), WHITE, pa)
        text_at(d, 1050, y + 48, sub, cn_light(24), GREY, pa)

    pb = fade(t, 5.2, 6.2, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, 900, "改一处, 跑一遍 —— 退化立刻暴露", cn(34), YELLOW, pb)
