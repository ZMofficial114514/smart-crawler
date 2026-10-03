"""
抓取服务层 —— Web 界面与 ``SmartCrawler`` 核心之间的适配器。

职责边界:
- 持有 ``SmartCrawler`` 实例与会话级设置, 负责生命周期与"崩溃后自愈";
- 把界面请求翻译成核心 API 调用, 并在恰当的时机发出进度事件;
- 把核心的 ``TaskResult`` 裁剪成界面需要的形状(预览 + 落盘产物);
- 处理设置的运行时热更新与 .env 持久化。

不在这里做的事: HTTP 语义(状态码/校验)、WebSocket 传输 —— 那些属于路由层。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from loguru import logger
from pydantic import ValidationError

from .. import __version__
from ..config import ENV_FILE, PROJECT_ROOT, Settings, get_settings
from ..crawler import SmartCrawler
from ..login_state import detect_login_state
from ..models import ExtractionRule, NetworkRecord, PageStructureReport, TaskResult
from ..plugins.manager import PluginManager
from ..session import resolve_session_path
from .login_flow import STATE_FILE, LoginFlow, clear_saved_session, session_overview
from .config_store import (
    EnvConfigStore,
    build_config_schema,
    coerce_value,
    encode_env_literal,
    flatten_settings,
    is_sensitive,
    mask_secret,
    path_to_env_key,
)

from .state import PREVIEW_LIMIT, TaskManager, TaskState

# 任务结果中的大对象落盘位置
TASK_OUTPUT_DIR = PROJECT_ROOT / "data" / "web_tasks"

# 抓取的步骤时间线
CRAWL_STEPS = [
    ("prepare", "准备环境"),
    ("launch", "启动浏览器"),
    ("navigate", "打开目标页面"),
    ("analyze", "分析页面结构"),
    ("rule", "确定提取规则"),
    ("extract", "提取数据"),
    ("pagination", "跟随分页"),
    ("save", "保存结果"),
]
ANALYZE_STEPS = [
    ("launch", "启动浏览器"),
    ("navigate", "打开目标页面"),
    ("dom", "解析 DOM 结构"),
    ("network", "汇总网络请求"),
]
REQUESTS_STEPS = [
    ("launch", "启动浏览器"),
    ("navigate", "打开目标页面"),
    ("collect", "捕获 XHR / WebSocket"),
]


class ConfigError(Exception):
    """配置补丁校验失败。"""


class CrawlService:
    """会话级爬虫服务(单实例, 由 FastAPI lifespan 管理)。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.store = EnvConfigStore(ENV_FILE)
        self.schema = build_config_schema(self.settings)
        self.tasks = TaskManager(TASK_OUTPUT_DIR)

        self._crawl_crawler: Optional[SmartCrawler] = None
        self._interactive_crawler: Optional[SmartCrawler] = None
        # 取消任务后浏览器上下文可能处于不确定状态, 下次任务前强制重建
        self._crawl_dirty = False
        self._interactive_dirty = False
        # 界面上勾选的插件(空 = 交给插件自身的启用配置决定)
        self._selected_plugins: list[str] = []
        # 手动登录流程(一次只允许一个)
        self.login_flow = LoginFlow()
        #: 登录辅助进程的结果是否已收集(避免重复合并会话文件)
        self._login_collected = False
        #: 当前会话是否已经"应用到浏览器"(重置过实例)。避免轮询与确认两条路径重复重置
        self._session_applied = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        # 注意: 这里**不要**再调用 setup_logging()。它虽然幂等, 但在某些加载方式下
        # 同一份 utils 会以两个模块名出现, 各自维护一份状态, 从而重复添加 handler。
        # 日志初始化由外层入口负责(cli.cmd_web / cmd_serve / scripts)。
        logger.info("SmartCrawler Web 控制台已就绪")
        logger.info(f"配置文件: {ENV_FILE}")

    async def shutdown(self) -> None:
        for crawler in (self._crawl_crawler, self._interactive_crawler):
            if crawler is not None:
                try:
                    await crawler.close()
                except Exception as exc:  # noqa: BLE001 - 退出路径尽力而为
                    logger.debug(f"关闭浏览器时忽略异常: {exc}")
        self._crawl_crawler = None
        self._interactive_crawler = None
        logger.info("SmartCrawler Web 控制台已关闭")

    @staticmethod
    def _is_dead_browser_error(exc: BaseException) -> bool:
        """判断异常是否表示"浏览器/上下文已经死了"。

        这类错误可以靠**重建浏览器**恢复, 性质和业务错误完全不同 —— 值得重试一次,
        而不是直接把任务判失败。
        """
        text = f"{type(exc).__name__}: {exc}".lower()
        markers = (
            "targetclosederror",
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
            "websocket is not connected",
            "has been closed",
        )
        return any(m in text for m in markers)

    async def _reset_crawler(self, *, interactive: bool) -> None:
        """关掉并丢弃指定用途的爬虫实例, 下次 :meth:`_get_crawler` 会重建一个。"""
        attr = "_interactive_crawler" if interactive else "_crawl_crawler"
        dirty_attr = "_interactive_dirty" if interactive else "_crawl_dirty"
        crawler: Optional[SmartCrawler] = getattr(self, attr)
        if crawler is not None:
            with contextlib.suppress(Exception):
                await crawler.close()
        setattr(self, attr, None)
        setattr(self, dirty_attr, False)
        logger.warning("浏览器已不可用, 已重建会话(下次任务使用新实例)")

    async def _get_crawler(self, *, interactive: bool) -> SmartCrawler:
        """按用途获取爬虫实例。

        抓取任务与"分析/抓包"分成两个实例: 前者内部有串行锁(共享浏览器),
        后者若复用同一个实例就会被锁挡住, 界面上的"分析"按钮会在抓取期间卡住。
        """
        attr = "_interactive_crawler" if interactive else "_crawl_crawler"
        dirty_attr = "_interactive_dirty" if interactive else "_crawl_dirty"
        crawler: Optional[SmartCrawler] = getattr(self, attr)

        if crawler is not None and getattr(self, dirty_attr):
            logger.warning("检测到上次任务被中断, 重建浏览器会话")
            try:
                await crawler.close()
            except Exception:  # noqa: BLE001
                pass
            crawler = None
            setattr(self, dirty_attr, False)

        if crawler is None:
            crawler = SmartCrawler(self.settings)
            setattr(self, attr, crawler)
        return crawler

    def mark_dirty(self, *, interactive: bool = False) -> None:
        setattr(self, "_interactive_dirty" if interactive else "_crawl_dirty", True)

    async def reset_crawlers(self, *, reason: str = "") -> None:
        """**立即**关掉已缓存的爬虫实例, 让下次任务用新的浏览器上下文。

        为什么不能只 `mark_dirty`: 那只是标记"下次取用时重建", 而**已经打开的浏览器
        上下文仍然带着旧 Cookie**。用户删掉会话之后再核实, 会因为复用旧上下文而依然
        显示"已登录" —— 明明删了却还带着, 看起来像删除没生效。会话变更(保存/删除)必须
        真正把实例关掉, 不能靠延迟重建。
        """
        for attr, dirty_attr in (
            ("_crawl_crawler", "_crawl_dirty"),
            ("_interactive_crawler", "_interactive_dirty"),
        ):
            crawler = getattr(self, attr)
            if crawler is not None:
                with contextlib.suppress(Exception):
                    await crawler.close()
                setattr(self, attr, None)
            setattr(self, dirty_attr, False)
        if reason:
            logger.info(f"已重置浏览器会话: {reason}")

    # ------------------------------------------------------------------
    # 健康检查 / 概览
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        ai = self.settings.ai
        key = ai.resolve_api_key()
        browser_reason = "已配置" if self._browser_installed() else "未检测到 Chromium 内核, 请先执行 playwright install chromium"
        return {
            "status": "ok",
            # 版本号统一从包根读, 不要在这里硬编码(以前这里写死 0.2.0, 而包是 0.1.0)
            "version": __version__,
            "ai": {
                "enabled": ai.enabled,
                "offline": ai.offline,
                "available": self._ai_available(),
                "provider": ai.provider,
                "model": ai.model,
                "base_url": ai.effective_base_url(),
                "api_key_masked": mask_secret(key),
                "has_key": bool(key),
                "cache_enabled": ai.cache_enabled,
            },
            "browser": {
                "engine": self.settings.browser.engine,
                "headless": self.settings.browser.headless,
                "ready": browser_reason,
                "stealth": self.settings.browser.stealth,
                "max_pages": self.settings.browser.max_pages,
            },
            "compliance": {
                "respect_robots": self.settings.anti_spider.respect_robots,
                "delay_range": self.settings.anti_spider.random_delay_range,
                "proxies": self._proxy_summary(),
            },
            "paths": {
                "env_file": str(ENV_FILE),
                "project_root": str(PROJECT_ROOT),
                "output_dir": str(self._output_dir()),
                "task_dir": str(TASK_OUTPUT_DIR),
            },
            "active_tasks": len(self.tasks.active()),
            "network_settle": self.settings.crawler.network_settle,
            "max_items": self.settings.crawler.max_items,
            "max_depth": self.settings.crawler.max_depth,
            "default_format": self.settings.storage.default_format,
            "plugins": self._plugin_summary(),
        }

    def _plugin_summary(self) -> dict[str, Any]:
        """插件概况(供顶栏芯片与插件页显示), 插件系统出错不影响健康检查。"""
        try:
            stats = self.crawler_plugins().stats()
            return {
                "total": stats["total"],
                "enabled": stats["enabled"],
                "user": stats["user"],
                "broken": stats["broken"],
                "ready": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"插件统计失败: {exc}")
            return {"total": 0, "enabled": 0, "user": 0, "broken": 0, "ready": False}

    def _ai_available(self) -> bool:
        ai = self.settings.ai
        if not ai.enabled or ai.offline:
            return False
        if ai.provider == "ollama":
            return True
        return bool(ai.resolve_api_key())

    def _browser_installed(self) -> bool:
        """粗判 Chromium 内核是否已下载(仅用于界面提示, 不阻塞功能)。"""
        try:
            from playwright._impl._driver import compute_driver_executable  # noqa: F401

            cache = Path.home() / "AppData" / "Local" / "ms-playwright"
            if not cache.exists():
                cache = Path.home() / ".cache" / "ms-playwright"
            return cache.exists() and any(cache.glob("chromium*"))
        except Exception:  # noqa: BLE001
            return True

    def _proxy_summary(self) -> dict[str, Any]:
        proxies = self.settings.anti_spider.proxies
        return {"count": len(proxies), "items": [self._mask_proxy(p) for p in proxies]}

    @staticmethod
    def _mask_proxy(proxy: str) -> str:
        """隐藏代理 URL 中的账号密码。"""
        from ..anti_spider import ProxyPool

        return ProxyPool._mask(proxy)

    def _output_dir(self) -> Path:
        path = Path(self.settings.storage.output_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    # ------------------------------------------------------------------
    # 插件
    # ------------------------------------------------------------------
    def crawler_plugins(self) -> PluginManager:
        """取当前会话的插件管理器(与爬虫实例共用同一个, 保证配置一致)。"""
        crawler = self._crawl_crawler or self._interactive_crawler
        if crawler is None:
            # 尚未创建爬虫实例: 先建一个托管插件管理器(此时不会启动浏览器)
            self._crawl_crawler = SmartCrawler(self.settings)
            crawler = self._crawl_crawler
        return crawler.plugins

    def prune_plugin_selection(self, known_ids: set[str]) -> None:
        """清掉已不存在的插件选择(用户删除插件文件后调用)。"""
        kept = [pid for pid in self._selected_plugins if pid in known_ids]
        if kept != self._selected_plugins:
            self._selected_plugins = kept

    # ------------------------------------------------------------------
    # 登录会话与手动登录
    # ------------------------------------------------------------------
    def session_overview(self) -> dict[str, Any]:
        """已保存会话的摘要 + 当前登录流程状态(不含任何凭据内容)。"""
        configured = self.settings.browser.storage_state
        return {
            "session": session_overview(configured),
            "configured_path": str(resolve_session_path(configured)),
            "storage_state_configured": bool(configured),
            "heads_up": (
                "会话文件等同于登录凭据, 请勿分享或提交到版本库"
                "(data/session.json 已在 .gitignore 中)"
            ),
            "flow": self.login_flow.read_status() if self.login_flow.started_at else None,
        }

    def start_login(
        self,
        url: str,
        pre_auth_url: str | None = None,
        mode: str = "login",
    ) -> dict[str, Any]:
        """打开可见浏览器让用户手动登录, 或手动过人机验证。

        ``mode``: ``login`` = 等用户登录; ``challenge`` = 等用户完成人机验证。
        ``pre_auth_url`` 仅供验收测试使用(模拟"用户已完成登录")。
        """
        configured = self.settings.browser.storage_state
        target = resolve_session_path(configured)
        status = self.login_flow.start(url, target, pre_auth_url=pre_auth_url, mode=mode)
        return {"ok": True, "status": status, "session_path": str(target), "mode": mode}

    async def login_status(self) -> dict[str, Any]:
        """轮询登录流程进度; 一旦会话数据就绪就立刻吸收进会话文件。

        **收集触发条件只看 `state.json` 是否存在, 不看进程是否退出** —— 辅助进程写完
        会话数据后还会停留两秒才退出, 而前端一看到"已保存"就会停止轮询。若把收集挂在
        "进程已退出"上, 这两秒里永远不会收集, 会话文件写不出来, 界面却已显示成功。
        """
        status = self.login_flow.read_status()

        # 会话数据已就绪且尚未吸收 → 立即吸收(幂等)
        if not self._login_collected and STATE_FILE.exists():
            self._login_collected = True
            collected = self.login_flow.collect()
            if collected.get("saved"):
                await self._on_session_saved(collected)
                # 用权威状态覆盖: "已保存"必须由会话文件本身证明, 而不是信辅助进程的说法
                status = self.login_flow.saved_status(
                    merged_cookies=collected.get("merged_cookies", 0),
                    merged_origins=collected.get("merged_origins", 0),
                )
            else:
                status = collected

        # 辅助进程已退出但还没收到数据 → 说明保存失败(或用户直接关了窗口)
        elif (
            self.login_flow.process is not None
            and self.login_flow.process.poll() is not None
            and not self._login_collected
        ):
            self._login_collected = True
            status = self.login_flow.collect()

        status["session_saved"] = bool(
            status.get("session_saved")
            or (self._login_collected and self.login_flow.verify_saved()["session_saved"])
        )
        return status

    async def _on_session_saved(self, status: dict[str, Any]) -> None:
        """会话落盘后的统一收尾: 记录日志 + 立即重置浏览器以带上新会话。

        **幂等**: 会话文件在轮询(`login_status`)里就可能已经被收集, 紧接着的
        `confirm` 不该重复重置一遍(每次重置都要关掉再开一个浏览器, 很贵)。
        """
        if self._session_applied:
            return
        self._session_applied = True
        logger.info(
            f"登录会话已生效: {status.get('merged_cookies')} 个 Cookie, "
            f"{status.get('merged_origins')} 个站点 localStorage"
        )
        # 会话变了 → 立刻关掉旧实例, 下次任务即带上登录态。
        # 用 reset 而不是 mark_dirty: 旧上下文还带着匿名 Cookie, 只标记的话"下一次"
        # 仍然可能是旧的(重新分析拿到的还是未登录页面, 正是用户遇到的那个问题)。
        await self.reset_crawlers(reason="登录会话已保存")

    async def _ask_continue_scroll(self, task: TaskState, outcome: Any) -> tuple[bool, int]:
        """问用户"内容还在增长, 要不要继续滚?"—— **一直问到用户说不滚**。

        实现: 建一个一次性事件挂在任务上, 发一条 ``confirm_scroll`` 事件给界面, 然后等
        界面回话(或超时)。超时按"停止"处理 —— 用户可能已经离开页面, 不能把任务永远挂着。

        这样"无限流"就不再是一刀切的上限: 用户想抓多久就抓多久, 直到他自己说不滚了。
        """
        # 每次追加的轮数: 用户在表单里填的优先, 否则用配置默认
        task_params = getattr(task, "params", None) or {}
        extra = int(
            task_params.get("scroll_continue_rounds")
            or self.settings.crawler.lazy_load_continue_rounds
        )
        extra = max(1, extra)
        waiter = asyncio.Event()
        task.scroll_waiter = waiter  # type: ignore[attr-defined]
        task.scroll_answer = None  # type: ignore[attr-defined]
        task.scroll_prompt = {  # type: ignore[attr-defined]
            "rounds": outcome.rounds,
            "node_gain": outcome.node_gain,
            "height_gain": outcome.height_gain,
            "summary": outcome.summary(),
            "suggested_rounds": extra,
        }

        self._emit(
            task,
            "confirm_scroll",
            message="页面内容仍在持续增长(疑似无上限), 是否继续向下滚动?",
            payload=task.scroll_prompt,  # type: ignore[attr-defined]
        )
        try:
            await asyncio.wait_for(
                waiter.wait(), timeout=self.settings.crawler.lazy_load_ask_timeout
            )
        except asyncio.TimeoutError:
            self._emit(
                task, "plugin", level="INFO", message="等待用户确认超时, 已按『停止滚动』继续"
            )
            return False, 0
        finally:
            task.scroll_waiter = None  # type: ignore[attr-defined]

        answer = getattr(task, "scroll_answer", None) or {}
        if not answer.get("continue"):
            task.scroll_prompt = None  # type: ignore[attr-defined]
            return False, 0
        return True, int(answer.get("rounds") or extra)

    def answer_scroll(self, task_id: str, cont: bool, rounds: int = 0) -> dict[str, Any]:
        """界面回话: 是否继续向下滚动。"""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "message": "任务不存在"}
        waiter = getattr(task, "scroll_waiter", None)
        if waiter is None:
            return {"ok": False, "message": "当前没有等待中的滚动确认"}
        task.scroll_answer = {"continue": bool(cont), "rounds": int(rounds or 0)}  # type: ignore[attr-defined]
        waiter.set()
        return {"ok": True, "message": "已收到, 继续滚动" if cont else "已收到, 停止滚动"}

    async def confirm_login(self, wait_seconds: float = 12.0) -> dict[str, Any]:
        """确认已登录并保存会话 —— **等会话真的写出来再返回**。

        这是修掉"界面提示保存成功、实际会话没生效"的关键: 以前这里只是转发一句
        "已请求保存会话"就立刻返回, 前端随即停止轮询; 而会话数据还在辅助进程手里,
        于是文件根本没写出来。现在这里会等到 `state.json` 出现并吸收完成, 返回的
        ``session_saved`` 是**会话文件本身**证明的结论。

        **注意**: 前端每隔 1.5 秒轮询 `login_status`, 所以会话很可能**在轮询里就已经
        被收集了**(`_login_collected=True`)。无论走哪条路径, 只要确认会话已保存, 就
        必须重置浏览器 —— 否则新会话写进了文件, 而分析用的还是**登录前那个匿名实例**,
        表现为"重新分析仍然拿到未登录页面"。
        """
        if not self.login_flow.confirm():
            return {
                "ok": False,
                "session_saved": False,
                "message": "登录流程未在运行(可能窗口已关闭)",
                "status": self.login_flow.read_status(),
            }

        # 等辅助进程把会话数据写出来(正常 1~3 秒)
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if STATE_FILE.exists():
                break
            if not self.login_flow.running:
                break  # 进程没了也没写出数据, 下面按失败处理
            await asyncio.sleep(0.3)

        collected: dict[str, Any] = {}
        if STATE_FILE.exists() and not self._login_collected:
            self._login_collected = True
            collected = self.login_flow.collect()
            if collected.get("saved"):
                await self._on_session_saved(collected)
                return {
                    "ok": True,
                    "session_saved": True,
                    "message": "会话已保存, 之后的抓取会自动带上登录态",
                    "status": self.login_flow.saved_status(
                        merged_cookies=collected.get("merged_cookies", 0),
                        merged_origins=collected.get("merged_origins", 0),
                    ),
                }

        verified = self.login_flow.verify_saved()
        if verified["session_saved"]:
            # 关键: 即便会话是在轮询里收集的, 也必须在这里重置浏览器 ——
            # 这正是"保存成功但重新分析还是未登录"的成因。
            await self._on_session_saved(
                {
                    "merged_cookies": verified.get("cookies") or collected.get("merged_cookies", 0),
                    "merged_origins": verified.get("origins") or collected.get("merged_origins", 0),
                }
            )
            return {
                "ok": True,
                "session_saved": True,
                "message": "会话已保存",
                "status": self.login_flow.saved_status(),
            }

        return {
            "ok": False,
            "session_saved": False,
            "message": "还没收到会话数据 —— 请确认已在浏览器窗口中完成登录, 然后重新点击保存",
            "status": self.login_flow.read_status(),
            "helper_log": self.login_flow.helper_log_tail(),
        }

    def cancel_login(self) -> dict[str, Any]:
        self.login_flow.cancel()
        self._login_collected = False
        self._session_applied = False
        return {"ok": True, "message": "已取消登录流程"}

    async def reset_login_flow(self) -> dict[str, Any]:
        """清理登录/验证流程状态, 并**把浏览器也重置掉**。

        只清状态文件是不够的: 浏览器上下文里可能还留着上一次登录得到的 Cookie, 于是
        "重置"之后的分析仍然是登录态 —— 用户会以为重置没生效。重置就该是重置。
        """
        self.login_flow.reset()
        self._login_collected = False
        self._session_applied = False
        await self.reset_crawlers(reason="登录流程已重置")
        return {"ok": True}

    async def clear_session(self) -> dict[str, Any]:
        """删除已保存的会话 —— 并**立刻**重置浏览器, 否则旧上下文还带着 Cookie。"""
        configured = self.settings.browser.storage_state
        result = clear_saved_session(configured)
        if result.get("removed"):
            await self.reset_crawlers(reason="会话已删除")
        # 会话被删掉了, 下次再保存时必须重新"应用"一次
        self._session_applied = False
        return {**result, "message": "会话已删除" if result.get("removed") else "本来就没有会话文件"}

    async def verify_session_effective(self, url: str) -> dict[str, Any]:
        """用**已保存的会话**打开该地址, 回报这次访问是不是登录态。

        存在的意义: 用户点完"保存会话"之后最想知道的是"到底生效了没"。光看"文件写出去了"
        不够 —— 站点可能换了 Cookie 名、会话可能已过期。这里真的带上会话访问一次并重新
        判定登录状态, 给出可核实的结论。

        注意: 先 `mark_dirty(interactive=True)` 强制重建实例, 否则会复用**登录之前**就已经
        启动、当时没带会话的那个浏览器 —— 那样测出来必然是"未生效", 反而误导用户。
        """
        self.mark_dirty(interactive=True)
        crawler = await self._get_crawler(interactive=True)
        page = await crawler.browser.new_page()
        try:
            await crawler.browser.goto(page, url)
            await asyncio.sleep(self.settings.crawler.network_settle)
            state = await detect_login_state(
                page, session_restored=crawler.browser.session_restored
            )
            title = ""
            with contextlib.suppress(Exception):
                title = await page.title()
            return {
                "ok": True,
                "url": page.url,
                "page_title": title,
                "session_restored": crawler.browser.session_restored,
                "login_state": state.state,
                "logged_in": state.logged_in,
                "confidence": state.confidence,
                "summary": state.summary(),
                "reasons": state.reasons[:6],
                "message": (
                    "会话已生效: 这次访问被识别为已登录"
                    if state.logged_in
                    else "会话已带上, 但该页面仍被判定为未登录 —— 可能是会话已失效, 或该站点的登录态特征比较特殊"
                ),
            }
        finally:
            with contextlib.suppress(Exception):
                await crawler.browser.close_page(page)

    # ------------------------------------------------------------------
    # 配置: 读取 / 校验 / 应用 / 持久化
    # ------------------------------------------------------------------
    def read_config(self) -> dict[str, Any]:
        """当前生效配置 + 每个字段的来源标注。"""
        flat = flatten_settings(self.settings.model_dump(mode="json"))
        env_dict = {k.upper(): v for k, v in self.store.as_dict().items()}
        shadowed = self.store.shadowed_keys()

        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for path, value in flat.items():
            env_key = path_to_env_key(path)
            in_env = env_key in env_dict
            sources[path] = "env" if env_key in shadowed else ("dotenv" if in_env else "default")
            values[path] = mask_secret(str(value)) if is_sensitive(path) and value else value

        return {
            "schema": self.schema,
            "values": values,
            "sources": sources,
            "shadowed": sorted(shadowed),
            "env_file": str(ENV_FILE),
        }

    def apply_patch(self, patch: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        """应用一批配置改动。

        ``patch`` 形如 ``{"browser.headless": false, "crawler.max_items": 200}``。
        流程: 校验 -> 构造新 Settings 并整体替换(失败则完全不生效) -> 写 .env。
        """
        if not patch:
            raise ConfigError("没有需要修改的配置项")

        kinds = self._field_kinds()
        unknown = [k for k in patch if k not in kinds]
        if unknown:
            raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")

        # 掩码值(界面上未改动密钥)直接跳过, 避免把 **** 写进 .env
        effective = {k: v for k, v in patch.items() if not self._is_masked(k, v)}
        if not effective:
            return {"changed": [], "restart_required": False, "applied": False, "reason": "没有实际改动"}

        raw = flatten_settings(self.settings.model_dump(mode="json"))
        changed: list[str] = []
        for path, value in effective.items():
            coerced = coerce_value(kinds[path], value)
            if raw.get(path) != coerced:
                raw[path] = coerced
                changed.append(path)

        if not changed:
            return {"changed": [], "restart_required": False, "applied": False, "reason": "取值与当前一致"}

        nested = self._unflatten(raw)
        try:
            candidate = Settings.load(ENV_FILE if ENV_FILE.exists() else None)
            candidate = type(self.settings).model_validate({**candidate.model_dump(mode="python"), **nested})
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ()))
            raise ConfigError(f"{field}: {first.get('msg', '取值非法')}") from exc

        # 静态类型已校验通过, 但再确认一次"运行时可变"约束
        enabled_errors = self._runtime_guard(candidate)
        if enabled_errors:
            raise ConfigError("; ".join(enabled_errors))

        self.settings = candidate
        if persist:
            self._persist(effective, kinds)

        restart_required = any(
            field in changed for section in self.schema["sections"] for field in [f["path"] for f in section["fields"] if f["restart_required"]]
        )
        if restart_required:
            # 浏览器相关参数变化后, 让下次任务用新参数重建会话
            self.mark_dirty(interactive=False)
            self.mark_dirty(interactive=True)

        logger.info(f"配置已更新: {', '.join(changed)}")
        return {
            "changed": changed,
            "restart_required": restart_required,
            "applied": True,
            "values": self.read_config()["values"],
        }

    @staticmethod
    def _runtime_guard(candidate: Settings) -> list[str]:
        """跨字段的运行时约束(单字段校验覆盖不到的组合)。"""
        errors: list[str] = []
        delay = candidate.anti_spider.random_delay_range
        if len(delay) != 2 or delay[0] > delay[1]:
            errors.append("随机限速区间需要两个数且左值不大于右值")
        if candidate.crawler.max_items < 1:
            errors.append("单任务条数上限至少为 1")
        return errors

    def _persist(self, effective: dict[str, Any], kinds: dict[str, str]) -> None:
        updates: dict[str, str] = {}
        for path, value in effective.items():
            env_key = path_to_env_key(path)
            raw = self.settings
            for part in path.split("."):
                raw = getattr(raw, part)
            updates[env_key] = encode_env_literal(kinds[path], raw)
        self.store.write(updates)

    @staticmethod
    def _is_masked(path: str, value: Any) -> bool:
        """识别前端回传的掩码密钥(含有连续星号即视为未修改)。"""
        return is_sensitive(path) and isinstance(value, str) and "****" in value

    def _field_kinds(self) -> dict[str, str]:
        return {
            field["path"]: field["type"]
            for section in self.schema["sections"]
            for field in section["fields"]
        }

    @staticmethod
    def _unflatten(flat: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path, value in flat.items():
            node = out
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return out

    # ------------------------------------------------------------------
    # 任务 1: 抓取
    # ------------------------------------------------------------------
    def start_crawl(self, params: dict[str, Any]) -> TaskState:
        """登记并启动一个抓取任务(立即返回任务句柄, 进度走 WebSocket)。"""
        task = self.tasks.create("crawl", params)
        task.set_steps(CRAWL_STEPS)
        handle = asyncio.create_task(self._run_crawl(task))
        self.tasks.attach_handle(task.id, handle)
        return task

    async def _run_crawl(self, task: TaskState) -> None:
        task.started_at = _now()
        task._started_perf = time.perf_counter()
        task.status = "running"
        self._emit(task, "status", status="running", message="任务已开始", progress=task.progress)

        params = task.params
        crawler = await self._get_crawler(interactive=False)
        try:
            task.step("prepare", "running")
            if params.get("use_ai"):
                if not self._ai_available():
                    task.errors.append(
                        "AI 未就绪(未配置 API Key 或处于离线模式), 将退化为规则引擎提取"
                    )
                    self._emit(task, "warning", message=task.errors[-1])
            task.step("prepare", "done", "配置已就绪")

            # 浏览器启动由 crawl() 内部触发, 这里用日志驱动步骤状态
            task.step("launch", "running")

            rule = self._parse_rule(params.get("rule"))
            output_path = self._resolve_output_path(params, task.id)

            task.step("navigate", "running")
            # 插件进度经 loguru 之外的专用通道直接推给界面, 避免只出现在日志里
            result: TaskResult = await crawler.crawl(
                url=params["url"],
                goal=params.get("goal") or None,
                rule=rule,
                format=params.get("format") or None,
                output=output_path,
                max_pages=params.get("max_pages"),
                incremental=bool(params.get("incremental", False)),
                use_ai=bool(params.get("use_ai", True)),
                extra_wait=float(params.get("wait") or 0.0),
                plugin_ids=params.get("plugin_ids") or self._selected_plugins or None,
                on_progress=lambda level, message: self._emit(task, "plugin", level=level, message=message),
                deep_scroll=int(params.get("deep_scroll") or 0),
                scroll_rounds=params.get("scroll_rounds"),
                # 下载数量: 用户在抓取页填的优先, 否则由 crawler 从抓取目标里解析
                media_limit=params.get("media_limit"),
                # 交互式续滚: 内容没上限时问用户"要不要继续", 一直到用户说不滚
                scroll_confirm=(
                    (lambda outcome: self._ask_continue_scroll(task, outcome))
                    if params.get("ask_scroll", True)
                    else None
                ),
                scroll_progress=lambda n, o: self._emit(
                    task, "plugin", level="INFO", message=f"滚动加载: 第 {n} 轮, {o.summary()}"
                ),
            )

            self._fill_steps_from_result(task, result)
            self._finalize_crawl(task, result, output_path)

        except asyncio.CancelledError:
            self.mark_dirty(interactive=False)
            task.errors.append("任务被用户取消")
            self._finish_task(task, "cancelled", "任务已取消")
            self.tasks.persist_result(task, {"kind": "crawl", "params": params, "status": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成任务状态而不是 500
            # 浏览器在空闲时可能被系统/Chromium 回收掉, 于是复用它的任务会立刻抛
            # TargetClosedError。这类错误**重建浏览器就能恢复**, 所以重建后重试一次,
            # 而不是让用户看到一条"任务失败"然后手动再点一遍。
            if self._is_dead_browser_error(exc) and not getattr(task, "_retried", False):
                task._retried = True  # type: ignore[attr-defined]
                logger.warning(f"抓取因浏览器失效中断, 重建后重试一次: {exc}")
                self._emit(task, "warning", message="浏览器已失效, 正在重建并自动重试…")
                await self._reset_crawler(interactive=False)
                await self._run_crawl(task)
                return
            self.mark_dirty(interactive=False)
            logger.exception(f"抓取任务异常: {exc}")
            task.errors.append(f"{type(exc).__name__}: {exc}")
            self._finish_task(task, "failed", f"执行失败: {exc}")
            self.tasks.persist_result(task, {"kind": "crawl", "params": params, "status": "failed", "errors": task.errors})

    def _finalize_crawl(self, task: TaskState, result: TaskResult, output_path: Optional[str]) -> None:
        """把 TaskResult 裁剪成界面负载, 并登记可下载产物。

        要点: 界面只需要预览(前 PREVIEW_LIMIT 条), 但**落盘的产物必须包含全部数据**,
        否则用户点"下载完整结果"只能拿到预览。这里先构造含全量的 payload, 再从它派生
        界面用的预览副本。
        """
        items = result.items or []
        rule_dump = result.rule.model_dump(mode="json") if result.rule else None
        report_dump = _trim_report(result.structure_report)
        access_dump = result.access_issue.model_dump(mode="json") if result.access_issue else None
        download_dump = [d.model_dump(mode="json") for d in result.downloads]

        summary = {
            "url": result.url,
            "goal": result.goal,
            "success": result.success,
            "item_count": result.item_count,
            "pages_crawled": result.pages_crawled,
            "network_record_count": result.network_record_count,
            "websocket_record_count": result.websocket_record_count,
            "duration_ms": result.duration_ms,
            "saved_to": result.saved_to,
            "errors": result.errors,
            "rule": rule_dump,
            "structure_report": report_dump,
            "columns": _columns(items),
            # ---- 访问诊断与插件 ----
            "access_issue": access_dump,
            "login_state": result.login_state,
            "challenge": result.challenge,
            "lazy_load": result.lazy_load,
            "overlays": result.overlays,
            "downloads": download_dump,
            "download_ok": sum(1 for d in result.downloads if d.ok),
            "plugins_used": result.plugins_used,
            "plugin_errors": result.plugin_errors,
        }

        # 1) 完整结果落盘(含全部条目), 供下载
        artifact = self.tasks.persist_result(
            task,
            {
                "kind": "crawl",
                "task_id": task.id,
                "params": task.params,
                "finished_at": _now(),
                "result": {**summary, "items": items},
            },
        )

        # 2) 界面负载: 只放预览, 避免几万条数据塞进 WebSocket 帧
        task.result = {
            **summary,
            "items_preview": items[:PREVIEW_LIMIT],
            "items_truncated": len(items) > PREVIEW_LIMIT,
        }

        if artifact:
            task.artifacts.append(
                {"name": Path(artifact).name, "path": artifact, "kind": "json", "label": "完整结果 (JSON)"}
            )
        if result.saved_to and Path(result.saved_to).exists():
            task.artifacts.append(
                {
                    "name": Path(result.saved_to).name,
                    "path": result.saved_to,
                    "kind": Path(result.saved_to).suffix.lstrip(".") or "file",
                    "label": "导出文件",
                }
            )
        # 把核心错误合并进任务错误列表
        for err in result.errors:
            if err not in task.errors:
                task.errors.append(err)

        if result.success:
            message = f"抓取完成, 共 {result.item_count} 条 / {result.pages_crawled} 页"
            status = "success"
        elif result.errors and any("robots" in e for e in result.errors):
            status, message = "failed", "被 robots.txt 拒绝(合规优先), 任务终止"
        elif not items:
            status, message = "failed", "未提取到任何数据, 请尝试放宽规则或增大「额外等待」"
        else:
            status, message = "failed", f"部分完成: {result.item_count} 条, 但存在错误"

        # 先推结果再推终态, 界面据此切回"已完成"并隐藏取消按钮
        self._finish_task(task, status, message, result=task.result)

    # ------------------------------------------------------------------
    # 按 URL 归类结果与产出, 以及按 URL 清理
    # ------------------------------------------------------------------
    def list_url_groups(self, limit: int = 200) -> list[dict[str, Any]]:
        """把任务按**目标 URL** 归类, 并把产出文件挂到各自的 URL 下。

        这样"结果与历史"看到的就是"某个站抓过几次、产出哪些文件", 而不是一堆
        互不相干的文件混在一张列表里 —— 后者在有多个目标站点时根本对不上号。
        """
        groups: dict[str, dict[str, Any]] = {}
        for task in self.tasks.all_tasks()[:limit]:
            url = (task.params or {}).get("url") or ""
            key = url or f"(未指定 URL)::{task.kind}"
            group = groups.setdefault(
                key,
                {
                    "url": url,
                    "host": _host_of(url),
                    "tasks": [],
                    "files": [],
                    "item_count": 0,
                    "bytes": 0,
                    "kinds": [],
                },
            )
            files = self._task_files(task)
            group["tasks"].append(
                {
                    "id": task.id,
                    "kind": task.kind,
                    "status": task.status,
                    "created_at": task.created_at,
                    "duration_ms": task.duration_ms,
                    "item_count": (task.result or {}).get("item_count"),
                    "goal": (task.params or {}).get("goal") or "",
                    "file_count": len(files),
                    "bytes": sum(f.get("bytes") or 0 for f in files),
                }
            )
            group["files"].extend(files)
            group["item_count"] += int((task.result or {}).get("item_count") or 0)
            group["bytes"] += sum(f.get("bytes") or 0 for f in files)
            if task.kind not in group["kinds"]:
                group["kinds"].append(task.kind)

        # 最近一次任务的时间作为分组排序依据(新的在前)
        for group in groups.values():
            group["tasks"].sort(key=lambda t: t.get("created_at") or "", reverse=True)
            group["files"].sort(key=lambda f: f.get("modified") or "", reverse=True)
            group["last_at"] = group["tasks"][0]["created_at"] if group["tasks"] else ""
            group["task_count"] = len(group["tasks"])
            group["file_count"] = len(group["files"])
        return sorted(groups.values(), key=lambda g: g.get("last_at") or "", reverse=True)

    def _task_files(self, task: TaskState) -> list[dict[str, Any]]:
        """某个任务产出的所有文件(结果 JSON、导出文件、插件下载的图片/音频)。"""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        candidates: list[tuple[str, str, str]] = []
        for art in task.artifacts or []:
            path = str(art.get("path") or "")
            if path:
                candidates.append((path, str(art.get("kind") or "file"), str(art.get("label") or "")))
        for dl in (task.result or {}).get("downloads") or []:
            path = str(dl.get("path") or "")
            if path:
                candidates.append((path, "download", str(dl.get("filename") or "")))

        for path, kind, label in candidates:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = PROJECT_ROOT / resolved
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = resolved.stat()
                size, modified = stat.st_size, stat.st_mtime
            except OSError:
                # 文件已经不在了(被手动删过): 仍然列出来, 但标成缺失, 让人知道有这回事
                size, modified = 0, 0.0
            out.append(
                {
                    "name": resolved.name,
                    "path": key,
                    "kind": kind,
                    "label": label,
                    "bytes": size,
                    "exists": modified > 0,
                    "modified": (
                        datetime.fromtimestamp(modified).isoformat(timespec="seconds")
                        if modified
                        else ""
                    ),
                    "task_id": task.id,
                }
            )
        return out

    def delete_task(self, task_id: str, *, remove_files: bool = True) -> dict[str, Any]:
        """删除一个任务, 并清理它的产出文件。"""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "message": "任务不存在"}
        if task.status in ("queued", "running"):
            return {"ok": False, "message": "任务仍在运行, 请先取消再删除"}

        files = self._task_files(task)
        removed, freed, failed = _unlink_files([f["path"] for f in files], remove_files)
        self.tasks.remove(task_id)
        return {
            "ok": True,
            "removed_tasks": 1,
            "removed_files": removed,
            "failed_files": failed,
            "freed_bytes": freed,
            "message": f"已删除任务 {task_id}, 清理 {removed} 个产出文件",
        }

    def delete_url_group(self, url: str, *, remove_files: bool = True) -> dict[str, Any]:
        """删除某个 URL 的**全部**任务与产出文件(结果与历史里的"删除这个 URL")。

        会先拦住在跑的任务 —— 删一个正在写的任务, 文件删完又被写回来, 反而更乱。
        """
        targets = [
            t for t in self.tasks.all_tasks() if ((t.params or {}).get("url") or "") == url
        ]
        if not targets:
            return {"ok": False, "message": "没有找到这个 URL 的任务记录"}
        busy = [t.id for t in targets if t.status in ("queued", "running")]
        if busy:
            return {
                "ok": False,
                "message": f"该 URL 还有 {len(busy)} 个任务在运行, 请先取消再删除",
                "running": busy,
            }

        paths: list[str] = []
        for task in targets:
            paths.extend(f["path"] for f in self._task_files(task))
        removed, freed, failed = _unlink_files(paths, remove_files)
        for task in targets:
            self.tasks.remove(task.id)

        # 顺手清掉为这个 URL 生成的结果目录(导出文件按 URL 归到子目录里)
        extra_removed, extra_freed = _remove_url_dir(url, remove_files)
        return {
            "ok": True,
            "url": url,
            "removed_tasks": len(targets),
            "removed_files": removed + extra_removed,
            "failed_files": failed,
            "freed_bytes": freed + extra_freed,
            "message": (
                f"已删除 {url} 的 {len(targets)} 条记录"
                f", 清理 {removed + extra_removed} 个产出文件"
            ),
        }

    def _fill_steps_from_result(self, task: TaskState, result: TaskResult) -> None:
        """根据 TaskResult 回填步骤状态, 让时间线在无细粒度回调时也准确。"""
        if result.structure_report is not None:
            task.step("analyze", "done", f"候选列表 {len(result.structure_report.candidate_lists)} 个")
        else:
            task.step("analyze", "skipped", "未获取结构报告")
        task.step("launch", "done", self.settings.browser.engine)
        task.step("navigate", "done" if result.structure_report else "error", result.url)
        if result.rule is not None:
            src = {"ai": "AI 生成", "rule": "规则引擎", "manual": "手动规则"}.get(result.rule.source, result.rule.source)
            task.step("rule", "done", src)
        else:
            task.step("rule", "error", "无法确定提取规则")
        if result.item_count:
            task.step("extract", "done", f"{result.item_count} 条")
        else:
            task.step("extract", "error", "0 条")
        task.step(
            "pagination",
            "done" if result.pages_crawled > 1 else "skipped",
            f"共 {result.pages_crawled} 页",
        )
        if result.saved_to:
            task.step("save", "done", Path(result.saved_to).name)
        else:
            task.step("save", "skipped", "未落盘")

    # ------------------------------------------------------------------
    # 任务 2: 结构分析
    # ------------------------------------------------------------------
    def start_analyze(self, params: dict[str, Any]) -> TaskState:
        task = self.tasks.create("analyze", params)
        task.set_steps(ANALYZE_STEPS)
        handle = asyncio.create_task(self._run_analyze(task))
        self.tasks.attach_handle(task.id, handle)
        return task

    async def _run_analyze(self, task: TaskState) -> None:
        task.started_at = _now()
        task._started_perf = time.perf_counter()
        task.status = "running"
        self._emit(task, "status", status="running", message="开始分析页面结构", progress=task.progress)
        crawler = await self._get_crawler(interactive=True)
        try:
            task.step("launch", "running")
            task.step("navigate", "running")
            report, stats = await crawler.analyze_only(
                task.params["url"],
                deep_scroll=int(task.params.get("deep_scroll") or 0),
                scroll_rounds=task.params.get("scroll_rounds"),
                # **分析默认不阻塞询问**: 只滚一轮就直接出报告, 由报告里的
                # lazy_load.infinite + 界面上的「继续向下滚动」按钮让用户按需加深。
                # 若在这里也阻塞等待, 一次普通分析就会卡住两分钟 —— 实测就是这么把
                # "分析中…" 永久挂住的, 连登录提示条都出不来。
                scroll_confirm=(
                    (lambda outcome: self._ask_continue_scroll(task, outcome))
                    if task.params.get("ask_scroll")
                    else None
                ),
                scroll_progress=lambda n, o: self._emit(
                    task, "plugin", level="INFO", message=f"滚动加载: 第 {n} 轮, {o.summary()}"
                ),
            )
            task.step("launch", "done", self.settings.browser.engine)
            if report is None:
                task.step("navigate", "error", "页面打开失败")
                task.finish("failed", "页面分析失败: 无法打开页面或超时")
                self._emit(task, "status", status="failed", message=task.message, progress=task.progress)
                return
            task.step("navigate", "done", report.title or task.params["url"])
            task.step("dom", "done", f"{len(report.candidate_lists)} 个候选列表")
            task.step("network", "done", f"{stats.get('total', 0)} 条请求")

            task.result = {"report": _trim_report(report), "network_stats": stats, "url": task.params["url"]}
            artifact = self.tasks.persist_result(task, {"kind": "analyze", "params": task.params, "result": task.result})
            if artifact:
                task.artifacts.append({"name": Path(artifact).name, "path": artifact, "kind": "json", "label": "结构报告"})
            self._finish_task(
                task, "success", f"分析完成: {len(report.candidate_lists)} 个候选列表", result=task.result
            )
        except asyncio.CancelledError:
            self.mark_dirty(interactive=True)
            task.errors.append("任务被用户取消")
            self._finish_task(task, "cancelled", "任务已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            # 同抓取任务: 浏览器失效属于可恢复错误, 重建后重试一次。
            # 这条对"分析"尤其重要 —— 用户点一次分析就期待一个结果,
            # 而不是看到一条"浏览器已关闭"然后自己再点一遍。
            if self._is_dead_browser_error(exc) and not getattr(task, "_retried", False):
                task._retried = True  # type: ignore[attr-defined]
                logger.warning(f"分析因浏览器失效中断, 重建后重试一次: {exc}")
                self._emit(task, "warning", message="浏览器已失效, 正在重建并自动重试…")
                await self._reset_crawler(interactive=True)
                await self._run_analyze(task)
                return
            self.mark_dirty(interactive=True)
            logger.exception(f"结构分析异常: {exc}")
            task.errors.append(f"{type(exc).__name__}: {exc}")
            self._finish_task(task, "failed", str(exc))

    # ------------------------------------------------------------------
    # 任务 3: 网络抓包
    # ------------------------------------------------------------------
    def start_requests(self, params: dict[str, Any]) -> TaskState:
        task = self.tasks.create("requests", params)
        task.set_steps(REQUESTS_STEPS)
        handle = asyncio.create_task(self._run_requests(task))
        self.tasks.attach_handle(task.id, handle)
        return task

    async def _run_requests(self, task: TaskState) -> None:
        task.started_at = _now()
        task._started_perf = time.perf_counter()
        task.status = "running"
        self._emit(task, "status", status="running", message="开始捕获网络请求", progress=task.progress)
        crawler = await self._get_crawler(interactive=True)
        params = task.params
        try:
            task.step("launch", "running")
            task.step("navigate", "running")
            records = await crawler.capture_requests(
                params["url"],
                wait_seconds=float(params.get("wait") or 5.0),
                scroll=bool(params.get("scroll", True)),
            )
            task.step("launch", "done", self.settings.browser.engine)
            task.step("navigate", "done", params["url"])
            matched = _filter_records(records, params)
            task.step("collect", "done", f"{len(matched)} / {len(records)} 条匹配")
            task.result = {
                "url": params["url"],
                "total": len(records),
                "matched": len(matched),
                "records": [r.model_dump(exclude_none=True) for r in matched[: int(params.get("limit") or 200)]],
                "json_records": sum(1 for r in records if r.body_json is not None),
                "ws_records": len(crawler.recorder.ws_records),
            }
            artifact = self.tasks.persist_result(
                task,
                {
                    "kind": "requests",
                    "params": params,
                    "result": {**task.result, "records": [r.model_dump(exclude_none=True) for r in records]},
                },
            )
            if artifact:
                task.artifacts.append({"name": Path(artifact).name, "path": artifact, "kind": "json", "label": "完整抓包记录"})
            self._finish_task(
                task, "success", f"捕获 {len(records)} 条请求, 匹配 {len(matched)} 条", result=task.result
            )
        except asyncio.CancelledError:
            self.mark_dirty(interactive=True)
            task.errors.append("任务被用户取消")
            self._finish_task(task, "cancelled", "任务已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            self.mark_dirty(interactive=True)
            logger.exception(f"抓包任务异常: {exc}")
            task.errors.append(f"{type(exc).__name__}: {exc}")
            self._finish_task(task, "failed", str(exc))

    # ------------------------------------------------------------------
    # 辅助动作
    # ------------------------------------------------------------------
    async def test_ai(self) -> dict[str, Any]:
        """实测 AI 连通性(使用当前配置发起一次最小对话)。"""
        from ..ai import AIClient

        if not self.settings.ai.enabled or self.settings.ai.offline:
            return {"ok": False, "message": "AI 已被关闭或处于离线模式"}
        if not self._ai_available():
            return {"ok": False, "message": "未配置 API Key(在线服务商必需)"}

        client = AIClient(self.settings.ai)
        started = time.perf_counter()
        # 不写入缓存, 避免污染真实规则生成的缓存键空间
        original = self.settings.ai.cache_enabled
        self.settings.ai.cache_enabled = False
        try:
            reply = await client.chat(
                [{"role": "user", "content": "只回复两个字: 连接成功"}],
                temperature=0.0,
                max_tokens=32,
            )
        finally:
            self.settings.ai.cache_enabled = original

        elapsed = round((time.perf_counter() - started) * 1000)
        if reply is None:
            return {
                "ok": False,
                "message": "调用失败, 请检查 Base URL / 模型名 / API Key(详细原因见实时日志)",
                "elapsed_ms": elapsed,
            }
        return {
            "ok": True,
            "message": f"连通正常: {reply.strip()[:80]}",
            "elapsed_ms": elapsed,
            "model": self.settings.ai.model,
            "base_url": self.settings.ai.effective_base_url(),
        }

    async def test_proxies(self, timeout: float = 8.0) -> dict[str, Any]:
        """逐个探测代理可用性(经代理访问 ipify)。"""
        import httpx

        proxies = self.settings.anti_spider.proxies
        if not proxies:
            return {"ok": False, "message": "代理池为空", "results": []}

        results: list[dict[str, Any]] = []

        async def probe(proxy: str) -> None:
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
                    resp = await client.get("https://api.ipify.org?format=json")
                ok = resp.status_code == 200
                results.append(
                    {
                        "proxy": self._mask_proxy(proxy),
                        "ok": ok,
                        "status": resp.status_code,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                        "ip": resp.json().get("ip") if ok else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "proxy": self._mask_proxy(proxy),
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                    }
                )

        await asyncio.gather(*(probe(p) for p in proxies))
        healthy = sum(1 for r in results if r["ok"])
        return {"ok": healthy > 0, "message": f"{healthy}/{len(results)} 个代理可用", "results": results}

    def clear_ai_cache(self) -> dict[str, Any]:
        """清空 AI 响应缓存文件。"""
        path = Path(self.settings.ai.cache_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        existed = path.exists()
        size = path.stat().st_size if existed else 0
        if existed:
            path.unlink()
        logger.info(f"AI 缓存已清空({size} 字节)")
        return {"ok": True, "removed": existed, "bytes": size, "path": str(path)}

    def reset_incremental_state(self) -> dict[str, Any]:
        """清空增量抓取状态(下次抓取视为全量)。"""
        path = Path(self.settings.crawler.state_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        from ..storage import load_state, save_state

        state = load_state(str(path))
        count = len(state.get("seen_hashes", []))
        save_state(str(path), {"seen_hashes": [], "last_run": None})
        logger.info(f"增量状态已重置(清除 {count} 条历史哈希)")
        return {"ok": True, "cleared": count, "path": str(path)}

    def list_output_files(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出输出目录与任务目录中的文件, 供界面下载/管理。"""
        files: list[dict[str, Any]] = []
        for base in (self._output_dir(), TASK_OUTPUT_DIR):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True):
                if not path.is_file():
                    continue
                stat = path.stat()
                files.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "dir": base.name,
                        "size": stat.st_size,
                        "modified": _now_from_ts(stat.st_mtime),
                    }
                )
                if len(files) >= limit:
                    return files
        return files

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _emit(self, task: TaskState, event_type: str, **payload: Any) -> None:
        self.tasks.emit(task.id, {"type": event_type, **payload})

    def _finish_task(
        self,
        task: TaskState,
        status: str,
        message: str,
        *,
        result: Optional[dict[str, Any]] = None,
    ) -> None:
        """结束任务并**按顺序**推送终态事件。

        顺序很关键, 曾因此出过一个显示 bug: 早先只在 ``task.finish()`` 之后发
        ``result`` 事件, 而终态的 ``status`` 事件只有失败/取消路径才发。于是成功的
        任务在界面上会一直停留在"进行中": 前端收到 result 时任务快照仍是 running,
        状态标签和"取消任务"按钮都不会切回去, 直到用户手动刷新才恢复。

        现在统一为: 先 result(带数据), 再 status(终态), 并且终态 status 一定会发。
        status 事件同时携带 progress, 让界面把进度条一次性推到 100% —— 否则进度条会
        停留在最后一次快照的比例上, 看起来像任务还没跑完。
        """
        if result is not None:
            self._emit(task, "result", payload=result)
        task.finish(status, message)  # type: ignore[arg-type]
        self._emit(task, "status", status=task.status, message=task.message, progress=task.progress)

    def _parse_rule(self, raw: Any) -> Optional[ExtractionRule]:
        """把界面传来的规则(JSON 字符串/对象)解析为 ExtractionRule。"""
        if not raw:
            return None
        import json

        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"规则 JSON 解析失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("规则必须是一个 JSON 对象")
        try:
            rule = ExtractionRule.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"规则校验失败: {exc.errors()[0].get('msg')}") from exc
        rule.source = "manual"
        return rule

    def _resolve_output_path(self, params: dict[str, Any], task_id: str) -> Optional[str]:
        """决定落盘路径: 用户显式指定 > 默认目录自动命名 > 不落盘。"""
        custom = (params.get("output") or "").strip()
        if custom:
            path = Path(custom)
            return str(path if path.is_absolute() else PROJECT_ROOT / path)

        fmt = (params.get("format") or "").strip()
        if not fmt:
            return None
        return None  # 交给 Storage 按时间自动命名到 output_dir


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _host_of(url: str) -> str:
    """从 URL 里取主机名, 用于分组标题的简短显示。"""
    if not url:
        return ""
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


def url_slug(url: str) -> str:
    """把 URL 变成一个安全的目录名, 让产出文件按 URL 归类。

    例: ``https://www.duitang.com/search/?kw=蔚蓝档案`` -> ``www.duitang.com_search_kw``

    做这个是为了让"某个 URL 的产出"在磁盘上就是**一个目录**, 删除时能整目录清掉,
    而不是靠一张容易过期的清单去逐个删 —— 后者一旦漏记就会留下孤儿文件。
    """
    if not url:
        return "_no_url"
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    host = (parsed.netloc if parsed else "") or "site"
    path = (parsed.path if parsed else "") or ""
    raw = f"{host}{path.replace('/', '_')}"
    # 查询串只留个短摘要, 避免目录名过长(Windows 路径限制)
    query = (parsed.query if parsed else "") or ""
    if query:
        import hashlib

        raw += "_" + hashlib.sha1(query.encode("utf-8")).hexdigest()[:6]
    slug = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw)
    slug = slug.strip("._-") or "site"
    return slug[:80]


def _unlink_files(paths: list[str], enabled: bool) -> tuple[int, int, list[str]]:
    """删除一批文件。返回 (删除数, 释放字节, 失败列表)。

    只删**文件**, 且只在确实存在于项目目录下时才动手 —— 避免把用户自定义输出路径
    之外的意外文件(比如手写的规则文件)删掉。
    """
    if not enabled:
        return 0, 0, []
    removed = 0
    freed = 0
    failed: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            failed.append(f"{path.name}: {exc}")
    return removed, freed, failed


def _remove_url_dir(url: str, enabled: bool) -> tuple[int, int]:
    """删除某个 URL 对应的结果子目录(如果存在)。"""
    if not enabled or not url:
        return 0, 0
    target = PROJECT_ROOT / "data" / "results" / url_slug(url)
    if not target.is_dir():
        return 0, 0
    removed = 0
    freed = 0
    for child in target.rglob("*"):
        if child.is_file():
            try:
                freed += child.stat().st_size
                child.unlink()
                removed += 1
            except OSError:
                continue
    # 自底向上清空目录
    for child in sorted(target.rglob("*"), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        target.rmdir()
    except OSError:
        pass
    return removed, freed


def _now_from_ts(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _columns(items: list[dict[str, Any]], limit: int = 24) -> list[str]:
    """推断结果表的列顺序(按出现频次, 保持首次出现顺序)。"""
    cols: list[str] = []
    for row in items[:200]:
        for key in row:
            if key not in cols:
                cols.append(key)
            if len(cols) >= limit:
                return cols
    return cols


def _trim_report(report: Optional[PageStructureReport]) -> Optional[dict[str, Any]]:
    """裁剪结构报告: 去掉体积最大的简化 DOM 树之外的长字段。"""
    if report is None:
        return None
    data = report.model_dump(mode="json")
    for candidate in data.get("candidate_lists", []):
        html = candidate.get("sample_html") or ""
        candidate["sample_html"] = html[:1200]
    tree = data.get("simplified_tree") or ""
    data["simplified_tree"] = tree[:20000]
    data["simplified_tree_truncated"] = len(tree) > 20000
    return data


def _filter_records(records: list[NetworkRecord], params: dict[str, Any]) -> list[NetworkRecord]:
    """按界面上的筛选条件过滤抓包结果。"""
    import re

    pattern = (params.get("pattern") or "").strip()
    mime = (params.get("mime") or "").strip()
    status = params.get("status")
    has_json = params.get("has_json")
    resource = (params.get("resource") or "").strip()

    out: list[NetworkRecord] = []
    for rec in records:
        if pattern:
            try:
                if not re.search(pattern, rec.url):
                    continue
            except re.error:
                if pattern.lower() not in rec.url.lower():
                    continue
        if mime and mime.lower() not in (rec.mime_type or "").lower():
            continue
        if status not in (None, "", 0) and rec.status != int(status):
            continue
        if has_json and rec.body_json is None:
            continue
        if resource and rec.resource_type != resource:
            continue
        out.append(rec)
    return out
