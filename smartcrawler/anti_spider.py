"""
SmartCrawler 反爬与稳定性模块。

包含:
- USER_AGENTS 池与随机请求头
- ProxyPool: 代理轮换、失败剔除、冷却恢复
- RateLimiter: 按域名随机延时限速(默认 1~3 秒, 合规默认值)
- STEALTH_JS: Playwright init script, 伪装 navigator.webdriver / Canvas / WebGL 等指纹
- RetryPolicy: 指数退避重试
- RobotsChecker: robots.txt 合规检查(默认开启)

⚠️ 合规提示: 本框架仅限用于对目标网站拥有合法授权或该网站允许的数据采集场景。
   默认遵守 robots.txt、默认限速。请勿将本工具用于任何违法违规用途。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger

from .config import Settings

T = TypeVar("T")

# ---------------------------------------------------------------------------
# User-Agent 池(定期自行更新)
# ---------------------------------------------------------------------------
USER_AGENTS: list[str] = [
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # macOS Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Windows Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    # macOS Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Windows Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # 移动端
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
]

ACCEPT_LANGUAGES = ["zh-CN,zh;q=0.9,en;q=0.8", "zh-CN,zh;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8"]


def random_user_agent() -> str:
    """随机返回一个 UA。"""
    return random.choice(USER_AGENTS)


def random_headers(user_agent: Optional[str] = None, referer: Optional[str] = None) -> dict[str, str]:
    """构造一套拟真随机请求头。"""
    headers = {
        "User-Agent": user_agent or random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    if referer:
        headers["Referer"] = referer
    return headers


# ---------------------------------------------------------------------------
# 指纹伪装脚本(注入到每个页面, 在任何站点脚本之前执行)
# ---------------------------------------------------------------------------
STEALTH_JS = """
(() => {
    // 1. 隐藏 navigator.webdriver(Playwright 默认为 true)
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. 补齐 window.chrome 特征(Chromium 无头模式缺失)
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}), app: { isInstalled: false } };
    }

    // 3. permissions.query 对 notifications 返回真实状态
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) =>
            params && params.name === 'notifications'
                ? Promise.resolve({ state: window.Notification ? Notification.permission : 'prompt' })
                : origQuery(params);
    }

    // 4. 语言 / 插件 / 硬件特征
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // 5. WebGL 厂商/渲染器伪装
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function (param) {
            if (param === 37445) return 'Intel Inc.';
            if (param === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, param);
        };
    } catch (e) { /* WebGL 不可用时忽略 */ }

    // 6. Canvas 指纹加入微量噪声
    try {
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function (...args) {
            const ctx = this.getContext('2d');
            if (ctx && !this.__scNoised) {
                this.__scNoised = true;
                try {
                    ctx.fillStyle = 'rgba(0,0,0,0.01)';
                    ctx.fillRect(Math.floor(Math.random() * 20), Math.floor(Math.random() * 20), 1, 1);
                } catch (e) { /* 忽略 */ }
            }
            return origToDataURL.apply(this, args);
        };
    } catch (e) { /* 忽略 */ }
})();
"""


# ---------------------------------------------------------------------------
# 代理池
# ---------------------------------------------------------------------------
class ProxyPool:
    """HTTP/SOCKS5 代理池: 轮换选取, 失败剔除, 冷却后自动恢复。

    代理来源优先级: 显式传入 > 配置文件 anti_spider.proxies > 环境变量 PROXY_POOL(逗号分隔)。
    """

    def __init__(self, settings: Settings, proxies: Optional[list[str]] = None) -> None:
        import os

        env_proxies = [
            p.strip()
            for p in os.getenv("PROXY_POOL", "").split(",")
            if p.strip()
        ]
        self._proxies: list[str] = list(proxies or settings.anti_spider.proxies or env_proxies)
        self._index = 0
        self._failed_until: dict[str, float] = {}
        self._success_count: dict[str, int] = {}
        if self._proxies:
            logger.info(f"代理池初始化完成, 共 {len(self._proxies)} 个代理")

    def pick(self) -> Optional[str]:
        """轮换选取一个可用代理; 池为空或全部冷却时返回 None(直连)。"""
        now = time.time()
        n = len(self._proxies)
        for _ in range(n):
            proxy = self._proxies[self._index % n]
            self._index += 1
            if self._failed_until.get(proxy, 0) <= now:
                return proxy
        return None

    def mark_failed(self, proxy: Optional[str]) -> None:
        """标记代理失败, 进入冷却期。"""
        if proxy:
            self._failed_until[proxy] = time.time() + 300.0
            logger.warning(f"代理标记失败进入冷却 5 分钟: {self._mask(proxy)}")

    def mark_ok(self, proxy: Optional[str]) -> None:
        """标记代理成功, 清除冷却。"""
        if proxy:
            self._failed_until.pop(proxy, None)
            self._success_count[proxy] = self._success_count.get(proxy, 0) + 1

    @property
    def size(self) -> int:
        return len(self._proxies)

    @staticmethod
    def _mask(proxy: str) -> str:
        """脱敏日志输出(隐藏账号密码)。"""
        if "@" in proxy:
            scheme_rest, _, host = proxy.rpartition("@")
            return f"{scheme_rest.split('//')[0]}//***@{host}"
        return proxy


# ---------------------------------------------------------------------------
# 限速器
# ---------------------------------------------------------------------------
class RateLimiter:
    """按域名随机延时限速: 同一域名两次访问间隔不小于 [min, max] 内的随机值。"""

    def __init__(self, settings: Settings) -> None:
        lo, hi = settings.anti_spider.random_delay_range or [0.0, 0.0]
        self.min_delay = max(0.0, float(lo))
        self.max_delay = max(self.min_delay, float(hi))
        self._last_access: dict[str, float] = {}

    async def wait(self, url: str) -> float:
        """访问 url 前调用; 返回实际等待的秒数。"""
        host = urlsplit(url).netloc or "global"
        delay = random.uniform(self.min_delay, self.max_delay)
        now = time.monotonic()
        last = self._last_access.get(host)
        sleep_for = 0.0
        if last is not None:
            sleep_for = max(0.0, last + delay - now)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        self._last_access[host] = time.monotonic()
        return sleep_for


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------
async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
    **kwargs: Any,
) -> T:
    """带指数退避的异步重试: sleep = backoff_base ** attempt + 随机抖动。

    最终一次仍失败则抛出原异常。
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络类异常种类多, 统一兜底
            last_exc = exc
            if attempt >= max_retries:
                break
            sleep_for = backoff_base**attempt + random.uniform(0, 0.5)
            if on_retry:
                on_retry(attempt, exc)
            logger.warning(
                f"第 {attempt + 1} 次尝试失败({type(exc).__name__}: {exc}), "
                f"{sleep_for:.1f}s 后重试..."
            )
            await asyncio.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# robots.txt 合规检查
