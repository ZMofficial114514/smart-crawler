"""
人机验证 / 风控挑战的识别与"交给用户手动过验证"。

**为什么需要它**: 有些站点(例如 pixiv)登录时总会拉起人机验证;还有站点在抓取过程中
突然弹出 Cloudflare / reCAPTCHA 挑战。这类验证**只有人能过**, 框架既不该也无法代劳。
合理的做法是: 识别出挑战 → 打开一个可见窗口让用户手动过 → 用户确认后带上新会话继续。

**难点在于"别误报"**: 很多站点(pixiv 就是)每次访问都会挂上 reCAPTCHA 的 anchor
iframe, 但只在部分情况才真的弹出挑战。实测数据:

- 未挑战时: anchor iframe 尺寸 256x60 但 ``visibility: hidden`` → 不可见;
- 挑战时: 会出现一个**可见**的 bframe / 挑战框。

因此判据以"**可见的**挑战元素"为准, 而不是"页面上提到了 captcha"。这一条如果不做,
就会变成每次访问都提示"需要人机验证", 比不提示更糟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from playwright.async_api import Page

# ---------------------------------------------------------------------------
# 采集脚本: 只回报"确实正在要求验证"的证据
# ---------------------------------------------------------------------------
DETECT_JS = r"""
() => {
    const visible = (el) => {
        if (!el) return false;
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 8 && r.height > 8;
    };

    // 挑战类 iframe: 必须**可见且尺寸够大**。
    // reCAPTCHA 在不需要挑战时也会有一个 anchor iframe, 但它是 hidden 的 ——
    // 这正是"已加载"与"正在挑战"的分界。
    const CHALLENGE_RE = /recaptcha|hcaptcha|turnstile|captcha|challenge|geetest|arkose|funcaptcha/i;
    const challengeFrames = [];
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.src || '';
        if (!CHALLENGE_RE.test(src)) continue;
        const r = f.getBoundingClientRect();
        // bframe 是 reCAPTCHA 真正弹出挑战时的容器; anchor 只是脚本锚点
        const isChallengeFrame = /bframe|hcaptcha|turnstile|captcha\/challenge|geetest/i.test(src);
        if (visible(f) && r.width >= 100 && r.height >= 60 && isChallengeFrame) {
            challengeFrames.push({
                src: src.slice(0, 200),
                width: Math.round(r.width),
                height: Math.round(r.height),
                title: f.title || '',
            });
        }
    }

    // 显式声明的挑战容器(带 data-sitekey 的 g-recaptcha / turnstile / h-captcha)
    const explicit = [];
    for (const el of document.querySelectorAll(
        '.g-recaptcha[data-sitekey], .cf-turnstile, .h-captcha, #geetest-box, [class*="captcha-container"], [id*="captcha" i]'
    )) {
        if (visible(el)) {
            const r = el.getBoundingClientRect();
            explicit.push({
                tag: el.tagName.toLowerCase(),
                cls: (el.className || '').toString().slice(0, 80),
                width: Math.round(r.width),
                height: Math.round(r.height),
            });
        }
    }

    const bodyText = (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ');
    const textHit = /verify you are human|确认您是真人|请(完成|通过)验证|滑动(验证|滑块)|人机验证|安全验证|不是机器人|i'?m not a robot|checking your browser|just a moment|正在验证|请稍候/i
        .test(bodyText);

    return {
        url: window.location.href,
        title: (document.title || '').trim(),
        challenge_frames: challengeFrames,
        explicit_containers: explicit,
        text_hit: textHit,
        body_text: bodyText.slice(0, 400),
    };
}
"""

#: 各挑战类型的识别与展示名
_VENDORS: tuple[tuple[str, str], ...] = (
    (r"recaptcha\.net|google\.com/recaptcha", "Google reCAPTCHA"),
    (r"hcaptcha\.com", "hCaptcha"),
    (r"challenges\.cloudflare\.com|turnstile", "Cloudflare Turnstile"),
    (r"geetest", "极验验证"),
    (r"arkose|funcaptcha", "Arkose Labs"),
    (r"tcaptcha|tencent", "腾讯验证码"),
)


@dataclass
class Challenge:
    """页面上正在要求用户完成的验证。"""

    detected: bool = False
    #: 人类可读的类型名, 如 "Google reCAPTCHA"
    kind: str = "人机验证"
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    #: 命中的挑战元素(iframe / 容器)
    elements: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    page_title: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def summary(self) -> str:
        if not self.detected:
            return "未检测到人机验证"
        return f"页面要求完成{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "kind": self.kind,
            "confidence": self.confidence,
            "summary": self.summary(),
            "reasons": self.reasons,
            "elements": self.elements,
            "url": self.url,
            "page_title": self.page_title,
            "checked_at": self.checked_at,
        }


def evaluate_challenge(raw: dict[str, Any]) -> Challenge:
    """由采集到的原始证据计算判定(纯函数, 便于单测)。"""
    result = Challenge(
        url=str(raw.get("url") or ""),
        page_title=str(raw.get("title") or ""),
    )

    frames = raw.get("challenge_frames") or []
    explicit = raw.get("explicit_containers") or []
    text_hit = bool(raw.get("text_hit"))

    if frames:
        result.confidence += 0.7
        result.elements.extend(frames[:4])
        result.reasons.append(f"页面上有 {len(frames)} 个可见的验证组件(含挑战框)")
    if explicit:
        result.confidence += 0.5
        result.elements.extend(explicit[:4])
        result.reasons.append(f"存在 {len(explicit)} 个显式验证容器(data-sitekey / captcha 容器)")
    if text_hit:
        result.confidence += 0.35
        result.reasons.append("页面文案提示需要完成验证")

    # 识别厂商(仅用于展示, 不参与判定)
    blob = " ".join(str(e.get("src") or e.get("cls") or "") for e in result.elements)
    for pattern, name in _VENDORS:
        if re.search(pattern, blob, re.I):
            result.kind = name
            break
    if result.kind == "人机验证" and text_hit:
        result.kind = "人机验证(类型未知)"

    # 判定门槛: 必须有**元素级证据**(可见的挑战框或显式容器), 不能只靠文案。
    # 很多站点(含 pixiv 登录页)都有 "This site is protected by reCAPTCHA" 这类声明文字,
    # 单凭文案会大面积误报 —— 那比不提示更糟。
    result.confidence = min(result.confidence, 1.0)
    has_element_evidence = bool(frames or explicit)
    result.detected = has_element_evidence and result.confidence >= 0.5
    if not has_element_evidence and text_hit:
        result.reasons.append("仅有验证类文案提示, 没有可见的验证组件, 不作为挑战处理")

    return result


def detect_challenge_sync(page: Any) -> Challenge:
    """同步版本(用于同步 API 的辅助进程)。"""
    try:
        raw: dict[str, Any] = page.evaluate(DETECT_JS)
    except Exception as exc:  # noqa: BLE001
        result = Challenge()
        result.reasons.append(f"检测脚本执行失败({type(exc).__name__}), 按未检测处理")
        return result
    return evaluate_challenge(raw)


async def detect_challenge(page: Page) -> Challenge:
    """检查当前页面是否正在要求人机验证(不抛异常)。"""
    try:
        raw: dict[str, Any] = await page.evaluate(DETECT_JS)
    except Exception as exc:  # noqa: BLE001
        result = Challenge()
        result.reasons.append(f"检测脚本执行失败({type(exc).__name__}), 按未检测处理")
        return result
    return evaluate_challenge(raw)


async def wait_for_challenge_cleared(
    page: Page,
    *,
    timeout: float = 300.0,
    interval: float = 2.0,
) -> tuple[bool, Challenge]:
    """等用户把验证过掉。返回 ``(是否已通过, 最后一次判定)``。

    判据是"**挑战元素消失**", 而不是"用户点了确认" —— 用户可能以为自己过了但页面还在
    挑战, 直接继续会立刻再次被拦。超时则如实回报未通过。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    challenge = await detect_challenge(page)
    while loop.time() < deadline:
        if not challenge.detected:
            return True, challenge
        await asyncio.sleep(interval)
        challenge = await detect_challenge(page)
    return not challenge.detected, challenge


def challenge_from_dict(data: Optional[dict[str, Any]]) -> Optional[Challenge]:
    """从字典还原(用于把判定结果塞进任务结果/状态文件)。"""
    if not data:
        return None
    return Challenge(
        detected=bool(data.get("detected")),
        kind=str(data.get("kind") or "人机验证"),
        confidence=float(data.get("confidence") or 0.0),
        reasons=list(data.get("reasons") or []),
        elements=list(data.get("elements") or []),
        url=str(data.get("url") or ""),
        page_title=str(data.get("page_title") or ""),
    )


__all__ = [
    "Challenge",
    "DETECT_JS",
    "detect_challenge",
    "detect_challenge_sync",
    "evaluate_challenge",
    "wait_for_challenge_cleared",
    "challenge_from_dict",
]
