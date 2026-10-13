"""
渲染前自检: 时长 / 字体 / 画布尺寸。

存在的理由: 这些都是**静默失败** —— 中文用错字体会渲染成豆腐块、时长不够只有跑到最后才知道、
画布尺寸不一致会错位但不报错。把它们在渲染前一次性报出来, 比渲染 4 分钟后才发现划算得多。
"""

from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

# Windows 控制台默认 GBK, 打不出 ✓/✗ 与中文会直接抛 UnicodeEncodeError。
# 渲染脚本自己不打印这些符号, 但自检脚本要, 所以在这里强制 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import render_kit as rk  # noqa: E402
import scenes  # noqa: E402
import scenes_easy
import scenes_extra  # noqa: E402
import make_intro_video as M  # noqa: E402

CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def main() -> int:
    print("=== 0) 时间轴归一化单测 ===")
    # 这段逻辑曾两次静默出错(画面空掉但不报错), 所以放在最前面当闸门。
    import test_timeline

    check(test_timeline.main() == 0, "时间轴单测通过")

    print("\n=== 1) 画布尺寸一致性 ===")
    sz = (rk.W, rk.H)
    for mod in (scenes, scenes_extra, scenes_easy):
        same = (getattr(mod, "W", None), getattr(mod, "H", None)) == sz
        check(same, f"{mod.__name__} 绑定的尺寸与 render_kit 一致",
              f"{getattr(mod, 'W', '?')}x{getattr(mod, 'H', '?')} vs {sz[0]}x{sz[1]}")

    print("\n=== 2) 时长 ===")
    sc = M.build_all()
    total = sum(s.dur for s in sc)
    check(210 <= total <= 300, "总时长在 3.5~5 分钟之间",
          f"{total:.1f}s = {total / 60:.2f} 分钟")
    for s in sc:
        if s.dur < 6:
            fails.append(f"场景 {s.key} 过短({s.dur}s)")
    check(all(s.dur >= 6 for s in sc), "每个场景不少于 6 秒")
    check(all(s.caption for s in sc), "每个场景都有字幕",
          f"缺: {[s.key for s in sc if not s.caption]}")

    print("\n=== 3) 字体是否画得出中英混排(豆腐块检查) ===")
    # **扫源码里的真实字面量**, 不用手挑的样本 —— 手挑样本会漏: 我第一版就是手挑的,
    # 一路绿灯, 而画面上 `latin(30)` 画含中文的副标题时全是豆腐块。
    import check_fonts

    check(check_fonts.main() == 0, "源码中所有写死文案的字体都正确")

    cases = [
        (rk.mono(24), "mono", ["page_title", "GET /api/search JSON 200", "$ ls -la"]),
        (rk.mono_cjk(24), "mono_cjk", ["分页器 li.next a", "候选列表区 section > ol (30 项)"]),
        (rk.cn(30), "cn", ["智能爬虫框架 + 图形化控制台", "只爬页面的一部分", "验收"]),
        (rk.latin(30), "latin", ["Playwright · LLM · FastAPI", "media plugins"]),
    ]
    bad = []
    for f, label, samples in cases:
        for s in samples:
            if not rk.has_glyphs(f, s):
                bad.append(f"{label} 画不出 {s!r}")
    check(not bad, "各字体对自己实际要画的文案都有字形",
          "; ".join(bad) if bad else f"{sum(len(c[2]) for c in cases)} 个样本")

    print("\n=== 4) 场景顺序与标签 ===")
    keys = [s.key for s in sc]
    check(len(keys) == len(set(keys)), "场景 key 无重复")
    check(keys[0] == "title", "开场是封面", keys[0])
    check(keys[-1] == "end", "结尾是收尾", keys[-1])
    check(all(s.tag for s in sc), "每个场景都有右上角标签")

    print("\n=== 5) 冒烟: 每个场景都把整段画一遍 ===")
    # 存在的理由: 曾经在一个场景里漏 import 了 ease_out, 静态看毫无问题, 直到渲染到第 14 个
    # 场景(约 3 分钟后)才 NameError 崩掉, 前面几分钟白跑。这里在渲染前把每个场景的每一帧
    # 都真画一遍 —— 静态检查抓不到的东西, 它会当场抓到。
    from PIL import Image, ImageDraw

    smoke_fail: list[str] = []
    frames_drawn = 0
    for s in sc:
        # 注入本场景时长 —— 场景里 fade(hold_to=DUR - 余量) 依赖它; 不注入就全是空屏
        M.inject_time(s)
        n = max(2, int(s.dur * rk.FPS))
        for k in range(n):
            t = k / rk.FPS
            try:
                layer = Image.new("RGBA", (rk.W, rk.H), (0, 0, 0, 0))
                d = ImageDraw.Draw(layer)
                s.draw(d, t)
                if s.caption:
                    rk.draw_caption(d, s.caption, rk.fade(t, s.caption_from,
                                                         s.caption_from + 0.7))
                if s.tag:
                    rk.draw_tag(d, s.tag, 96)
            except Exception as exc:  # noqa: BLE001
                smoke_fail.append(f"{s.key} @t={t:.2f}s: "
                                  f"{type(exc).__name__}: {exc}")
                break
            frames_drawn += 1
    check(not smoke_fail, f"**所有场景都能画完({frames_drawn} 帧无异常)**",
          "; ".join(smoke_fail[:4]))

    print("\n=== 6) 尾帧不能是空屏 ===")
    # 补时(HOLD)会改变场景时长, 而淡出时机如果写死, 内容就会提前淡掉、尾巴留一段空屏。
    # 实测抓到过: t=52s 那一帧画面全空, 只剩字幕。这里逐场景检查"最后一帧还有多少内容"。
    empty_tails: list[str] = []
    for s in sc:
        M.inject_time(s)
        t = s.dur - 0.05
        layer = Image.new("RGBA", (rk.W, rk.H), (0, 0, 0, 0))
        s.draw(ImageDraw.Draw(layer), t)
        # 统计不透明像素占比: 空屏时几乎为 0
        alpha = layer.getchannel("A")
        opaque = sum(1 for v in alpha.getdata() if v > 24)
        ratio = opaque / (rk.W * rk.H)
        if ratio < 0.0006:
            empty_tails.append(f"{s.key} 尾帧内容占比 {ratio * 100:.3f}%")
    check(not empty_tails, "**每个场景的尾帧都还有内容(没有早早淡成空屏)**",
          "; ".join(empty_tails[:5]))

    print("\n" + "=" * 62)
    if fails:
        print(f"渲染前自检: 未通过 ✗ ({len(fails)} 项)")
        for x in fails:
            print(f"  - {x}")
    else:
        print(f"渲染前自检: 通过 ✓   {len(sc)} 个场景 / {total:.1f}s / "
              f"{int(total * 30)} 帧 @30fps")
    print("=" * 62)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
