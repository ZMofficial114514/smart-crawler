"""
Web 任务的状态模型与事件订阅总线。

一个"任务"是用户在界面上发起的一次操作: 抓取 / 结构分析 / 抓包。它可能耗时数十秒
(要启动浏览器、限速等待、AI 生成规则…), 因此这里做三件事:

1. :class:`TaskState` 保存任务的完整生命周期快照(状态/步骤/结果/错误/耗时);
2. :class:`EventBus` 让 HTTP 上传的任务把进度事件**推**给所有 WebSocket 订阅者;
3. :class:`TaskManager` 持有任务注册表, 落盘大结果, 并支持取消。

设计要点: 订阅者用独立的有界队列, 慢客户端只会丢自己的事件, 不会阻塞爬虫。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from loguru import logger

TaskKind = Literal["crawl", "analyze", "requests"]
TaskStatus = Literal["queued", "running", "success", "failed", "cancelled"]

# 单个任务在内存中保留的数据条数上限(完整结果落盘, 界面只预览头部)
PREVIEW_LIMIT = 300
# 任务注册表容量(FIFO 淘汰, 防止长时间运行后内存膨胀)
MAX_TASKS = 60


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TaskStep:
    """任务进度时间线中的一个步骤。"""

    key: str
    label: str
    status: Literal["pending", "running", "done", "error", "skipped"] = "pending"
    detail: str = ""
    at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "status": self.status, "detail": self.detail, "at": self.at}


@dataclass
class TaskState:
    """一个 Web 任务的完整快照(可直接序列化给前端)。"""

    id: str
    kind: TaskKind
    params: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = "queued"
    steps: list[TaskStep] = field(default_factory=list)
    progress: float = 0.0
    message: str = ""
    errors: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: float = 0.0
    # 各类型任务的结果负载(均为 JSON 可序列化结构)
    result: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    _started_perf: float = 0.0

    # -- 步骤控制 --
    def set_steps(self, steps: list[tuple[str, str]]) -> None:
        """初始化步骤时间线([(key, label), ...])。"""
        self.steps = [TaskStep(key=k, label=label) for k, label in steps]

    def step(self, key: str, status: str, detail: str = "") -> None:
        """更新某个步骤的状态并同步总进度。"""
        for s in self.steps:
            if s.key == key:
                s.status = status  # type: ignore[assignment]
                if detail:
                    s.detail = detail
                if status in ("running", "done", "error", "skipped"):
                    s.at = _now_iso()
                break
        done = sum(1 for s in self.steps if s.status in ("done", "skipped"))
        if self.steps:
            self.progress = round(done / len(self.steps), 4)

    def finish(self, status: TaskStatus, message: str = "") -> None:
        self.status = status
        self.finished_at = _now_iso()
        if message:
            self.message = message
        self.duration_ms = round((time.perf_counter() - self._started_perf) * 1000, 1) if self._started_perf else 0.0

        # 结束即进度灌满。曾经只对 success 这样做, 于是失败的快照会带着"跑到一半"
        # 的进度值被缓存下来并回放给后连上的客户端, 界面看着像还在跑。
        self.progress = 1.0
        for s in self.steps:
            if s.status in ("pending", "running"):
                # 未显式标记的步骤: 成功/取消视为已走完, 失败时保持原状以便定位断点
                s.status = "done" if status in ("success", "cancelled") else "skipped"  # type: ignore[assignment]
                s.at = self.finished_at

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "progress": self.progress,
            "message": self.message,
            "errors": self.errors,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "artifacts": self.artifacts,
            # 是否正卡在"等用户回答是否继续滚动"上。
            # 暴露出来有两个用处: 界面刷新后能恢复提示条; 测试/脚本能据此驱动交互。
            "waiting_scroll": getattr(self, "scroll_waiter", None) is not None,
            "scroll_prompt": getattr(self, "scroll_prompt", None),
        }
        if include_result:
            data["result"] = self.result
        return data


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------
class EventBus:
    """任务事件的发布/订阅总线(单任务粒度)。

    **为什么要保留一段历史**: 任务可能在前端把 WebSocket 连上之前就跑完了(小页面
    结构分析只要一两秒), 而 ``publish`` 在没有订阅者时**本来就会把事件丢掉** ——
    于是终态事件永远送不到, 界面卡在"分析中…"不动。这不是理论问题: 本地靶站分析
    约 2 秒结束, 复现率接近 100%。

    因此总线保留最近的 ``history_limit`` 条事件, ``subscribe(replay=True)`` 时先补发。
    建连时"先订阅(带补发)再读快照"的配合, 保证既不会漏事件, 也不会因为快照读到旧
    状态而误判。
    """

    def __init__(self, maxsize: int = 500, history_limit: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._maxsize = maxsize
        self._history_limit = history_limit
        self._history: list[dict[str, Any]] = []

    def subscribe(self, replay: bool = False) -> asyncio.Queue[dict[str, Any]]:
        """新建订阅队列。``replay=True`` 时先把历史事件补进队列(用于迟到的连接)。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        if replay:
            for event in self._history[-self._maxsize :]:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover - maxsize >= 历史上限
                    break
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        """广播事件; 队列满则丢弃该订阅者的最旧事件(宁可丢日志也不阻塞爬虫)。"""
        # 先记历史: 即使此刻一个订阅者都没有, 后连上来的也能补到终态事件
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: -self._history_limit]

        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------
