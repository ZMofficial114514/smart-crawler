"""
懒加载处理 —— 通过模拟滚动把"还没渲染出来的内容"加载出来。

**为什么需要它**: 现代站点(尤其 SPA)首屏只渲染骨架 —— 容器在 DOM 里, 内容靠
IntersectionObserver / 滚动事件再拉取。这时候抓到的是一条**看起来完整的空页面**:

- 导航/侧边栏(立即渲染)都在, 甚至能看到"退出登录", 于是"登录态"判定完全正确;
- 但主内容区(轮播、作品网格)是**空容器**, 一条数据都没有。

实测 pixiv 登录后首页: 直接抓到的简化 DOM 树里 ``/artworks/`` 链接 **0** 个、
空容器 **118** 个; 而真实页面有 **70** 个作品链接。这不是登录问题, 是**没滚**。

**两种终止条件要分清**, 因为它们的用户意图不同:

- ``settled``: 滚不动了(高度不再增长 / 无新节点) —— 内容已经加载完, 可以停;
- ``infinite``: 一直有新内容, 没有上限(无限流) —— 框架不该替用户决定抓多久,
  应当**如实报告并询问用户是否继续**。这正是"若本身无加载上限, 则提示用户"那一条。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from playwright.async_api import Page

# ---------------------------------------------------------------------------
# 采集脚本: 一次滚动动作 + 度量
# ---------------------------------------------------------------------------
SCROLL_JS = r"""
(stepRatio) => {
    // 找出"页面上真正在滚动的容器": body 不动而某个内部 div 在滚的情况下,
    // 只滚窗口是没用的(实测 pixiv 就属于这种)。
    const isScrollable = (el) => {
        if (!el || el === document.documentElement) return false;
        const s = getComputedStyle(el);
        const oy = s.overflowY;
        if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') return false;
        return el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 120;
    };

    const scrollers = [];
    for (const el of document.querySelectorAll('div, main, section, ul')) {
        if (isScrollable(el)) scrollers.push(el);
    }
    // 最深的那个通常才是真正的滚动容器
    scrollers.sort((a, b) => b.scrollHeight - a.scrollHeight);

    const before = {
        docHeight: Math.max(
            document.body ? document.body.scrollHeight : 0,
            document.documentElement ? document.documentElement.scrollHeight : 0
        ),
        nodes: document.querySelectorAll('*').length,
        // 用"离屏图片是否已加载"判断懒加载图片是否被触发
        images: document.querySelectorAll('img').length,
        loadedImages: [...document.querySelectorAll('img')].filter(i => i.complete && i.naturalWidth > 0).length,
    };

    const step = Math.max(300, Math.round(window.innerHeight * stepRatio));

    // 1) 滚窗口
    window.scrollBy(0, step);

    // 2) 也滚内部滚动容器(两者都做, 覆盖两类实现)
    let scrolledInner = 0;
    for (const el of scrollers.slice(0, 3)) {
        const prev = el.scrollTop;
        el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
        if (el.scrollTop !== prev) scrolledInner++;
    }

    return {
        before,
        scrolledInner,
        scrollY: Math.round(window.scrollY),
        atBottom: (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 4),
        innerScrollers: scrollers.slice(0, 3).map(el => ({
            cls: (el.className || '').toString().slice(0, 60),
            scrollTop: Math.round(el.scrollTop),
            scrollHeight: el.scrollHeight,
        })),
    };
}
"""

#: 度量脚本(不滚动, 只看当前状态)
MEASURE_JS = r"""
() => {
    const imgs = [...document.querySelectorAll('img')];

    // **关键**: 内容可能长在内部滚动容器里, 而不是文档上。
    // 实测 pixiv 与常见 SPA 都是"body 不动、内部 div 在滚" —— 此时
    // document.scrollHeight 恒等于视口高度, 用它判断"有没有长出新内容"会永远看不到变化,
    // 于是无限流会被误判成"已经滚到底"。
    let scroller = null;
    let scrollerHeight = 0;
    for (const el of document.querySelectorAll('div, main, section, ul, ol')) {
        const s = getComputedStyle(el);
        const oy = s.overflowY;
        if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') continue;
        if (el.clientHeight < 120) continue;
        if (el.scrollHeight > scrollerHeight) {
            scroller = el;
            scrollerHeight = el.scrollHeight;
        }
    }

    const docHeight = Math.max(
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0
    );

    return {
        docHeight,
        // 取"文档高度"与"内部滚动容器高度"的较大者作为有效内容高度
        contentHeight: Math.max(docHeight, scrollerHeight),
        scrollerCls: scroller ? (scroller.className || '').toString().slice(0, 60) : '',
        scrollerHeight,
        scrollerTop: scroller ? Math.round(scroller.scrollTop) : 0,
        nodes: document.querySelectorAll('*').length,
        links: document.querySelectorAll('a[href]').length,
        images: imgs.length,
        loadedImages: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
        scrollY: Math.round(window.scrollY),
        innerHeight: window.innerHeight,
    };
}
"""


@dataclass
class ScrollOutcome:
    """一次"滚动加载"的结果。"""

    rounds: int = 0
    #: settled / infinite / max_rounds / disabled / no_growth / user_stopped / error
    reason: str = "settled"
    grew: bool = False
    nodes_before: int = 0
    nodes_after: int = 0
    height_before: int = 0
    height_after: int = 0
    images_before: int = 0
    images_after: int = 0
    #: 高度是否一直在增长(没有上限的迹象)
    height_series: list[int] = field(default_factory=list)
    #: 用户在"是否继续滚动"里选了"继续"的次数。
    #: 用户可以选择一直滚下去 —— 这个计数就是"内容无上限时用户实际续了多少段"。
    continuations: int = 0

    @property
    def node_gain(self) -> int:
        return max(0, self.nodes_after - self.nodes_before)

    @property
    def height_gain(self) -> int:
        return max(0, self.height_after - self.height_before)

    @property
    def still_growing(self) -> bool:
        """最后一轮仍在增长 —— 说明还没到底。"""
        if len(self.height_series) < 3:
            return False
        return self.height_series[-1] - self.height_series[-2] > 40

    @property
    def infinite(self) -> bool:
        return self.reason in ("infinite", "user_stopped")

    def summary(self) -> str:
        if self.rounds == 0:
            return "未执行滚动加载"
        # 用"累计"字样: 这些是**从开始滚动算起**的总增量, 不是最后一轮的增量。
        # 不写清楚的话, 用户看到"新增节点 0"会以为滚动没生效(实际高度可能涨了几千 px,
        # 只是节点数没变 —— 有些站点靠撑高容器来加载内容)。
        parts = [f"滚动 {self.rounds} 轮", f"累计新增节点 {self.node_gain}"]
        if self.height_gain:
            parts.append(f"内容高度 +{self.height_gain}px")
        if self.images_after > self.images_before:
            parts.append(f"累计新增图片 {self.images_after - self.images_before}")
        if self.continuations:
            parts.append(f"用户续滚 {self.continuations} 次")
        if self.infinite:
            parts.append("内容仍在增长(疑似无上限)")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "reason": self.reason,
            "grew": self.grew,
            "node_gain": self.node_gain,
            "height_gain": self.height_gain,
            "images_before": self.images_before,
            "images_after": self.images_after,
            "infinite": self.infinite,
            "still_growing": self.still_growing,
            "continuations": self.continuations,
            "summary": self.summary(),
            "height_series": self.height_series[-8:],
        }


async def _measure(page: Page) -> dict[str, Any]:
    try:
        return await page.evaluate(MEASURE_JS)
    except Exception:  # noqa: BLE001
        return {"docHeight": 0, "nodes": 0, "links": 0, "images": 0,
                "loadedImages": 0, "scrollY": 0, "innerHeight": 0}


async def scroll_to_load(
    page: Page,
    *,
    max_rounds: int = 8,
    step_ratio: float = 1.4,
    settle_ms: float = 900.0,
    growth_threshold: int = 3,
    confirm: Optional[Any] = None,
    on_progress: Optional[Any] = None,
) -> ScrollOutcome:
    """模拟滚动触发懒加载, 直到"滚不动"或达到轮次上限。

    ``growth_threshold``: 连续这么多轮既没长高也没多节点, 就认为到底了。

    ``confirm``: 可选回调 ``async (outcome) -> (bool, int)``, 在**每一段轮次跑完且内容仍在
    增长**时调用, 用来询问用户"是否继续"。返回 ``(是否继续, 追加轮次)``。
    有了它就能做到"一直滚到用户说不滚为止", 而不再是一刀切地上限。

    ``on_progress``: 可选回调 ``(round_no, outcome) -> None``, 每轮结束后上报, 供界面
    实时显示"已滚 N 轮 / 新增多少"。
    """
    import asyncio

    outcome = ScrollOutcome()
    start = await _measure(page)
    outcome.nodes_before = int(start.get("nodes") or 0)
    outcome.height_before = int(start.get("contentHeight") or 0)
    outcome.images_before = int(start.get("images") or 0)

    prev_height = outcome.height_before
    prev_nodes = outcome.nodes_before
    flat_rounds = 0
    round_no = 0
    budget = max(1, max_rounds)

    while round_no < budget:
        round_no += 1
        try:
            await page.evaluate(SCROLL_JS, step_ratio)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"滚动执行失败, 停止懒加载滚动: {exc}")
            outcome.reason = "error"
            break
        await asyncio.sleep(settle_ms / 1000.0)

        current = await _measure(page)
        height = int(current.get("contentHeight") or 0)
        nodes = int(current.get("nodes") or 0)
        outcome.height_series.append(height)
        outcome.rounds = round_no

        grew_this_round = (height - prev_height) > 40 or (nodes - prev_nodes) > growth_threshold
        if grew_this_round:
            outcome.grew = True
            flat_rounds = 0
        else:
            flat_rounds += 1
            if flat_rounds >= 2:
                outcome.reason = "settled"
                break

        prev_height, prev_nodes = height, nodes

        if callable(on_progress):
            try:
                on_progress(round_no, outcome)
            except Exception:  # noqa: BLE001 - 上报失败不影响滚动
                pass

        # 跑满这一段了: 若内容仍在增长, 询问用户是否继续
        if round_no >= budget:
            outcome.reason = "infinite" if outcome.still_growing else "max_rounds"
            if not outcome.still_growing or not callable(confirm):
                break
            more = False
            extra = 0
            try:
                result = confirm(outcome)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, tuple):
                    more, extra = bool(result[0]), int(result[1] or 0)
                else:
                    more = bool(result)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"滚动确认回调失败, 按停止处理: {exc}")
                more = False
            if not more:
                outcome.reason = "user_stopped"
                break
            outcome.continuations += 1
            budget += max(1, extra or max_rounds)
            continue
    else:  # pragma: no cover - while 循环正常退出时不会走到
        outcome.reason = "max_rounds"

    final = await _measure(page)
    outcome.nodes_after = int(final.get("nodes") or 0)
    outcome.height_after = int(final.get("contentHeight") or 0)
    outcome.images_after = int(final.get("images") or 0)

    if outcome.rounds:
        logger.info(f"懒加载滚动: {outcome.summary()}")
    return outcome


def outcome_from_dict(data: Optional[dict[str, Any]]) -> Optional[ScrollOutcome]:
    """从字典还原(用于把滚动结果塞进任务结果/报告)。"""
    if not data:
        return None
    outcome = ScrollOutcome(
        rounds=int(data.get("rounds") or 0),
        reason=str(data.get("reason") or "settled"),
        grew=bool(data.get("grew")),
        nodes_before=int(data.get("nodes_before") or 0),
        nodes_after=int(data.get("nodes_after") or 0),
        height_before=int(data.get("height_before") or 0),
        height_after=int(data.get("height_after") or 0),
        images_before=int(data.get("images_before") or 0),
        images_after=int(data.get("images_after") or 0),
        continuations=int(data.get("continuations") or 0),
        height_series=list(data.get("height_series") or []),
    )
    return outcome


__all__ = ["ScrollOutcome", "scroll_to_load", "outcome_from_dict", "SCROLL_JS", "MEASURE_JS"]
