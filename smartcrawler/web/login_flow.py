"""
手动登录流程 —— 让用户在有头浏览器里自己登录一次, 然后把会话保存下来复用。

设计要点
--------
1. **浏览器跑在独立子进程里**。Playwright 的浏览器对象绑定在创建它的 asyncio 事件循环上,
   无法"这个 HTTP 请求打开浏览器、下一个请求继续用"。所以子进程
   (``scripts/login_helper.py``) 负责打开可见浏览器并等用户操作, 主服务只做两件事:
   读状态文件展示进度、写信号文件下达"保存/取消"。
2. **框架不接触用户密码**。用户在真实浏览器窗口里输入凭据, 框架只保存服务端签发的
   Cookie 与 localStorage。验证码/短信/扫码这些也只有人能过。
3. **主服务与辅助进程用文件通信**(``.runtmp/login_flow/``)。比管道简单, 而且主服务
   重启后仍能读到上一次的最终状态。
4. 一次只允许一个登录流程 —— 多个可见浏览器窗口会让用户困惑, 也会争用同一个会话文件。

会话文件保存后, 主服务的两个爬虫实例会被标记为"需要重建", 下次任务即带上新会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..config import PROJECT_ROOT
from ..session import (
    DEFAULT_SESSION_PATH,
    delete_session,
    merge_storage_state,
    resolve_session_path,
    save_storage_state,
    summarize_session,
)

#: 与 login_helper.py 约定的通信目录
FLOW_DIR = PROJECT_ROOT / ".runtmp" / "login_flow"
STATUS_FILE = FLOW_DIR / "status.json"
CONFIRM_FILE = FLOW_DIR / "confirm"
CANCEL_FILE = FLOW_DIR / "cancel"
STATE_FILE = FLOW_DIR / "state.json"

HELPER = PROJECT_ROOT / "scripts" / "login_helper.py"

#: 状态文件超过这个秒数没更新, 就认为辅助进程已经死了(避免界面一直转圈)
STALE_SECONDS = 25.0


class LoginFlow:
    """一次手动登录流程的状态机(由 CrawlService 持有)。"""

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.url: str = ""
        self.session_path: Path = DEFAULT_SESSION_PATH
        self.started_at: float = 0.0
        #: "login" = 等用户登录; "challenge" = 等用户过人机验证
        self.mode: str = "login"
        self._log_file: Optional[Any] = None

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def read_status(self) -> dict[str, Any]:
        """读取辅助进程写入的进度(带"是否已过期"判断)。"""
        data: dict[str, Any] = {}
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        updated_at = float(data.get("updated_at") or 0.0)
        data["stale"] = bool(updated_at) and (time.time() - updated_at) > STALE_SECONDS
        data["alive"] = self.running
        data["url"] = data.get("url") or self.url
        data["mode"] = data.get("mode") or self.mode
        data["session_path"] = str(self.session_path)
        data["started_at"] = self.started_at or None
        if self.process is not None and self.process.poll() is not None:
            data["exit_code"] = self.process.returncode
        return data

    # ------------------------------------------------------------------
    def start(
        self,
        url: str,
        session_path: Optional[Path] = None,
        pre_auth_url: Optional[str] = None,
        mode: str = "login",
    ) -> dict[str, Any]:
        """启动辅助进程(立即返回, 不阻塞 HTTP 请求)。

        ``mode``: ``login`` = 等用户登录; ``challenge`` = 等用户完成人机验证。

        ``pre_auth_url`` 仅供验收测试使用 —— 让辅助窗口在打开目标站点前先访问一个
        入口以获得会话 Cookie, 用来代替"用户在窗口里手动登录"这一步。
        """
        if self.running:
            raise RuntimeError("已有登录/验证流程在进行中, 请先完成或取消它")

        if not HELPER.exists():
            raise RuntimeError(f"缺少登录辅助脚本: {HELPER}")

        self.url = url
        self.mode = mode if mode in ("login", "challenge") else "login"
        self.session_path = Path(session_path) if session_path else DEFAULT_SESSION_PATH
        self.started_at = time.time()

        # 清掉上一次的信号与状态, 避免被误读
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        for path in (STATUS_FILE, CONFIRM_FILE, CANCEL_FILE, STATE_FILE):
            with contextlib.suppress(OSError):
                path.unlink()

        # 辅助进程的输出留一份日志, 出问题时能查。
        # 用行缓冲 + UTF-8: 默认全缓冲在进程被强杀时会把日志整段丢掉, 那样出错时
        # 反而什么都看不到(这正是"日志文件是空的"那次踩到的坑)。
        log_path = FLOW_DIR / "helper.log"
        self._log_file = log_path.open("wb", buffering=1)
        creationflags = 0
        if sys.platform == "win32":
            # 独立进程组: 主服务退出/重启时不连带杀掉用户的登录窗口
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        command = [
            sys.executable, "-u", str(HELPER),
            "--url", url,
            "--out", str(self.session_path),
            "--mode", self.mode,
        ]
        if pre_auth_url:
            command += ["--pre-auth-url", pre_auth_url]

        self.process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        logger.info(f"已启动手动登录流程: {url} -> {self.session_path} (pid={self.process.pid})")
        return self.read_status()

    def helper_log_tail(self, max_chars: int = 1200) -> str:
        """读辅助进程日志的尾部(界面在报错时展示, 便于自查)。"""
        log_path = FLOW_DIR / "helper.log"
        if not log_path.exists():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max_chars:]

    def confirm(self) -> bool:
        """通知辅助进程"用户已登录, 保存会话"。"""
        if not self.running:
            return False
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        CONFIRM_FILE.write_text(str(time.time()), encoding="utf-8")
        logger.info("已请求保存登录会话")
        return True

    def cancel(self) -> bool:
        """通知辅助进程取消并关闭浏览器窗口。"""
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        CANCEL_FILE.write_text(str(time.time()), encoding="utf-8")
        if self.process is not None and self.process.poll() is None:
            # 给它 3 秒自己收尾(关浏览器), 之后强杀
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    self.process.kill()
        self._cleanup()
        return True

    def collect(self) -> dict[str, Any]:
        """把辅助进程写好的会话吸收进正式会话文件(幂等)。

        **触发条件只看 `state.json` 是否存在, 不看进程是否退出** —— 这是踩过的坑:
        辅助进程写完 `state.json` 与"已保存"状态后还会停留 2 秒才退出, 而前端一看到
        "已保存"就会停止轮询、关掉弹窗。若收集逻辑挂在"进程已退出"上, 这两秒里的收集
        永远不会发生 —— 会话文件写不出来, 界面却显示保存成功, 用户随后重新分析时
        会话自然不生效。`state.json` 一旦出现就说明数据已经写完了, 此刻收集是安全的。
        """
        saved = False
        merged_cookies = 0
        merged_origins = 0

        if STATE_FILE.exists():
            try:
                incoming = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                base = None
                if self.session_path.exists():
                    with contextlib.suppress(json.JSONDecodeError, OSError):
                        base = json.loads(self.session_path.read_text(encoding="utf-8"))
                merged = merge_storage_state(base, incoming)
                save_storage_state(merged, self.session_path)
                merged_cookies = len(merged.get("cookies") or [])
                merged_origins = len(merged.get("origins") or [])
                saved = merged_cookies > 0 or merged_origins > 0
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"合并登录会话失败: {exc}")

        status = self.read_status()
        status["saved"] = saved
        status["merged_cookies"] = merged_cookies
        status["merged_origins"] = merged_origins
        # 带上辅助进程日志尾部: 出错时用户/开发者不用去翻文件就知道发生了什么
        if not saved or status.get("state") == "error":
            status["helper_log"] = self.helper_log_tail()
        return status

    def verify_saved(self) -> dict[str, Any]:
        """确认会话文件是否**真的**已经落盘可用。

        为什么要单独确认: 界面上的"已保存"来自辅助进程写的状态文件, 与会话文件是否
        真的写出是两件事。如果只信前者, 就可能出现"界面说保存成功、实际文件没写出来"
        的情况 —— 用户随后重新分析发现会话没生效, 却不知道问题出在哪。
        """
        summary = summarize_session(self.session_path)
        return {
            "session_saved": bool(summary.exists and (summary.cookies or summary.origins)),
            "session_path": str(self.session_path),
            "cookies": summary.cookies,
            "origins": summary.origins,
            "domains": summary.domains,
            "saved_at": summary.saved_at,
        }

    def saved_status(self, **extra: Any) -> dict[str, Any]:
        """构造一个权威的"会话已保存"状态(不依赖辅助进程写了什么)。"""
        verified = self.verify_saved()
        return {
            "state": "saved",
            "step": "会话已保存, 后续抓取会自动带上登录态",
            "url": self.url,
            "session_path": str(self.session_path),
            "alive": self.running,
            **verified,
            **extra,
        }

    def _cleanup(self) -> None:
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.close()
            self._log_file = None

    def reset(self) -> None:
        """清空流程状态(保留会话文件本身)。"""
        self._cleanup()
        self.process = None
        self.started_at = 0.0
        for path in (STATUS_FILE, CONFIRM_FILE, CANCEL_FILE, STATE_FILE):
            with contextlib.suppress(OSError):
                path.unlink()


# ---------------------------------------------------------------------------
# 供 CrawlService 组合使用的辅助函数
# ---------------------------------------------------------------------------
def session_overview(configured_path: str = "") -> dict[str, Any]:
    """会话总览(用于界面显示"是否已保存登录态")。"""
    path = resolve_session_path(configured_path)
    summary = summarize_session(path)
    return summary.to_dict()


def clear_saved_session(configured_path: str = "") -> dict[str, Any]:
    """删除已保存的会话(退出登录)。"""
    path = resolve_session_path(configured_path)
    removed = delete_session(path)
    return {"removed": removed, "path": str(path)}


async def ensure_flow_dir() -> None:
    """确保通信目录存在(启动时调用一次)。"""
    await asyncio.to_thread(FLOW_DIR.mkdir, parents=True, exist_ok=True)
