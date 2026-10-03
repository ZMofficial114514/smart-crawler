"""
WebSocket 实时通道。

两个端点:
- ``/ws/logs``            实时日志流(全局, 后台任务也会推送到这里);
- ``/ws/tasks/{task_id}`` 单个任务的进度事件流, 建连时先补发一次任务快照,
  这样即使客户端在任务开始后才连上, 界面也能立刻渲染正确状态。

心跳: 每 20 秒发一个 ``ping``, 防止反向代理掐断空闲连接, 也让前端能据此判断链路健康。
"""

from __future__ import annotations

import asyncio
import contextlib
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

# 注意: 本文件位于 smartcrawler/web/ 下, 所以指向同包内的 service 只需一个点。
# 写成 `..service` 会解析成 smartcrawler.service(不存在), 触发 ModuleNotFoundError。
# get_service 定义在 routes 子包里(唯一依赖入口), 因此要从 .routes 导入。
from .service import CrawlService
from .routes import get_service

router = APIRouter()

HEARTBEAT_SECONDS = 20.0


def _resolve_service(websocket: WebSocket) -> CrawlService:
    """WebSocket 端点没有 Request 对象, 手动取一次服务实例。"""
    return get_service(websocket)  # type: ignore[arg-type]


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """实时日志流。"""
    service = _resolve_service(websocket)
    await websocket.accept()
    bus = websocket.app.state.log_bus
    queue = bus.subscribe(replay=True)
    await websocket.send_json({"type": "hello", "channel": "logs"})

    async def pump() -> None:
        while True:
            entry = await queue.get()
            await websocket.send_json({"type": "log", **entry})

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await websocket.send_json({"type": "ping", "active_tasks": len(service.tasks.active())})

    tasks = [asyncio.create_task(pump()), asyncio.create_task(keepalive())]
    try:
        # 客户端消息仅用于保活/订阅(当前无需处理具体内容)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"日志 WebSocket 断开: {exc}")
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        bus.unsubscribe(queue)


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str) -> None:
    """单任务进度事件流。"""
    service = _resolve_service(websocket)
    await websocket.accept()

    task = service.tasks.get(task_id)
    if task is None:
        await websocket.send_json({"type": "error", "message": "任务不存在"})
        await websocket.close(code=4004)
        return

    bus = service.tasks.bus(task_id)
    # **先订阅(带历史补发)再发快照**: 任务是异步跑的, 快照构造与 send_json 之间都有
    # await —— 任务完全可能在这个间隙里结束并广播 result。先订阅就不会漏; 带补发则
    # 连"订阅之前就已经广播过"的事件也能收到。
    queue = bus.subscribe(replay=True) if bus else None
    # 建连即补发快照, 前端不必额外拉一次 /api/tasks/{id}
    await websocket.send_json({"type": "snapshot", "task": task.to_dict()})

    if queue is None:
        await websocket.close(code=4005)
        return

    async def pump() -> None:
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    async def keepalive() -> None:
        """心跳: 同时附带状态与进度作为兜底。

        若某次终态事件在网络上丢失, 前端最多 20 秒后也能靠这里纠正标签与进度条,
        不至于永远停在"进行中"。字段与 snapshot/status 保持一致, 前端可复用同一套
        状态渲染逻辑。
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            snapshot = service.tasks.get(task_id)
            if snapshot is None:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(
                {
                    "type": "ping",
                    "status": snapshot.status,
                    "progress": snapshot.progress,
                }
            )

    tasks = [asyncio.create_task(pump()), asyncio.create_task(keepalive())]
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"任务 WebSocket 断开: {exc}")
    finally:
        for task_handle in tasks:
            task_handle.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        bus.unsubscribe(queue)  # type: ignore[union-attr]


@router.websocket("/ws/health")
async def ws_health(websocket: WebSocket) -> None:
    """低频状态推送: 供顶部状态栏实时显示 AI/浏览器/任务概况。"""
    service = _resolve_service(websocket)
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "health", "data": service.health()})
            await asyncio.sleep(8.0)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"状态 WebSocket 关闭: {exc}")


__all__ = ["router"]