class TaskManager:
    """任务注册表 + 每任务事件总线 + 结果落盘。"""

    def __init__(self, storage_dir: Path) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._order: list[str] = []
        self._buses: dict[str, EventBus] = {}
        self._handles: dict[str, asyncio.Task[Any]] = {}
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    # -- 创建 / 查询 --
    def create(self, kind: TaskKind, params: dict[str, Any]) -> TaskState:
        task = TaskState(id=uuid.uuid4().hex[:10], kind=kind, params=params)
        self._tasks[task.id] = task
        self._buses[task.id] = EventBus()
        self._order.append(task.id)
        self._evict()
        return task

    def _evict(self) -> None:
        while len(self._order) > MAX_TASKS:
            old = self._order.pop(0)
            state = self._tasks.get(old)
            # 只淘汰已结束的任务
            if state and state.status in ("queued", "running"):
                self._order.append(old)
                break
            self._tasks.pop(old, None)
            self._buses.pop(old, None)
            self._handles.pop(old, None)

    def get(self, task_id: str) -> Optional[TaskState]:
        return self._tasks.get(task_id)

    def bus(self, task_id: str) -> Optional[EventBus]:
        return self._buses.get(task_id)

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        """按创建时间倒序列出任务(不含完整结果, 界面用)。"""
        items = [self._tasks[tid] for tid in reversed(self._order) if tid in self._tasks]
        return [t.to_dict(include_result=False) for t in items[:limit]]

    def all_tasks(self) -> list[TaskState]:
        """按创建时间倒序返回**任务对象**本身(需要读 params/artifacts 时用)。"""
        return [self._tasks[tid] for tid in reversed(self._order) if tid in self._tasks]

    def remove(self, task_id: str) -> bool:
        """从注册表里彻底移除一个任务(界面删除历史时用)。"""
        if task_id not in self._tasks:
            return False
        self._tasks.pop(task_id, None)
        self._buses.pop(task_id, None)
        self._handles.pop(task_id, None)
        with contextlib.suppress(ValueError):
            self._order.remove(task_id)
        return True

    def active(self) -> list[dict[str, Any]]:
        return [
            t.to_dict(include_result=False)
            for t in self._tasks.values()
            if t.status in ("queued", "running")
        ]

    # -- 执行句柄(用于取消) --
    def attach_handle(self, task_id: str, handle: asyncio.Task[Any]) -> None:
        self._handles[task_id] = handle

    def cancel(self, task_id: str) -> bool:
        handle = self._handles.get(task_id)
        if handle and not handle.done():
            handle.cancel()
            return True
        return False

    # -- 事件 --
    def emit(self, task_id: str, event: dict[str, Any]) -> None:
        bus = self._buses.get(task_id)
        if bus is not None:
            bus.publish({**event, "task_id": task_id, "ts": time.time()})

    # -- 结果落盘 --
    def persist_result(self, task: TaskState, payload: dict[str, Any]) -> Optional[str]:
        """把大结果写入 data/web_tasks/<task_id>.json, 返回相对路径。"""
        try:
            path = self.storage_dir / f"{task.kind}_{task.id}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            return str(path)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning(f"任务结果落盘失败: {exc}")
            return None
