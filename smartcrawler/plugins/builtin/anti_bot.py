"""
内置插件: 反爬增强(默认关闭)。

提供几项与核心反爬模块**互补**的能力 —— 核心负责"降低被识别概率", 这里负责
"让每次会话更像人、并在被拦时给出可诊断的信号":

- ``extra_headers``: 注入自定义请求头(如站点要求的 ``X-Requested-With``、鉴权头);
- ``scroll_times`` / ``scroll_step``: 导航后模拟人类滚动, 触发懒加载;
- ``dwell_seconds``: 页面停留时间, 让访问节奏不"机械匀速";
- ``hide_webdriver``: 额外补一层 ``navigator.webdriver`` 伪装(核心 stealth 失效时的兜底);
- ``detect_block``: 识别"访问过于频繁/需验证"这类拦截页并写入日志, 方便判断是
  反爬拦截还是选择器写错 —— 这两者的表象(空结果)完全一样, 极易误判。

配置里的 ``extra_headers`` 用 JSON 或 ``Key: Value`` 每行一条的写法都可以。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ...anti_spider import STEALTH_JS
from ..base import BasePlugin, PluginContext

#: 常见拦截页特征(中英双语, 覆盖主流 WAF 与 CDN)
_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"访问(过于)?频繁|请稍后再试|操作太频繁", "站点提示访问频繁"),
    (r"人机验证|安全验证|滑动验证|拖动滑块|请完成验证", "出现人机验证"),
    (r"verify you are human|are you a robot|unusual traffic", "人机校验英文提示"),
    (r"access denied|forbidden|请求被拒绝", "访问被拒绝"),
    (r"cf-browser-verification|cf_chl_|challenge-platform", "Cloudflare 挑战页"),
    (r"captcha|recaptcha|hcaptcha|geetest", "验证码组件"),
    (r"您的访问速度过快|blocked by|security check", "安全拦截"),
)


class AntiBotPlugin(BasePlugin):
    """补充请求头、模拟人类节奏, 并识别拦截页(默认关闭)。"""

    id = "anti-bot"
    name = "反爬增强"
    description = "注入自定义请求头、模拟人类滚动与停留节奏, 并识别被拦截的页面"
    version = "1.0.0"
    author = "SmartCrawler"
    category = "anti-bot"
    tags = ["反爬", "请求头", "限速"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {
            "key": "extra_headers",
            "label": "附加请求头",
            "type": "textarea",
            "default": "",
            "description": '支持 JSON {"X-A":"1"} 或每行 "Key: Value"。留空不注入',
        },
        {
            "key": "scroll_times",
            "label": "导航后滚动次数",
            "type": "int",
            "default": 2,
            "min": 0,
            "max": 50,
            "description": "0 表示不滚动",
        },
        {
            "key": "scroll_step",
            "label": "每次滚动像素",
            "type": "int",
            "default": 600,
            "min": 50,
            "max": 5000,
        },
        {
            "key": "dwell_seconds",
            "label": "页面停留(秒)",
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "max": 60.0,
            "description": "额外随机停留 0~该值秒, 让访问节奏更自然",
        },
        {
            "key": "hide_webdriver",
            "label": "额外隐藏 webdriver",
            "type": "bool",
            "default": True,
            "description": "在核心 stealth 之上再补一层伪装",
        },
        {
            "key": "detect_block",
            "label": "识别拦截页",
            "type": "bool",
            "default": True,
            "description": "命中拦截特征时写日志, 便于区分「被反爬」与「选择器写错」",
        },
    ]

    # ------------------------------------------------------------------
    async def before_navigate(self, ctx: PluginContext) -> None:
        """导航前注入额外请求头(通过 context 的 route 拦截实现)。"""
        headers = self._parse_headers(ctx.config.get("extra_headers"))
        if not headers or ctx.page is None:
            return
        context = getattr(ctx.page, "context", None)
        if context is None:
            return
        try:
            await context.set_extra_http_headers(headers)
            ctx.notify("INFO", f"反爬增强: 已注入 {len(headers)} 个附加请求头")
        except Exception as exc:  # noqa: BLE001
            ctx.notify("WARNING", f"反爬增强: 注入请求头失败 {exc}")

    async def after_navigate(self, ctx: PluginContext) -> None:
        """导航后: 补伪装、模拟滚动、停留、检测拦截页。"""
        if ctx.page is None:
            return

        if bool(ctx.config.get("hide_webdriver", True)):
            try:
                await ctx.page.add_init_script(STEALTH_JS)
                await ctx.page.evaluate(
                    "() => { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); }"
                )
            except Exception:  # noqa: BLE001 - 伪装失败不影响抓取
                pass

        times = int(ctx.config.get("scroll_times") or 0)
        if times > 0 and ctx.crawler is not None:
            try:
                await ctx.crawler.browser.human_scroll(
                    ctx.page, times=times, step=int(ctx.config.get("scroll_step") or 600)
                )
                ctx.notify("DEBUG", f"反爬增强: 已模拟滚动 {times} 次")
            except Exception:  # noqa: BLE001
                pass

        dwell = float(ctx.config.get("dwell_seconds") or 0.0)
        if dwell > 0:
            import random

            await asyncio.sleep(random.uniform(0, dwell))

        if bool(ctx.config.get("detect_block", True)):
            await self._detect_block(ctx)

    async def _detect_block(self, ctx: PluginContext) -> None:
        """在页面文本与标题里找拦截特征, 命中则告警。"""
        try:
            text = await ctx.page.evaluate(
                "() => ((document.title || '') + '\\n' + (document.body ? document.body.innerText : '')).slice(0, 20000)"
            )
        except Exception:  # noqa: BLE001
            return
        haystack = (text or "").lower()
        hits = [label for pattern, label in _BLOCK_PATTERNS if re.search(pattern, haystack, re.IGNORECASE)]
        if hits:
            ctx.notify(
                "WARNING",
                "反爬增强: 当前页面疑似被拦截("
                + "、".join(hits)
                + ") —— 若结果为空, 请先排查拦截而不是选择器",
            )

    @staticmethod
    def _parse_headers(raw: Any) -> dict[str, str]:
        """解析附加请求头, 兼容 JSON 与 ``Key: Value`` 每行一条两种写法。"""
        import json

        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        text = str(raw).strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in "{}" or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().strip('"').strip("'")
            value = value.strip().strip('"').strip("'").rstrip(",")
            if key:
                out[key] = value
        return out


__all__ = ["AntiBotPlugin"]