# ---------------------------------------------------------------------------
class RobotsChecker:
    """robots.txt 检查器(带缓存)。遵守默认开启, 可通过配置关闭但会记录警告。"""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.anti_spider.respect_robots
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}
        self._ttl = 3600.0  # robots.txt 缓存 1 小时

    async def can_fetch(self, url: str, user_agent: str = "SmartCrawler") -> bool:
        """判断当前 UA 是否允许抓取 url。robots.txt 不可得时放行并告警。"""
        if not self.enabled:
            logger.warning("robots.txt 合规检查已关闭 —— 请确认你的采集行为合法且获得授权!")
            return True

        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        cached = self._cache.get(robots_url)
        now = time.time()
        if cached is None or now - cached[1] > self._ttl:
            rp = RobotFileParser()
            try:
                async with httpx.AsyncClient(
                    timeout=10.0, follow_redirects=True,
                    headers={"User-Agent": "SmartCrawler"},
                ) as client:
                    resp = await client.get(robots_url)
                if resp.status_code == 200 and resp.text.strip():
                    rp.parse(resp.text.splitlines())
                elif resp.status_code == 404:
                    rp.parse([])  # 无 robots.txt: 默认全部允许
                else:
                    logger.warning(f"robots.txt 返回 {resp.status_code}, 放行并继续")
                    rp.parse([])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"获取 robots.txt 失败({exc}), 放行并继续")
                rp.parse([])
            self._cache[robots_url] = (rp, now)
        allowed = self._cache[robots_url][0].can_fetch(user_agent, url)
        if not allowed:
            logger.warning(f"robots.txt 禁止抓取: {url} (UA={user_agent})")
        return allowed
