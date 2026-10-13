"""
演示录屏的分析层: 活动度、速度计划、操作事件。

这些是**纯计算**, 不产出文件, 所以放在 kit 里给脚本共用:
  - ``make_demo_frames.py`` 用它们渲染;
  - ``plan_demo.py`` 用它们只打印计划(调参时看效果, 不用等出片)。

判据说明都在各函数 docstring 里 —— 这里的阈值都是实测定出来的, 改之前先读一遍。
"""

from __future__ import annotations

import numpy as np

from demo_kit import (
    change_centroid,
    ensure_prepped,
    probe_dur,
    read_gray_big,
)

#: 活动度分析用的降采样尺寸与帧率。320x172 足够刻画"哪里在变", 又比全分辨率快两个数量级。
GW, GH = 320, 172
AFPS = 10


def analyze() -> tuple[float, np.ndarray, np.ndarray, list[tuple[float, float, float]]]:
    """返回 (时长, 逐帧差, 时间轴, 每帧的变化中心)。

    变化中心用于**把鼠标符放到真正被点的位置**: 整帧平均只能说明"有变化", 定位不到哪里变了,
    而观众需要的正是"这里被点了"。
    """
    src = ensure_prepped()
    dur = probe_dur(src)
    big = read_gray_big(src, GW, GH, AFPS)
    diff = np.abs(np.diff(big.reshape(len(big), -1), axis=0)).mean(axis=1)
    centers: list[tuple[float, float, float]] = []
    for i in range(len(big) - 1):
        # 只在有明显变化时算中心 —— change_centroid 要遍历整帧, 静止帧没必要算
        if diff[i] >= 0.5:
            centers.append(change_centroid(big[i], big[i + 1], GW, GH))
        else:
            centers.append((0.5, 0.5, 0.0))
    print(f"  演示视频 {dur:.1f}s, 分析 {len(big)} 帧, "
          f"帧差 中位 {np.median(diff):.2f} / 最大 {diff.max():.2f}")
    return dur, diff, np.arange(len(diff)) / AFPS, centers


def find_events(diff: np.ndarray, times: np.ndarray,
                centers: list[tuple[float, float, float]]
                ) -> list[tuple[float, float, float, str]]:
    """挑出操作时刻, 返回 [(时间, x 比例, y 比例, 音效种类), ...]。

    分类看**变化块的时间宽度**:
      - 宽度 <= 0.20s -> 点击(按钮/切换, 一两帧内完成);
      - 宽度 >= 0.35s 且峰值显著 -> 滑动/滚动;
      - 中间地带不发声。

    **不为滚动配连续音效**: 第一版按宽度分类但阈值给太松, 结果 64 秒里塞进 30 多个滑动音,
    听起来像噪声。滚动在画面上已经看得很清楚, 配一串滑音只会稀释点击提示。所以滑动只在
    变化**明显强于**周围时才配一次。
    """
    med = float(np.median(diff))
    hi = max(med * 3.5, 1.4)
    lo = max(med * 1.5, 0.55)
    events: list[tuple[float, float, float, str]] = []
    i = 0
    n = len(diff)
    while i < n:
        if diff[i] >= hi:
            j = i
            while j + 1 < n and diff[j + 1] >= lo:
                j += 1
            width = (j - i + 1) / AFPS
            peak = float(diff[i:j + 1].max())
            k = min(i, len(centers) - 1)
            cx, cy, _ = centers[k]
            if width <= 0.20:
                events.append((float(times[i]), cx, cy, "click"))
            elif width >= 0.35 and peak >= hi * 1.6:
                events.append((float(times[i]), cx, cy, "swipe"))
            i = j + AFPS // 5
        else:
            i += 1

    # 同类音效至少隔 0.30s, 不同类型隔 0.18s, 免得叠在一起糊掉
    cleaned: list[tuple[float, float, float, str]] = []
    for t, cx, cy, kind in events:
        if cleaned:
            gap = t - cleaned[-1][0]
            if gap < (0.45 if kind == "swipe" else 0.22):
                continue
        cleaned.append((t, cx, cy, kind))
    return cleaned


# ---------------------------------------------------------------------------
# 速度计划
# ---------------------------------------------------------------------------
def build_speed_plan(dur: float, diff_t: np.ndarray, diff: np.ndarray,
                     fps: int = AFPS) -> list[tuple[float, float, float]]:
    """返回 [(起, 止, 倍速), ...], 覆盖整段; 有操作的区间保持 1x。

    实测原录屏 **62% 的时长画面几乎不动**(等抓取跑完 18.5s、等分析结果 9.8s),
    这些段落按倍数压缩。倍速按静止时长递增 —— 停得越久压得越狠。
    """
    med = float(np.median(diff))
    active = diff >= max(med * 2.0, 0.8)

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
    """原始时间 -> 压缩后时间。音效与鼠标符都按这个映射对齐到压缩后的时间轴。"""
    out = 0.0
    for a, b, s in plan:
        if t <= a:
            return out
        if t <= b:
            return out + (t - a) / s
        out += (b - a) / s
    return out
