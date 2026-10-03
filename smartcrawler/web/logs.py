"""
实时日志总线 —— 把 loguru 的输出接到 WebSocket 上。

爬虫的进度信息(限速等待、第 N 页提取、AI 生成规则、命中缓存…)本来就以日志形式
产生。与其在核心模块里到处埋回调, 不如在 Web 层挂一个 loguru sink, 把日志变成
一条实时事件流推给浏览器 —— 核心模块保持零改动。

线程安全说明: 日志文件的 handler 开了 ``enqueue=True``(见 utils.setup_logging),
sink 可能在工作线程被调用, 因此所有对订阅者的操作都通过
``loop.call_soon_threadsafe`` 回投到事件循环。
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any, Optional

# 历史缓冲区大小(新客户端连接时先补发最近这些行)
HISTORY_SIZE = 300
# 单条日志消息的截断长度
MAX_MESSAGE = 4000


class LogBus:
    """环形缓冲 + 多订阅者广播的日志总线。"""

    def __init__(self, history: int = HISTORY_SIZE) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sink_id: Optional[int] = None
        self._seq = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录主事件循环(用于跨线程投递)。"""
        self._loop = loop

    def attach(self, level: str = "DEBUG") -> int:
        """把自身注册为 loguru sink。"""
        from loguru import logger

        if self._sink_id is not None:
            return self._sink_id
        self._sink_id = logger.add(self._write, level=level.upper(), format="{message}", enqueue=False)
        return self._sink_id

    def detach(self) -> None:
        from loguru import logger

        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except ValueError:
                pass
            self._sink_id = None

    # ------------------------------------------------------------------
    # 写入(sink 回调, 可能来自任意线程)
    # ------------------------------------------------------------------
    def _write(self, message: Any) -> None:
        record = message.record
        entry = {
            "seq": self._next_seq(),
            "time": record["time"].strftime("%H:%M:%S.%f")[:-3],
            "level": record["level"].name,
            "module": record["name"].split(".")[-1],
            "message": str(record["message"])[:MAX_MESSAGE],
        }
        self._history.append(entry)
        self._dispatch(entry)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _dispatch(self, entry: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            self._push(entry)
            return
        try:
            loop.call_soon_threadsafe(self._push, entry)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _push(self, entry: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(entry)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self, replay: bool = True) -> asyncio.Queue[dict[str, Any]]:
        """订阅日志流; replay=True 时先补发历史(界面打开即见上下文)。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        if replay:
            for entry in list(self._history)[-120:]:
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    break
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]

    def inject(self, level: str, message: str, module: str = "web") -> None:
        """绕过 loguru 直接投递一条系统消息(如"任务已取消")。"""
        entry = {
            "seq": self._next_seq(),
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "level": level.upper(),
            "module": module,
            "message": message[:MAX_MESSAGE],
        }
        self._history.append(entry)
        self._dispatch(entry)
