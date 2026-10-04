"""
SmartCrawler 浏览器自动化模块。

BrowserManager 职责:
- 启动/关闭 Playwright 浏览器(Chromium/Firefox/WebKit, 默认 Chromium, async_playwright)
- 共享浏览器上下文: UA / 语言 / 时区 / 视口 / 会话持久化(storage_state)
- 指纹伪装脚本注入(见 anti_spider.STEALTH_JS)
- 页面并发控制(asyncio.Semaphore)
- 页面导航(带限速 + 指数退避重试)、等待、模拟交互、iframe、多标签页管理
- 调试辅助: 选择器高亮、截图

使用示例:
    async with BrowserManager(settings) as bm:
        page = await bm.new_page()
        await bm.goto(page, "https://example.com")
        await bm.human_scroll(page, times=3)
        await bm.close_page(page)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal, Optional, Union

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .anti_spider import STEALTH_JS, RateLimiter, random_headers, random_user_agent, retry_async
from .config import Settings

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class BrowserManager:
    """Playwright 浏览器生命周期与页面管理器。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # 共享上下文中的页面; isolate 页面单独记录其 context
        self._pages: list[Page] = []
        self._isolated_contexts: dict[Page, BrowserContext] = {}
        self._semaphore = asyncio.Semaphore(max(1, settings.browser.max_pages))
        self.limiter = RateLimiter(settings)  # 导航限速器(按域名)
        from .anti_spider import ProxyPool

        self._proxy_pool = ProxyPool(settings)  # 构造即初始化(start 前也可查询)
        self._started = False
        #: 本次会话是否成功恢复了已保存的登录态(供界面显示"已登录/匿名")。
        self._session_restored = False

    @property
    def session_restored(self) -> bool:
        """本次浏览器会话是否带着已保存的登录态启动。"""
        return self._session_restored

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 Playwright 与浏览器(幂等)。"""
        if self._started:
            return
        cfg = self.settings.browser
        logger.info(f"正在启动浏览器引擎: {cfg.engine} (headless={cfg.headless})")

        self._playwright = await async_playwright().start()
        engine_map = {
            "chromium": self._playwright.chromium,
            "firefox": self._playwright.firefox,
            "webkit": self._playwright.webkit,
        }
        engine = engine_map[cfg.engine]

        launch_kwargs: dict = {
            "headless": cfg.headless,
            "args": ["--disable-blink-features=AutomationControlled"] if cfg.engine == "chromium" else [],
        }
        # 浏览器级代理(整个会话共用一个代理, 由代理池轮换选取)
        proxy = self._proxy_pool.pick()
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            logger.info(f"本次会话使用代理: {ProxyPool._mask(proxy)}")

        self._browser = await engine.launch(**launch_kwargs)

        context_kwargs = await self._build_context_kwargs()
        #: 记下建上下文用的参数, 供 recycle_context() 重建一个等价的新上下文
        self._context_kwargs = context_kwargs

        # 会话恢复: 把 storage_state 直接交给 new_context —— Playwright 会同时注入
        # Cookie 与 localStorage, 比"先建上下文再 add_cookies + add_init_script"可靠得多。
        # 早先的手写实现有两个毛病: ① 上下文建好后再 add_init_script 对已打开的页面
        # 不生效; ② 用 Python repr 拼 JS 字面量, 遇到带引号/反斜杠的值会写坏。
        if cfg.storage_state:
            state_path = Path(cfg.storage_state)
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    cookies = state.get("cookies") or []
                    origins = state.get("origins") or []
                    if cookies or origins:
                        context_kwargs["storage_state"] = state
                        self._session_restored = True
                        logger.info(
                            f"已从 {state_path} 恢复会话: {len(cookies)} 个 Cookie, "
                            f"{len(origins)} 个站点的 localStorage"
                        )
                    else:
                        logger.info(f"会话文件 {state_path} 为空(还没有登录记录), 以匿名身份访问")
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(f"会话文件无法读取, 将以匿名身份访问: {exc}")
            else:
                logger.info(f"会话文件尚不存在({state_path}), 以匿名身份访问")

        self._context = await self._browser.new_context(**context_kwargs)

        if cfg.stealth:
            await self._context.add_init_script(STEALTH_JS)

        self._started = True
        logger.info("浏览器启动完成")

    async def recycle_context(self) -> bool:
        """丢弃当前浏览器上下文并新建一个等价的, 返回是否成功。

        **为什么需要**: 有些站点(实测网易云音乐)会在**同一个浏览器上下文累积了多次访问
        之后**开始返回降级页面 —— 内容 iframe 只加载骨架(43 个元素), 而列表数据始终不注入,
        接口也不报错(全是 HTTP 200)。此时**重新导航没有用**, 因为问题出在上下文这一层;
        换一个全新的上下文立刻恢复(实测: 旧上下文 43 元素 / 0 首歌 -> 新上下文 949 元素 /
        150 首歌, 连续 3 次稳定)。

        实现上直接关掉旧上下文再建新的 —— 页面属于上下文, 会随之失效; 调用方需要重新导航。
        """
        if not self._started or self._browser is None:
            return False
        try:
            if self._context is not None:
                await self._context.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"关闭旧上下文时出错(忽略): {exc}")
        self._pages = []
        self._isolated_contexts = {}
        try:
            kwargs = dict(getattr(self, "_context_kwargs", {}) or {})
            self._context = await self._browser.new_context(**kwargs)
            if self.settings.browser.stealth:
                await self._context.add_init_script(STEALTH_JS)
            logger.info("已重建浏览器上下文(丢弃累积状态)")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"重建浏览器上下文失败: {type(exc).__name__}: {exc}")
            self._context = None
            self._started = False
            return False

    async def _build_context_kwargs(self) -> dict:
        """构造浏览器上下文参数(UA/视口/语言/时区)。"""
        cfg = self.settings.browser
        ua = cfg.user_agent or (random_user_agent() if self.settings.anti_spider.rotate_user_agent else "")
        kwargs: dict = {
            "viewport": {"width": cfg.viewport_width, "height": cfg.viewport_height},
            "locale": cfg.locale,
            "timezone_id": cfg.timezone,
        }
        if ua:
            kwargs["user_agent"] = ua
        else:
            # 未指定 UA 时随机一套请求头(仅取语言偏好)
            kwargs["extra_http_headers"] = random_headers()
        return kwargs

    async def close(self, *, persist_session: bool = False) -> None:
        """关闭全部页面、上下文与浏览器(幂等)。

        **``persist_session`` 默认为 False**, 这一点很关键。早先这里无条件把当前上下文的
        Cookie 写回 ``storage_state`` 文件, 带来两个真实故障:

        1. 用户点"删除已保存的会话"后, 删除动作会触发重置浏览器, 重置又调用 ``close()``
           —— 于是刚删掉的文件被浏览器里的 Cookie **原样写回来**, 表现为"删了还在、
           退出登录无效";
        2. 会话文件的权威来源应该是**用户登录那一次**明确保存的内容。任务是只读访问,
           顺手把当前状态写回去, 只会在用户已登出等情况下把好端端的登录态覆盖掉。

        需要保存时会话时, 请走显式路径(手动登录流程的 ``storage_state()`` 结果)。
        """
        for page in list(self._pages):
            await self.close_page(page, release=False)
        if self._context:
            if persist_session and self.settings.browser.storage_state:
                try:
                    Path(self.settings.browser.storage_state).parent.mkdir(parents=True, exist_ok=True)
                    await self._context.storage_state(path=self.settings.browser.storage_state)
                    logger.info(f"会话状态已保存到 {self.settings.browser.storage_state}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"保存会话状态失败: {exc}")
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        self._started = False
        logger.info("浏览器已关闭")

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 页面管理(多标签页 + 并发控制)
    # ------------------------------------------------------------------
    async def new_page(self, isolate: bool = False) -> Page:
        """新建页面。

        isolate=False: 使用共享上下文(共享 Cookie 会话, 适合同一站点的连续抓取);
        isolate=True : 独立上下文(独立 Cookie + 随机 UA, 适合隔离任务)。
        页面数量受 Semaphore 限制, 超出时挂起等待。
        """
        await self.start()
        await self._semaphore.acquire()
        try:
            if isolate or self._context is None:
                ctx = await self._browser.new_context(**(await self._build_context_kwargs()))
                if self.settings.browser.stealth:
                    await ctx.add_init_script(STEALTH_JS)
                page = await ctx.new_page()
                self._isolated_contexts[page] = ctx
            else:
                page = await self._context.new_page()
            self._pages.append(page)
            logger.debug(f"新页面已打开, 当前页面数: {len(self._pages)}")
            return page
        except Exception:
            self._semaphore.release()
            raise

    async def close_page(self, page: Page, release: bool = True) -> None:
        """关闭页面并释放并发配额。"""
        if page in self._isolated_contexts:
            ctx = self._isolated_contexts.pop(page)
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        if page in self._pages:
            self._pages.remove(page)
        if release:
            self._semaphore.release()

    def list_pages(self) -> list[Page]:
        """列出所有打开的页面。"""
        return list(self._pages)

    async def switch_page(self, index: int) -> Page:
        """切换到第 index 个页面并将其带到前台, 返回该页面。"""
        if not (0 <= index < len(self._pages)):
            raise IndexError(f"页面索引越界: {index}, 当前共 {len(self._pages)} 个页面")
        page = self._pages[index]
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        return page

    # ------------------------------------------------------------------
    # 导航与等待
    # ------------------------------------------------------------------
    #: 值得重试的 HTTP 状态: 服务端临时故障或限流。**不包含** 401/403/404 ——
    #: 那些是服务端的确定性答复, 重试不会改变结果, 只会浪费时间并把页面丢掉。
    RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})

    async def goto(
        self,
        page: Page,
        url: str,
        wait_until: WaitUntil = "domcontentloaded",
        timeout: Optional[float] = None,
    ) -> Optional[object]:
        """导航到 url: 先按域名限速, 再带指数退避重试。失败返回 None(不抛异常)。

        **状态码的策略**(这里踩过一个很深的坑): 早先的实现把"任何 >=400"都当作导航
        失败, 于是重试 3 次后返回 ``None``。后果是**页面明明已经渲染出来了, 却被整个
        丢掉** —— 例如洛谷在无权限时返回 HTTP 401 + 一个完整的错误页(``出错啦 /
        没有权限请求此资源。``), 而 401 属于"确定性答复", 重试毫无意义; 最终用户只看到
        "页面导航失败", 拿不到任何可排查的信息。

        现在的规则:
        - **可重试状态**(5xx / 429 / 408 等) 抛出异常走退避重试;
        - **其它 4xx**(401/403/404…) 视为**导航成功**并原样返回响应 —— 页面确实是
          服务端给出的答复, 由上层(访问受限诊断)去解释它。
        """
        cfg = self.settings.anti_spider
        waited = await self.limiter.wait(url)
        if waited > 0:
            logger.debug(f"限速等待 {waited:.1f}s 后访问: {url}")

        async def _go() -> object:
            assert page is not None
            resp = await page.goto(
                url,
                wait_until=wait_until,
                timeout=(timeout or self.settings.browser.timeout) * 1000,
            )
            if resp is not None and resp.status in self.RETRYABLE_STATUS:
                raise RuntimeError(f"HTTP {resp.status}(可重试): {url}")
            if resp is not None and resp.status >= 400:
                # 确定性错误: 不再重试, 但保留页面供上层诊断
                logger.warning(
                    f"页面返回 HTTP {resp.status}, 已保留该页面用于诊断: {url}"
                )
            return resp

        try:
            return await retry_async(
                _go,
                max_retries=cfg.max_retries,
                backoff_base=cfg.retry_backoff_base,
            )
        except PlaywrightTimeoutError as exc:
            logger.error(f"导航超时: {url} ({exc})")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"导航失败: {url} ({type(exc).__name__}: {exc})")
            return None

    async def wait_for_selector(
        self,
        page: Page,
        selector: str,
        timeout: float = 10.0,
        state: str = "visible",
        frame_name: str = "",
    ) -> Optional[ElementHandle]:
        """等待选择器出现, 超时返回 None(不抛异常, 便于降级处理)。

        ``frame_name``: 在指定名称的 iframe 内等待。外壳 + 内嵌 iframe 的站点必须传,
        否则等的是主文档 —— 那里没有目标元素, 只会一直等到超时。
        """
        scope: Any = page
        if frame_name:
            # 延迟导入避免循环依赖(extractor 已导入 browser 的类型)
            from .extractor import Extractor

            scope = Extractor.resolve_frame(page, frame_name)
        try:
            return await scope.wait_for_selector(selector, timeout=timeout * 1000, state=state)
        except PlaywrightTimeoutError:
            logger.warning(f"等待选择器超时: {selector}")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"等待选择器出错({type(exc).__name__}): {selector} -> {exc}")
            return None

    async def wait_for_load_state(self, page: Page, state: WaitUntil = "load", timeout: float = 15.0) -> None:
        """等待页面到达某个加载状态(容错包装)。"""
        try:
            await page.wait_for_load_state(state=state, timeout=timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"wait_for_load_state({state}) 未达成: {exc}")

    # ------------------------------------------------------------------
    # 模拟用户行为
    # ------------------------------------------------------------------
    async def click(
        self, page: Page, selector: str, timeout: float = 10.0, frame_name: str = ""
    ) -> bool:
        """点击元素(先等待可见)。``frame_name`` 指定在哪个 iframe 内点击。"""
        el = await self.wait_for_selector(page, selector, timeout=timeout, frame_name=frame_name)
        if el is None:
            return False
        try:
            await el.click()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"点击失败 {selector}: {exc}")
            return False

    async def fill(self, page: Page, selector: str, value: str, timeout: float = 10.0) -> bool:
        """填充输入框。"""
        el = await self.wait_for_selector(page, selector, timeout=timeout)
        if el is None:
            return False
        try:
            await el.fill(value)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"填充失败 {selector}: {exc}")
            return False

    async def hover(self, page: Page, selector: str, timeout: float = 10.0) -> bool:
        """悬停元素(常用于触发懒加载菜单)。"""
        el = await self.wait_for_selector(page, selector, timeout=timeout)
        if el is None:
            return False
        try:
            await el.hover()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"悬停失败 {selector}: {exc}")
            return False

    async def press(self, page: Page, key: str) -> None:
        """按下键盘按键, 如 'Enter'。"""
        try:
            await page.keyboard.press(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"按键失败 {key}: {exc}")

    async def human_scroll(
        self, page: Page, times: int = 3, step: int = 600, pause: float = 0.8, back_to_top: bool = False
    ) -> None:
        """模拟人类滚动(随机步长 + 随机停顿), 常用于触发懒加载/无限流加载。"""
        for _ in range(times):
            delta = step + random_jitter(120)
            try:
                await page.mouse.wheel(0, delta)
            except Exception:  # noqa: BLE001
                await page.evaluate(f"window.scrollBy(0, {delta})")
            await asyncio.sleep(pause + random_jitter(0.4) * 0.1)
        if back_to_top:
            await page.evaluate("window.scrollTo(0, 0)")

    # ------------------------------------------------------------------
    # iframe 支持
    # ------------------------------------------------------------------
    async def get_frame(self, page: Page, frame_selector: str, timeout: float = 10.0) -> Optional[Frame]:
        """按 CSS 选择器定位 iframe 并返回 Frame 对象(用于 iframe 内内容捕获)。"""
        el = await self.wait_for_selector(page, frame_selector, timeout=timeout)
        if el is None:
            return None
        frame = await el.content_frame()
        return frame

    # ------------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------------
    async def highlight(self, page: Page, selector: str) -> int:
        """在页面上用红色描边高亮选择器命中的元素(调试用), 返回命中数量。"""
        try:
            count = await page.evaluate(
                """(sel) => {
                    const els = document.querySelectorAll(sel);
                    els.forEach(el => {
                        el.style.outline = '2px solid red';
                        el.style.outlineOffset = '2px';
                    });
                    return els.length;
                }""",
                selector,
            )
            return int(count)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"高亮失败 {selector}: {exc}")
            return 0

    async def screenshot(self, page: Page, path: str, full_page: bool = False) -> Optional[str]:
        """页面截图(调试用), 返回文件路径或 None。"""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=path, full_page=full_page)
            logger.info(f"截图已保存: {path}")
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"截图失败: {exc}")
            return None


def random_jitter(amplitude: float) -> float:
    """[-amplitude, +amplitude] 的随机抖动。"""
    import random as _random

    return _random.uniform(-amplitude, amplitude)
