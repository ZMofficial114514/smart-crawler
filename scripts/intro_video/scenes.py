"""
SmartCrawler 介绍动画 —— 场景脚本。

内容**全部取自仓库真实文档**(README.md / CHANGELOG.md / 项目结构), 不编造功能与数字。
时长合计约 240 秒(4 分钟)。

风格对齐参考视频: 深灰细纹理 + 白色超粗中文 + 斜体英文副题 + 黄色强调/箭头/字幕。
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from render_kit import (  # noqa: E402
    GREY, GREY_DIM, H, LINE, W, WHITE, YELLOW, YELLOW_DIM,
    Scene, arrow, cn, cn_light, draw_caption, draw_tag, ease_in_out, ease_out,
    ease_out_back, fade, latin, measure, mono, mono_cjk, noise_layer, rounded,
    seg, text_at, text_center,
)

#: 时间轴拉伸比例 = 实际时长 / 设计时长。**由渲染器逐场景注入**。
#: 1.0 表示不拉伸(设计时长即实际时长)。
K = 1.0

#: 当前场景的总时长(秒)。**由渲染器在每个场景绘制前注入** —— 补时(HOLD)会
#: 改变时长, 而淡出时机必须跟着走, 否则内容早早淡掉、剩一段空屏。
DUR = 0.0

ACCENT_TAGS = {
    "title": "SMARTCRAWLER · 介绍",
    "pain": "问题",
    "pillars": "架构",
    "flow": "数据流",
    "console": "控制台",
    "ai": "AI 规则",
    "scope": "抓取区域",
    "media": "媒体下载",
    "plugins": "插件",
    "robust": "工程化",
    "compliance": "合规",
    "start": "快速开始",
    "end": "SMARTCRAWLER",
}


def _noise(seed: int, n: int = 14):
    return noise_layer(seed, n, latin(48), WHITE, 34)


# ---------------------------------------------------------------------------
# 01 封面
# ---------------------------------------------------------------------------
def scene_title(d: ImageDraw.ImageDraw, t: float) -> None:
    p = ease_out(seg(t, 0.3, 1.5))
    a = int(255 * p)
    # 标题从下方微微上移并淡入
    dy = int(28 * (1 - p))
    text_center(d, 470 + dy, "SmartCrawler", cn(132), WHITE, a)
    text_center(d, 596 + dy, "智能爬虫框架 + 图形化控制台", cn(46), YELLOW, a)

    p2 = ease_out(seg(t, 1.3, 2.4))
    text_center(d, 690, "Playwright  ·  LLM  ·  FastAPI  ·  零构建前端",
                mono_cjk(30), GREY, int(255 * p2))

    # 下划黄线展开
    p3 = ease_out(seg(t, 1.8, 2.6))
    if p3 > 0:
        half = int(260 * p3)
        d.line([(W // 2 - half, 760), (W // 2 + half, 760)], fill=YELLOW, width=6)

    p4 = ease_out(seg(t, 2.6, 3.6))
    text_center(d, 828, "仅限用于合法授权的数据采集场景", cn_light(28), GREY_DIM,
                int(255 * p4))


# ---------------------------------------------------------------------------
# 02 传统爬虫的痛
# ---------------------------------------------------------------------------
def scene_pain(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 200, "传统爬虫的问题", cn(64), WHITE, a)

    rows = [
        ("硬编码选择器", "页面一改版, 整条链路失效"),
        ("结构靠人眼看", "哪个 div 是列表项, 得自己猜"),
        ("接口数据捞不到", "内容在 XHR 里, 不在 DOM 里"),
        ("地址藏在 iframe", "只分析主文档, 结果是 0 条"),
    ]
    for i, (head, sub) in enumerate(rows):
        pa = fade(t, 1.0 + i * 0.55, 1.8 + i * 0.55, hold_to=DUR - 0.9, tmax=DUR)
        y = 320 + i * 118
        # 序号
        text_at(d, 200, y, f"{i + 1:02d}", mono(40), YELLOW_DIM, pa)
        text_at(d, 300, y - 6, head, cn(46), WHITE, pa)
        text_at(d, 300, y + 54, sub, cn_light(30), GREY, pa)

    pb = fade(t, 4.2, 5.2, hold_to=DUR - 0.9, tmax=DUR)
    if pb > 0:
        rounded(d, (200, 812, 1720, 900), 16, fill=(46, 46, 46), alpha=pb)
        text_at(d, 236, 838, "共同点: 全都依赖“页面长什么样”", cn(34), YELLOW, pb)


# ---------------------------------------------------------------------------
# 03 四个支柱
# ---------------------------------------------------------------------------
def scene_pillars(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 176, "框架的四个支柱", cn(62), WHITE, a)
    text_at(d, 200, 258, "browser · network · structure · ai", latin(30), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    cards = [
        ("browser.py", "Playwright", "多内核 / 无头可配 / 会话持久化"),
        ("network.py", "XHR · WS", "后台捕获接口与 WebSocket"),
        ("structure.py", "DOM 分析", "重复结构识别 + 唯一选择器"),
        ("ai.py", "LLM", "自然语言直接生成提取规则"),
    ]
    for i, (fname, tag, desc) in enumerate(cards):
        pa = fade(t, 1.2 + i * 0.6, 2.0 + i * 0.6, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        # 依次入场: 从下方 24px 上移
        pp = ease_out_back(seg(t, 1.2 + i * 0.6, 2.0 + i * 0.6))
        dy = int(24 * (1 - pp))
        x = 200 + i * 390
        rounded(d, (x, 360 + dy, x + 350, 640 + dy), 18,
                fill=(48, 48, 48), outline=LINE, width=2, alpha=pa)
        d.line([(x + 28, 404 + dy), (x + 108, 404 + dy)], fill=YELLOW, width=5)
        text_at(d, x + 28, 428 + dy, fname, mono(34), WHITE, pa)
        text_at(d, x + 28, 486 + dy, tag, cn(30), YELLOW, pa)
        text_at(d, x + 28, 540 + dy, desc[:13], cn_light(24), GREY, pa)
        if len(desc) > 13:
            text_at(d, x + 28, 576 + dy, desc[13:], cn_light(24), GREY, pa)

    pc = fade(t, 4.4, 5.4, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, 780, "四者由 crawler.py 编排成一条流水线", cn(36), WHITE, pc)


# ---------------------------------------------------------------------------
# 04 数据流
# ---------------------------------------------------------------------------
def scene_flow(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "一条任务的数据流", cn(62), WHITE, a)

    steps = ["合规检查", "限速", "导航", "网络监听", "结构分析",
             "生成规则", "提取清洗", "去重增量", "存储"]
    y = 420
    n = len(steps)
    # 节点逐个点亮
    for i, s in enumerate(steps):
        pa = fade(t, 1.0 + i * 0.5, 1.7 + i * 0.5, hold_to=DUR - 0.9, tmax=DUR)
        x = 160 + i * 182
        rounded(d, (x, y, x + 158, y + 96), 14, fill=(50, 50, 50),
                outline=LINE, width=2, alpha=pa)
        text_center(d, y + 48, s, cn(28), WHITE, pa, xy=(x + 79, y + 48))
        if i:
            xa = 160 + (i - 1) * 182 + 158
            pa2 = fade(t, 1.35 + i * 0.5, 1.95 + i * 0.5, hold_to=DUR - 0.9, tmax=DUR)
            arrow(d, xa + 4, y + 48, xa + 20, y + 48, YELLOW, 4, pa2, 12)

    pb = fade(t, 5.6, 6.6, hold_to=DUR - 0.9, tmax=DUR)
    if pb > 0:
        text_at(d, 200, 600, "规则来源三选一", cn(40), YELLOW, pb)
        for i, s in enumerate(["① 调用方显式传入", "② AI 按自然语言目标生成",
                               "③ 规则引擎降级兜底"]):
            pa = fade(t, 6.2 + i * 0.35, 6.9 + i * 0.35, hold_to=DUR - 0.9, tmax=DUR)
            text_at(d, 236, 668 + i * 54, s, cn_light(32), WHITE, pa)

    pc = fade(t, 8.2, 9.2, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 862, "任一环节可观测 —— 每一步都推实时事件到控制台",
            cn_light(28), GREY, pc)


# ---------------------------------------------------------------------------
# 05 图形化控制台
# ---------------------------------------------------------------------------
def scene_console(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "图形化控制台", cn(62), WHITE, a)
    text_at(d, 200, 250, "FastAPI + WebSocket + 零构建前端", mono_cjk(30), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    pages = [
        ("抓取任务", "自然语言或手写规则; 8 步进度时间线"),
        ("结构分析", "候选列表区 / 分页器 / 元数据; 一键回填规则"),
        ("网络抓包", "捕获 XHR 与 WebSocket; 找到真正的数据接口"),
        ("结果与历史", "表格预览、导出、按 URL 归类与清理"),
        ("插件", "图片 / 音乐 / 视频下载, 故障隔离"),
        ("系统配置", "配置内省、代理自检、AI 连通性测试"),
    ]
    for i, (name, desc) in enumerate(pages):
        col, row = i % 2, i // 2
        pa = fade(t, 1.1 + i * 0.45, 1.8 + i * 0.45, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        x = 200 + col * 800
        y = 340 + row * 168
        rounded(d, (x, y, x + 720, y + 132), 16, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pa)
        d.line([(x + 26, y + 34), (x + 86, y + 34)], fill=YELLOW, width=5)
        text_at(d, x + 26, y + 52, name, cn(38), WHITE, pa)
        text_at(d, x + 26, y + 100, desc, cn_light(25), GREY, pa)

    pb = fade(t, 4.4, 5.4, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, 880, "浏览器打开 http://127.0.0.1:8322 即可", cn(34), YELLOW, pb)


# ---------------------------------------------------------------------------
# 06 AI 生成规则
# ---------------------------------------------------------------------------
def scene_ai(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "用自然语言生成规则", cn(62), WHITE, a)

    # 左: 用户一句话
    pa = fade(t, 1.0, 1.8, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 320, 760, 470), 16, fill=(48, 48, 48), outline=LINE,
            width=2, alpha=pa)
    text_at(d, 236, 350, "goal", mono(26), YELLOW_DIM, pa)
    text_at(d, 236, 392, "“抓取书名和价格”", cn(38), WHITE, pa)

    # 中: 箭头
    arrow(d, 800, 395, 940, 395, YELLOW, 6, fade(t, 2.0, 2.8, hold_to=DUR - 0.9, tmax=DUR), 24)

    # 右: 结构报告
    pb = fade(t, 2.6, 3.4, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (980, 300, 1720, 560), 16, fill=(48, 48, 48), outline=LINE,
            width=2, alpha=pb)
    text_at(d, 1016, 328, "结构报告", cn(30), YELLOW, pb)
    for i, s in enumerate(["候选列表区  section > ol > li (30 项)",
                           "样本字段    title / price / link",
                           "分页器      li.next a"]):
        # 用 mono_cjk: 这几行混了中文("候选列表区"/"30 项"), Consolas 画不出中文会成豆腐块
        text_at(d, 1016, 388 + i * 52, s, mono_cjk(24), WHITE,
                fade(t, 3.0 + i * 0.3, 3.7 + i * 0.3, hold_to=DUR - 0.9, tmax=DUR))

    # 下: 规则 JSON
    pc = fade(t, 4.4, 5.4, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 620, 1720, 900), 16, fill=(44, 44, 44), outline=LINE,
            width=2, alpha=pc)
    lines = [
        '{',
        '  "mode": "dom",',
        '  "list_rule": {',
        '    "item_selector": "article.product_pod",',
        '    "fields": [',
        '      {"name": "title", "selector": "h3 a", "transform": ["strip"]},',
        '      {"name": "price", "selector": ".price_color", "transform": ["price"]}',
        '    ]',
        '  }',
        '}',
    ]
    for i, s in enumerate(lines):
        col = YELLOW if ('"title"' in s or '"price"' in s
                         or "item_selector" in s) else WHITE
        text_at(d, 244, 646 + i * 25, s, mono(22), col,
                fade(t, 4.8 + i * 0.12, 5.4 + i * 0.12, hold_to=DUR - 0.9, tmax=DUR))


# ---------------------------------------------------------------------------
# 07 只爬页面的一部分
# ---------------------------------------------------------------------------
def scene_scope(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "只爬页面的一部分", cn(62), WHITE, a)
    text_at(d, 200, 250, "抓取区域 · scope", mono_cjk(30), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    # 页面示意
    pb = fade(t, 1.0, 1.8, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (200, 330, 1000, 860), 14, fill=(48, 48, 48), outline=LINE,
            width=2, alpha=pb)
    # 页头导航
    for i in range(5):
        d.rectangle([224 + i * 108, 356, 300 + i * 108, 386],
                    fill=(72, 72, 72) if pb >= 255 else (72, 72, 72, pb))
    # 侧栏
    d.rectangle([224, 420, 380, 820],
                fill=(64, 64, 64) if pb >= 255 else (64, 64, 64, pb))
    # 目标列表(高亮)
    hl = fade(t, 1.8, 2.8, hold_to=DUR - 0.9, tmax=DUR)
    d.rectangle([412, 420, 976, 820],
                fill=(70, 66, 40) if hl >= 255 else (70, 66, 40, hl),
                outline=YELLOW if hl >= 255 else YELLOW + (hl,), width=4)
    for i in range(6):
        d.rectangle([436, 444 + i * 62, 952, 486 + i * 62],
                    fill=(92, 86, 52) if hl >= 255 else (92, 86, 52, hl))
    text_center(d, 840, "目标列表", cn(26), YELLOW, hl, xy=(694, 840))

    # 右侧说明
    pc = fade(t, 3.0, 3.9, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 1090, 360, "整页分析", cn(34), WHITE, pc)
    text_at(d, 1090, 412, "7 个候选(含导航/页脚)", cn_light(27), GREY, pc)
    text_at(d, 1090, 500, "限定区域后", cn(34), YELLOW, pc)
    text_at(d, 1090, 552, "1 个候选 · 结构树 761 → 489 行", cn_light(27), GREY, pc)

    pd = fade(t, 5.0, 6.0, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (1090, 640, 1720, 800), 14, fill=(46, 46, 46), alpha=pd)
    text_at(d, 1120, 664, "候选列表卡片上点「只抓取这一块」", cn(28), WHITE, pd)
    text_at(d, 1120, 712, "不用自己写选择器", cn_light(26), YELLOW, pd)


# ---------------------------------------------------------------------------
# 08 媒体下载
# ---------------------------------------------------------------------------
def scene_media(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "图片 · 音乐 · 视频", cn(62), WHITE, a)
    text_at(d, 200, 250, "media plugins", latin(30), GREY,
            fade(t, 0.8, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    cards = [
        ("图片下载", "懒加载属性优先(data-src), 避免下到占位图"),
        ("音乐下载", "直链直接下; m3u8/mpd 交给 ffmpeg 转封装"),
        ("视频下载", "落盘后按文件魔数纠正扩展名并校验完整性"),
        ("网易云", "会话换 weapi 音频直链; 产物按「歌手 - 歌名」命名"),
    ]
    for i, (name, desc) in enumerate(cards):
        pa = fade(t, 1.1 + i * 0.45, 1.8 + i * 0.45, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 340 + i * 130
        rounded(d, (200, y, 1180, y + 108), 14, fill=(48, 48, 48),
                outline=LINE, width=2, alpha=pa)
        d.line([(224, y + 30), (284, y + 30)], fill=YELLOW, width=5)
        text_at(d, 224, y + 46, name, cn(34), WHITE, pa)
        text_at(d, 430, y + 48, desc, cn_light(25), GREY, pa)

    pb = fade(t, 3.6, 4.6, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (1260, 340, 1720, 700), 14, fill=(46, 46, 46), alpha=pb)
    text_at(d, 1290, 366, "落盘后把关", cn(30), YELLOW, pb)
    for i, s in enumerate(["魔数辨认容器", "扩展名不符则纠正",
                           "MP4 无 moov 判截断", "文案当音频? 删掉并记失败"]):
        text_at(d, 1290, 424 + i * 62, "· " + s, cn_light(26), WHITE,
                fade(t, 4.0 + i * 0.25, 4.7 + i * 0.25, hold_to=DUR - 0.9, tmax=DUR))


# ---------------------------------------------------------------------------
# 09 插件
# ---------------------------------------------------------------------------
def scene_plugins(d: ImageDraw.ImageDraw, t: float) -> None:
    # 首元素从 0.05s 就开始淡入: 之前是 0.2s + 常数淡入时长, 实测在场景开头留下约 2 秒
    # "只有背景"的窗口(成片体检把它标成了内容低谷)。标题早点出来, 场景入口更紧凑。
    a = fade(t, 0.05, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "插件系统", cn(62), WHITE, a)
    text_at(d, 200, 250, "生命周期钩子 · 故障隔离", mono_cjk(30), GREY,
            fade(t, 0.5, 1.6, hold_to=DUR - 0.9, tmax=DUR))

    hooks = ["before_navigate", "after_extract", "after_paginate", "finish"]
    for i, h in enumerate(hooks):
        pa = fade(t, 0.9 + i * 0.4, 1.7 + i * 0.4, hold_to=DUR - 0.9, tmax=DUR)
        y = 360 + i * 92
        rounded(d, (200, y, 720, y + 68), 12, fill=(48, 48, 48), outline=LINE,
                width=2, alpha=pa)
        text_at(d, 232, y + 20, h, mono(26), YELLOW, pa)

    pb = fade(t, 3.0, 4.0, hold_to=DUR - 0.9, tmax=DUR)
    rounded(d, (820, 360, 1720, 760), 14, fill=(46, 46, 46), alpha=pb)
    text_at(d, 852, 388, "两条扩展路径", cn(32), WHITE, pb)
    for i, s in enumerate(["写 Python 插件: 继承 BasePlugin, 实现钩子",
                           "声明式扩展: 只写 YAML, 不碰代码"]):
        text_at(d, 852, 452 + i * 56, "· " + s, cn_light(27), WHITE,
                fade(t, 3.4 + i * 0.3, 4.1 + i * 0.3, hold_to=DUR - 0.9, tmax=DUR))
    text_at(d, 852, 600, "单个插件抛异常只记录并跳过,", cn_light(27), GREY,
            fade(t, 4.0, 4.7, hold_to=DUR - 0.9, tmax=DUR))
    text_at(d, 852, 642, "不会拖垮整条抓取链路。", cn_light(27), GREY,
            fade(t, 4.0, 4.7, hold_to=DUR - 0.9, tmax=DUR))


# ---------------------------------------------------------------------------
# 10 工程化
# ---------------------------------------------------------------------------
def scene_robust(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "踩过的坑, 都留了防线", cn(62), WHITE, a)

    items = [
        ("MP3 被误删", "魔数表漏了 ID3, 合法音频被当“无法识别”删掉"),
        ("假成功", "把几 KB 的播放列表当音频存下, 记录还显示成功"),
        ("选择器过深", "同一列两种 DOM 写法, 一深就只命中部分行"),
        ("推理模型预算", "思考内容吃光 max_tokens, 回复为空"),
        ("验收误报", "测试端口被占用, 5 项验收集体失败"),
    ]
    for i, (head, sub) in enumerate(items):
        pa = fade(t, 1.0 + i * 0.5, 1.7 + i * 0.5, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 300 + i * 122
        text_at(d, 200, y, "×", cn(34), YELLOW_DIM, pa)
        text_at(d, 262, y - 4, head, cn(40), WHITE, pa)
        text_at(d, 262, y + 52, sub, cn_light(27), GREY, pa)

    pb = fade(t, 4.6, 5.6, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, 900, "每一条都有对应的验收脚本守着, 不再回归",
                cn(34), YELLOW, pb)


# ---------------------------------------------------------------------------
# 11 合规
# ---------------------------------------------------------------------------
def scene_compliance(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "合规优先", cn(62), WHITE, a)

    items = [
        ("robots.txt 默认开启", "被拒绝时直接终止任务, 不做绕过"),
        ("随机限速 1–3 秒", "不给对方造成压力"),
        ("访问受限诊断", "分辨登录 / 权限 / 风控 / 验证码 / 空壳"),
        ("密钥不进仓库", "推送前扫描 + 全历史扫描"),
    ]
    for i, (head, sub) in enumerate(items):
        pa = fade(t, 1.1 + i * 0.5, 1.8 + i * 0.5, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 320 + i * 128
        d.line([(200, y + 18), (272, y + 18)], fill=YELLOW, width=5)
        text_at(d, 200, y + 36, head, cn(40), WHITE, pa)
        text_at(d, 200, y + 92, sub, cn_light(28), GREY, pa)

    pb = fade(t, 4.2, 5.2, hold_to=DUR - 0.9, tmax=DUR)
    text_center(d, 900, "仅限用于合法授权的数据采集场景", cn(36), YELLOW, pb)


# ---------------------------------------------------------------------------
# 12 快速开始
# ---------------------------------------------------------------------------
def scene_start(d: ImageDraw.ImageDraw, t: float) -> None:
    a = fade(t, 0.2, 1.0, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 200, 168, "四步跑起来", cn(62), WHITE, a)

    steps = [
        ("01", "start.bat", "一键启动: 建环境 → 装依赖 → 下内核 → 起服务"),
        ("02", "pip install -r requirements.txt", "手动安装(跨平台)"),
        ("03", "python scripts/bootstrap_browsers.py", "浏览器内核, 支持断点续传"),
        ("04", "python -m smartcrawler web", "打开 http://127.0.0.1:8322"),
    ]
    for i, (num, cmd, note) in enumerate(steps):
        pa = fade(t, 1.0 + i * 0.55, 1.8 + i * 0.55, hold_to=DUR - 0.9, tmax=DUR)
        if pa <= 0:
            continue
        y = 300 + i * 148
        text_at(d, 200, y, num, mono(40), YELLOW_DIM, pa)
        rounded(d, (300, y - 14, 1400, y + 46), 10, fill=(44, 44, 44),
                outline=LINE, width=2, alpha=pa)
        text_at(d, 328, y - 2, "$ " + cmd, mono(26), WHITE, pa)
        text_at(d, 300, y + 62, note, cn_light(26), GREY, pa)

    pb = fade(t, 4.6, 5.6, hold_to=DUR - 0.9, tmax=DUR)
    text_at(d, 1480, 320, "依赖", cn(30), YELLOW, pb)
    for i, s in enumerate(["Python ≥ 3.10", "Chromium(~150MB)",
                           "AI 可选(未配则降级)"]):
        text_at(d, 1480, 378 + i * 52, "· " + s, cn_light(26), GREY,
                fade(t, 5.0 + i * 0.25, 5.7 + i * 0.25, hold_to=DUR - 0.9, tmax=DUR))


# ---------------------------------------------------------------------------
# 13 结尾
# ---------------------------------------------------------------------------
def scene_end(d: ImageDraw.ImageDraw, t: float) -> None:
    p = ease_out(seg(t, 0.4, 1.6))
    a = int(255 * p)
    text_center(d, 430, "SmartCrawler", cn(120), WHITE, a)
    p2 = fade(t, 1.4, 2.4)
    text_center(d, 552, "自然语言 → 结构分析 → 规则 → 数据", cn(40), YELLOW, p2)
    p3 = fade(t, 2.4, 3.4)
    text_center(d, 664, "github.com/ZMofficial114514/smart-crawler",
                mono(30), GREY, p3)

    p4 = fade(t, 3.6, 4.6)
    if p4 > 0:
        half = int(220 * ease_out(seg(t, 3.6, 4.6)))
        d.line([(W // 2 - half, 748), (W // 2 + half, 748)],
               fill=YELLOW if p4 >= 255 else YELLOW + (p4,), width=5)
    p5 = fade(t, 4.4, 5.4)
    text_center(d, 826, "仅限用于合法授权的数据采集场景", cn_light(28),
                GREY_DIM, p5)


# ---------------------------------------------------------------------------
# 场景表
# ---------------------------------------------------------------------------
def build_scenes() -> list[Scene]:
    return [
        Scene("title", 10.0, scene_title,
              "基于 Playwright 与 LLM 的爬虫框架", 3.4, ACCENT_TAGS["title"]),
        Scene("pain", 10.0, scene_pain,
              "传统爬虫的共同点: 全都依赖页面长什么样", 4.4, ACCENT_TAGS["pain"]),
        Scene("pillars", 11.0, scene_pillars,
              "browser / network / structure / ai —— 由 crawler.py 编排", 4.6,
              ACCENT_TAGS["pillars"]),
        Scene("flow", 12.0, scene_flow,
              "规则来源三选一, 任一环节都可观测", 5.8, ACCENT_TAGS["flow"]),
        Scene("console", 11.5, scene_console,
              "零构建前端 · 实时日志总线 · 6 个页面", 4.6, ACCENT_TAGS["console"]),
        Scene("ai", 11.5, scene_ai,
              "自然语言直接变成可执行的提取规则", 5.6, ACCENT_TAGS["ai"]),
        Scene("scope", 11.0, scene_scope,
              "候选 7 → 1, 结构树 761 → 489 行", 5.2, ACCENT_TAGS["scope"]),
        Scene("media", 10.5, scene_media,
              "直链直接下; 流式交给 ffmpeg; 落盘后按魔数把关", 3.8,
              ACCENT_TAGS["media"]),
        Scene("plugins", 10.0, scene_plugins,
              "钩子 + 故障隔离: 一个插件坏了不影响抓取", 3.2,
              ACCENT_TAGS["plugins"]),
        Scene("robust", 10.5, scene_robust,
              "踩过的坑都留了防线, 并有验收脚本守着", 4.8,
              ACCENT_TAGS["robust"]),
        Scene("compliance", 9.5, scene_compliance,
              "robots.txt 默认开启; 有限速; 密钥不进仓库", 4.4,
              ACCENT_TAGS["compliance"]),
        Scene("start", 11.0, scene_start,
              "四步跑起来, 浏览器打开即可用", 4.8, ACCENT_TAGS["start"]),
        Scene("end", 8.0, scene_end,
              "MIT License · 欢迎 star 与 PR", 2.6, ACCENT_TAGS["end"]),
    ]


TOTAL = sum(s.dur for s in build_scenes())

__all__ = ["build_scenes", "TOTAL", "noise_layer", "Image", "ImageDraw",
           "draw_caption", "draw_tag", "text_at", "text_center", "cn",
           "cn_light", "latin", "mono", "arrow", "rounded", "fade", "seg",
           "ease_out", "ease_in_out", "ease_out_back", "measure",
           "WHITE", "YELLOW", "YELLOW_DIM", "GREY", "GREY_DIM", "LINE", "W", "H"]
