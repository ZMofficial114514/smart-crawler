"""
面向大众的补充场景 —— 让完全不懂爬虫的人也能看懂。

原片是从"技术介绍"角度写的(四个支柱 / 数据流 / 插件系统), 对没有爬虫背景的观众门槛偏高。
这里补几段**用日常语言**讲的:

  - 抓取到底是什么(用"翻页复制粘贴"作类比)
  - 手工做 vs 自动做(并排对比, 一眼看出价值)
  - 能拿来干什么(四个具体场景, 不讲技术)
  - 三步上手(说人话, 不甩命令)

同时把整体节奏收紧: 每段停留从 2.4s 降到 1.7s, 并把几个技术段的停留再压一点。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from render_kit import (  # noqa: E402
    GREY, GREY_DIM, H, LINE, W, WHITE, YELLOW, YELLOW_DIM,
    arrow, cn, cn_light, fade, latin, mono_cjk, rounded, seg, text_at,
    text_center,
)

#: 当前场景的总时长(秒)。由渲染器逐场景注入。
DUR = 0.0


def scene_whatis(d: ImageDraw.ImageDraw, t: float) -> None:
    """抓取是什么 —— 用"翻页复制粘贴"类比。"""
    a = fade(t, 0.05, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 150, "先说说「抓取」是什么", cn(58), WHITE, a)
    text_at(d, 200, 228, "说人话: 把网页上你想要的内容, 自动搬到表格里",
            cn_light(32), YELLOW, fade(t, 0.4, 1.4, hold_to=DUR - 0.9, tmax=DUR))

    # 左: 手工
    pa = fade(t, 1.4, 2.3, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 330, 900, 700), 16, fill=(48, 48, 48), outline=LINE,
            width=2, alpha=pa)
    text_at(d, 236, 358, "手工做法", cn(34), GREY, pa)
    steps = ["打开网页", "选中文字", "Ctrl+C", "切到表格", "Ctrl+V", "翻下一页…"]
    for i, s in enumerate(steps):
        text_at(d, 236, 420 + i * 44, f"{i + 1}.  {s}", cn_light(27), GREY, pa)
    pb = fade(t, 3.2, 4.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 236, 640, "100 条就是 100 遍", cn(30), YELLOW_DIM, pb)

    # 右: 自动
    pc = fade(t, 2.4, 3.3, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (1020, 330, 1720, 700), 16, fill=(70, 66, 40), outline=YELLOW,
            width=3, alpha=pc)
    text_at(d, 1056, 358, "用 SmartCrawler", cn(34), YELLOW, pc)
    text_at(d, 1056, 430, "告诉它一句话:", cn_light(27), WHITE, pc)
    text_at(d, 1056, 480, "「抓取书名和价格」", cn(34), WHITE, pc)
    text_at(d, 1056, 560, "它自己翻页、自己整理,", cn_light(27), WHITE, pc)
    text_at(d, 1056, 606, "最后给你一个 CSV 表格。", cn_light(27), WHITE, pc)

    arrow(d, 930, 515, 1000, 515, YELLOW, 6,
          fade(t, 1.9, 2.8, hold_to=DUR - 0.9, tmax=DUR), 22)


def scene_why(d: ImageDraw.ImageDraw, t: float) -> None:
    """能拿来干什么 —— 四个具体场景, 不讲技术。"""
    a = fade(t, 0.05, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 150, "它能帮你做什么", cn(58), WHITE, a)
    text_at(d, 200, 228, "下面这些事, 手工做都要几个小时", cn_light(32), YELLOW,
            fade(t, 0.4, 1.4, hold_to=DUR - 0.9, tmax=DUR))

    cases = [
        ("比价", "把十几个商城的同款价格抓下来, 对比哪个便宜"),
        ("收集资料", "把列表页的标题、作者、链接整理成一张表"),
        ("归档自己的收藏", "把歌单 / 书签 / 图集批量存到本地"),
        ("盯变化", "每天抓一次, 只留下有改动的部分"),
    ]
    for i, (head, sub) in enumerate(cases):
        pa = fade(t, 1.3 + i * 0.5, 2.1 + i * 0.5, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 350 + i * 132
        rounded(d, (200, y, 1720, y + 108), 14, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pa)
        d.line([(230, y + 32), (300, y + 32)], fill=YELLOW, width=5)
        text_at(d, 230, y + 46, head, cn(34), WHITE, pa)
        text_at(d, 620, y + 48, sub, cn_light(27), GREY, pa)

    pb = fade(t, 3.8, 4.7, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, H - 190, "数据留下来, 时间省下来", cn(34), YELLOW, pb)


def scene_steps(d: ImageDraw.ImageDraw, t: float) -> None:
    """三步上手 —— 说人话, 不甩命令。"""
    a = fade(t, 0.05, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 150, "用起来只要三步", cn(58), WHITE, a)

    steps = [
        ("①", "双击 start.bat", "自动装好环境、打开控制台 —— 不用敲命令"),
        ("②", "粘贴网页地址", "再把网址填进去, 就像发给朋友一样自然"),
        ("③", "说一句你要什么", "比如「抓取书名和价格」, 剩下的它自己做"),
    ]
    for i, (num, head, sub) in enumerate(steps):
        pa = fade(t, 1.1 + i * 0.7, 1.9 + i * 0.7, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 340 + i * 152
        text_at(d, 200, y, num, cn(60), YELLOW, pa)
        rounded(d, (330, y - 16, 1720, y + 104), 14, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pa)
        text_at(d, 366, y + 2, head, cn(38), WHITE, pa)
        text_at(d, 366, y + 58, sub, cn_light(27), GREY, pa)

    pb = fade(t, 3.6, 4.5, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, H - 176, "不用会编程 —— 会打字就能用", cn(36), YELLOW, pb)


def scene_boundary(d: ImageDraw.ImageDraw, t: float) -> None:
    """边界说明 —— 诚实交代适用范围(对宣传反而加分)。"""
    a = fade(t, 0.05, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 150, "它也做不到什么", cn(58), WHITE, a)

    rows = [
        ("要遵守 robots.txt", "站点说不让抓就停下, 不做绕过", YELLOW_DIM),
        ("有访问频率限制", "故意慢, 默认每次间隔 1~3 秒", YELLOW_DIM),
        ("登录 / 付费内容", "不破解 —— 需要你自己有权限", YELLOW_DIM),
        ("站点明确限流时", "会告诉你是限流, 而不是让你反复调规则", YELLOW),
    ]
    for i, (head, sub, col) in enumerate(rows):
        pa = fade(t, 1.1 + i * 0.55, 1.9 + i * 0.55, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 330 + i * 130
        text_at(d, 200, y, "—", cn(40), col, pa)
        text_at(d, 280, y - 4, head, cn(40), WHITE, pa)
        text_at(d, 280, y + 54, sub, cn_light(27), GREY, pa)

    pb = fade(t, 3.6, 4.5, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, H - 180, "用在合法授权的场景 —— 这条是底线", cn(36), YELLOW, pb)
