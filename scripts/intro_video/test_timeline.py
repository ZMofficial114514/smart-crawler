"""
fade 时间轴归一化的单测。

为什么单独测它: 这块逻辑一错就会静默出问题 —— 之前两次都是"画面看着空", 而不是报错。
判据很明确: 停留期必须覆盖场景的大部分时间, 淡入淡出各只占固定的一小段。
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
import render_kit as rk  # noqa: E402

fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def opacity_curve(design: float, actual: float, a: float, hold: float,
                  steps: int = 400) -> list[tuple[float, int]]:
    rk.set_design(design, actual)
    return [(i / steps * actual,
             rk.fade(i / steps * actual, a, a + 1.0, hold_to=hold, tmax=actual))
            for i in range(steps + 1)]


def main() -> int:
    print("=== 1) 拉伸场景: 停留期必须铺满中段 ===")
    for design, actual in ((10.0, 14.0), (8.0, 10.4), (12.0, 12.0)):
        a, hold = 0.5, design * 0.91
        curve = opacity_curve(design, actual, a, hold)
        opaque = [t for t, o in curve if o >= 250]
        span = (opaque[-1] - opaque[0]) if opaque else 0.0
        ratio = span / actual
        check(ratio > 0.6, f"设计 {design}s -> 实际 {actual}s: 不透明段占比 {ratio * 100:.0f}%",
              f"{opaque[0]:.2f}s .. {opaque[-1]:.2f}s")
        # 尾帧不能已淡空
        check(curve[-1][1] < 250, "尾帧确实在淡出(不会硬切)")

    print("\n=== 2) 淡入必须利落(不随拉伸变慢) ===")
    for design, actual in ((10.0, 10.0), (10.0, 14.0), (8.0, 14.0)):
        curve = opacity_curve(design, actual, 0.5, design * 0.91)
        # 找到首次达到不透明的时刻
        first = next((t for t, o in curve if o >= 250), None)
        check(first is not None and first < 1.6,
              f"设计 {design}s -> 实际 {actual}s: 淡入在 {first:.2f}s 内完成",
              f"恒定 {rk.FADE_IN}s + 起始偏移")

    print("\n=== 3) 压缩场景也一样成立 ===")
    curve = opacity_curve(14.0, 10.0, 0.5, 14.0 * 0.91)
    opaque = [t for t, o in curve if o >= 250]
    check(bool(opaque), "压缩后仍有不透明段",
          f"{opaque[0]:.2f}s .. {opaque[-1]:.2f}s" if opaque else "无")

    print("\n" + "=" * 60)
    if fails:
        print(f"时间轴单测: 未通过 ✗ ({len(fails)} 项)")
        for f in fails:
            print(f"  - {f}")
    else:
        print("时间轴单测: 通过 ✓")
    print("=" * 60)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
