"""
只打印演示片段的速度计划与事件, 不出片。

调参时用这个 —— 改完阈值立刻能看效果, 不用等 60 秒渲染。

    python scripts/intro_video/plan_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import demo_kit as dk  # noqa: E402
import demo_plan as dp  # noqa: E402


def main() -> int:
    dur, diff, times, centers = dp.analyze()
    plan = dp.build_speed_plan(dur, times, diff)
    new_dur = dp.plan_duration(plan)

    print("\n=== 速度计划 ===")
    for a, b, s in plan:
        tag = "保持原速" if s == 1.0 else f"{s:g}x"
        print(f"    {a:>7.2f}s .. {b:>7.2f}s   {b - a:>6.2f}s  ->  "
              f"{(b - a) / s:>6.2f}s   {tag}")
    kept = sum(b - a for a, b, s in plan if s == 1.0)
    print(f"    原始 {dur:.1f}s -> 压缩后 {new_dur:.1f}s  "
          f"(保持原速 {kept:.1f}s = {kept / dur * 100:.0f}%, "
          f"省下 {dur - new_dur:.1f}s)")

    events = dp.find_events(diff, times, centers)
    mapped = [(dp.map_time(t, plan), cx, cy, k) for t, cx, cy, k in events]
    clicks = sum(1 for *_x, k in mapped if k == "click")
    swipes = sum(1 for *_x, k in mapped if k == "swipe")
    print(f"\n=== 事件 {len(mapped)} 个(点击 {clicks} / 滑动 {swipes})===")
    for t, cx, cy, kind in mapped:
        print(f"    {t:>6.2f}s  {kind:<6} 位置 ({cx * 100:>4.0f}%, {cy * 100:>4.0f}%)")

    print(f"\n  鼠标符贴图尺寸 {dk.CURSOR_W}x{dk.CURSOR_H}; "
          f"音效时长 点击 {len(dk.sfx_click()) / dk.SR:.2f}s / "
          f"滑动 {len(dk.sfx_swipe()) / dk.SR:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
