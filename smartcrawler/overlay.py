"""
遮罩/弹层处理 —— 自动关掉挡住内容的登录框等浮层。

**为什么需要它**: 很多站点(堆糖就是典型)打开后会弹一个**登录浮层**, 但内容其实
**已经在页面里了** —— 只是一个半透明遮罩盖在上面。实测 duitang 搜索页:

| | 弹层在 | 叉掉后 |
|---|---|---|
| 图片 | 55(已加载大图 26) | 55(26) |
| 链接 | 230 | 230 |
| 正文长度 | 796 | 663 |

内容一条没少, 但框架看到"1 个密码框 + 含密码框的表单"就判成 ``login_required``,
进而**直接跳过提取** —— 于是用户看到的是"检测到登录页就终止"。叉掉就能抓的页面,
不该因为一个遮罩而放弃。

**做法**: 在诊断之前, 尝试点掉明显的关闭按钮。三步, 按"越激进越往后"排序:

1. 按已知的关闭按钮特征找(`close` / `关闭` / `×`);
2. 按 Esc(很多弹层都支持);
3. 点遮罩本身(点击遮罩关闭是常见交互)。

**只点"看起来就是关闭"的东西**, 绝不点登录/提交按钮 —— 误点可能触发真实提交。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from playwright.async_api import Page

#: 点击关闭按钮: 只在"确实存在一个像登录/遮罩的浮层"时才动手
CLICK_CLOSE_JS = r"""
() => {
    const visible = (el) => {
        if (!el) return false;
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    };

    // 1) 先找一个"挡在内容前面的浮层": 尺寸够大、且像登录/弹窗
    const OVERLAY_RE = /(login|signin|sign-in|mask|modal|dialog|popup|overlay|passport|auth)/i;
    const overlays = [];
    for (const el of document.querySelectorAll('div, section, aside, form')) {
        if (!visible(el)) continue;
        const cls = (el.className || '').toString();
        if (!OVERLAY_RE.test(cls + ' ' + (el.id || ''))) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 200 || r.height < 120) continue;
        overlays.push(el);
    }
    if (!overlays.length) return { dismissed: false, reason: 'no-overlay' };

    // 2) 在这个浮层(或其祖先)里找关闭按钮
    const CLOSE_RE = /^(×|✕|✖|x|X|✗|关闭|close)$/i;
    const CLOSE_CLS_RE = /(close|dismiss|cancel-?btn|mask-close|btn-close|icon-close)/i;
    const candidates = [];
    const roots = new Set();
    for (const ov of overlays) {
        roots.add(ov);
        if (ov.parentElement) roots.add(ov.parentElement);
    }
    for (const root of roots) {
        for (const el of root.querySelectorAll('div, span, button, a, i, svg')) {
            if (!visible(el)) continue;
            const txt = (el.innerText || el.textContent || '').trim();
            const cls = (el.className || '').toString();
            const aria = (el.getAttribute('aria-label') || '') + (el.getAttribute('title') || '');
            // **安全**: 绝不允许点到"登录/注册/提交"这类会真的提交表单的元素
            if (/登录|注册|提交|确定|确认|submit|sign\s?in|log\s?in|register/i.test(txt)) continue;
            if (CLOSE_CLS_RE.test(cls) || CLOSE_RE.test(txt) || /关闭|close/i.test(aria)) {
                candidates.push({ el, cls: cls.slice(0, 60), txt: txt.slice(0, 10) });
            }
        }
    }
    if (!candidates.length) return { dismissed: false, reason: 'no-close-button', overlayCount: overlays.length };

    // 优先"最像关闭按钮"的: class 里带 close 的排前面
    candidates.sort((a, b) => (CLOSE_CLS_RE.test(b.cls) ? 1 : 0) - (CLOSE_CLS_RE.test(a.cls) ? 1 : 0));
    const picked = candidates[0];
    try {
        picked.el.click();
    } catch (e) {
        return { dismissed: false, reason: 'click-failed', error: String(e).slice(0, 80) };
    }
    return {
        dismissed: true,
        reason: 'clicked-close',
        cls: picked.cls,
        text: picked.txt,
        overlayCount: overlays.length,
        candidateCount: candidates.length,
    };
}
"""

#: 统计"还有多少遮挡物"
COUNT_OVERLAYS_JS = r"""
() => {
    const visible = (el) => {
        if (!el) return false;
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    };
    const OVERLAY_RE = /(login|signin|sign-in|mask|modal|dialog|popup|overlay|passport|auth)/i;
    let count = 0;
    for (const el of document.querySelectorAll('div, section, aside, form')) {
        if (!visible(el)) continue;
        const cls = (el.className || '').toString();
        if (!OVERLAY_RE.test(cls + ' ' + (el.id || ''))) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 200 || r.height < 120) continue;
        count++;
    }
    return { count };
}
"""


@dataclass
class DismissOutcome:
    """一次"关掉遮挡物"的结果。"""

    dismissed: bool = False
    attempts: int = 0
    overlays_before: int = 0
    overlays_after: int = 0
    reason: str = ""
    clicked: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.dismissed:
            return "未发现需要关闭的遮挡浮层"
        return (
            f"已关闭 {len(self.clicked)} 个遮挡浮层"
            f"(剩余 {self.overlays_after} 个): {', '.join(self.clicked[:3])}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dismissed": self.dismissed,
            "attempts": self.attempts,
            "overlays_before": self.overlays_before,
            "overlays_after": self.overlays_after,
            "clicked": self.clicked,
            "summary": self.summary,
        }


async def _count_overlays(page: Page) -> int:
    try:
        data = await page.evaluate(COUNT_OVERLAYS_JS)
        return int(data.get("count") or 0)
    except Exception:  # noqa: BLE001
        return 0


async def dismiss_overlays(page: Page, *, max_attempts: int = 3) -> DismissOutcome:
    """尝试关掉挡住内容的登录框/遮罩。**不会抛异常**。

    返回结果里的 ``dismissed`` 表示是否真的点掉了什么; 调用方据此决定是否重采页面。
    """
    import asyncio

    outcome = DismissOutcome()
    outcome.overlays_before = await _count_overlays(page)
    if outcome.overlays_before == 0:
        outcome.reason = "no-overlay"
        return outcome

    for _ in range(max_attempts):
        try:
            result = await page.evaluate(CLICK_CLOSE_JS)
        except Exception as exc:  # noqa: BLE001
            outcome.reason = f"error:{type(exc).__name__}"
            break

        outcome.attempts += 1
        if not result.get("dismissed"):
            outcome.reason = str(result.get("reason") or "not-dismissed")
            break

        outcome.dismissed = True
        label = str(result.get("cls") or result.get("text") or "close")
        outcome.clicked.append(label)
        await asyncio.sleep(0.5)

        remaining = await _count_overlays(page)
        if remaining == 0:
            outcome.reason = "cleared"
            break
        outcome.reason = "still-present"

    outcome.overlays_after = await _count_overlays(page)
    if outcome.dismissed:
        logger.info(f"遮罩处理: {outcome.summary}")
    else:
        logger.debug(f"遮罩处理: 未关闭({outcome.reason}), 遮罩数={outcome.overlays_after}")

    # Esc 兜底: 有些弹层只认键盘
    if not outcome.dismissed and outcome.overlays_before > 0:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            remaining = await _count_overlays(page)
            if remaining < outcome.overlays_before:
                outcome.dismissed = True
                outcome.reason = "escape"
                outcome.clicked.append("Esc")
                outcome.overlays_after = remaining
                logger.info(f"遮罩处理: 用 Esc 关闭成功(剩余 {remaining} 个)")
        except Exception:  # noqa: BLE001
            pass

    return outcome


def outcome_from_dict(data: Optional[dict[str, Any]]) -> Optional[DismissOutcome]:
    """从字典还原(用于把结果塞进任务结果/报告)。"""
    if not data:
        return None
    return DismissOutcome(
        dismissed=bool(data.get("dismissed")),
        attempts=int(data.get("attempts") or 0),
        overlays_before=int(data.get("overlays_before") or 0),
        overlays_after=int(data.get("overlays_after") or 0),
        reason=str(data.get("reason") or ""),
        clicked=list(data.get("clicked") or []),
    )


__all__ = ["DismissOutcome", "dismiss_overlays", "outcome_from_dict"]
