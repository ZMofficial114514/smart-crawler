"""
SmartCrawler 网络监听模块。

NetworkRecorder 挂载到 Playwright Page 上, 监听:
- request / response / requestfinished / requestfailed 事件
- websocket 连接及其收发帧

核心逻辑:
1. 只捕获配置关心的资源类型(默认 xhr/fetch/websocket), 排除 image/css/font/script;
2. 请求阶段记录 URL/方法/头/POST 数据/页面与 frame 来源;
3. 响应阶段异步抓取响应体(带超时与大小上限), JSON 自动解析, 文本保留原文;
4. 结束阶段计算耗时, 落入内存列表, 可选 JSON Lines 落盘;
5. 提供按 URL 正则 / MIME / 状态码 / 是否含 JSON 的查询接口。

注意: Playwright 事件回调内不能直接 await, 响应体捕获通过 asyncio.create_task 完成。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from playwright.async_api import Page, Request, Response, WebSocket

from .config import Settings
from .models import NetworkRecord, WebSocketFrame, WebSocketRecord
from .utils import now_ts, safe_json_loads, truncate


class NetworkRecorder:
    """网络请求/响应/WS 捕获器。一个爬虫任务通常持有一个实例, 可挂载多个页面。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.records: list[NetworkRecord] = []
        self.ws_records: list[WebSocketRecord] = []
        # request 对象 -> (元数据 dict, NetworkRecord 或 None)
        self._pending: dict[int, dict[str, Any]] = {}
        self._background_tasks: set[asyncio.Task] = set()  # 持有引用防 GC
        self._attached_pages: set[int] = set()

    # ------------------------------------------------------------------
    # 挂载 / 卸载
    # ------------------------------------------------------------------
    def attach(self, page: Page) -> None:
        """把监听器挂到页面上(幂等)。"""
        if id(page) in self._attached_pages:
            return
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)
        if self.settings.network.ws_capture_frames:
            page.on("websocket", self._on_websocket)
        self._attached_pages.add(id(page))
        logger.debug(f"网络监听已挂载: {page.url}")

    def detach(self, page: Page) -> None:
        """卸载监听器。"""
        try:
            page.remove_listener("request", self._on_request)
            page.remove_listener("response", self._on_response)
            page.remove_listener("requestfinished", self._on_finished)
            page.remove_listener("requestfailed", self._on_failed)
            page.remove_listener("websocket", self._on_websocket)
        except Exception:  # noqa: BLE001
            pass
        self._attached_pages.discard(id(page))

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _should_capture(self, request: Request) -> bool:
        """资源类型过滤 + data: URL 过滤。"""
        if request.url.startswith(("data:", "blob:")):
            return False
        return request.resource_type in self.settings.network.capture_resource_types

    def _on_request(self, request: Request) -> None:
        """request 事件: 记录请求侧元数据。"""
        if not self._should_capture(request):
            return
        page_url, frame_url = "", ""
        try:
            frame_url = request.frame.url
            page_url = request.frame.page.url
        except Exception:  # noqa: BLE001 - 页面可能已销毁
            pass
        self._pending[id(request)] = {
            "request": request,
            "timestamp": now_ts(),
            "record": NetworkRecord(
                id=uuid.uuid4().hex[:12],
                timestamp=now_ts(),
                url=request.url,
                method=request.method,
                resource_type=request.resource_type,
                request_headers=_headers_to_dict(request.headers),
                post_data=_safe_post_data(request),
                page_url=page_url,
                frame_url=frame_url,
            ),
        }

    def _on_response(self, response: Response) -> None:
        """response 事件: 记录响应侧元数据, 并异步抓取响应体。"""
        request = response.request
        entry = self._pending.get(id(request))
        if entry is None:
            return  # 不在捕获范围
        record: NetworkRecord = entry["record"]
        try:
            record.status = response.status
            record.response_headers = _headers_to_dict(response.headers)
            record.mime_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        except Exception:  # noqa: BLE001
            pass
        self._append_record(record)
        task = asyncio.create_task(self._capture_body(response, record))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _on_finished(self, request: Request) -> None:
        """requestfinished 事件: 计算耗时并落盘(JSON Lines 可选)。"""
        entry = self._pending.pop(id(request), None)
        if entry is None:
            return
        record: NetworkRecord = entry["record"]
        record.duration_ms = round((now_ts() - entry["timestamp"]) * 1000, 2)
        if self.settings.network.dump_jsonl:
            self._dump_line(record)

    def _on_failed(self, request: Request) -> None:
        """requestfailed 事件: 记录失败请求(错误文本在 request.failure 属性)。"""
        entry = self._pending.pop(id(request), None)
        if entry is None:
            return
        record: NetworkRecord = entry["record"]
        record.failed = True
        try:
            record.error = str(request.failure or "unknown")[:500]
        except Exception:  # noqa: BLE001
            record.error = "unknown"
        record.duration_ms = round((now_ts() - entry["timestamp"]) * 1000, 2)
        logger.debug(f"请求失败: {record.url} ({record.error})")

    async def _capture_body(self, response: Response, record: NetworkRecord) -> None:
        """异步抓取响应体: 超时/大小受控, JSON 自动解析。"""
        cfg = self.settings.network
        try:
            # 大响应体跳过(按 Content-Length 预判)
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > cfg.max_body_size:
                record.body_text = ""
                logger.debug(f"响应体过大跳过: {record.url}")
                return
            body = await asyncio.wait_for(
                response.text(), timeout=cfg.body_capture_timeout
            )
            record.body_text = truncate(body, cfg.max_body_size)
            # JSON 自动解析: MIME 为 json, 或文本以 { [ 开头
            mime = record.mime_type.lower()
            stripped = body.lstrip()[:1]
            if "json" in mime or (mime in ("", "text/plain") and stripped in ("{", "[")):
                record.body_json = safe_json_loads(body)
        except asyncio.TimeoutError:
            logger.debug(f"响应体捕获超时: {record.url}")
        except Exception as exc:  # noqa: BLE001 - 重定向/二进制/已销毁等均属正常
            logger.debug(f"响应体捕获失败({type(exc).__name__}): {record.url} -> {exc}")

    def _on_websocket(self, ws: WebSocket) -> None:
        """websocket 事件: 记录连接与全部收发帧。"""
        record = WebSocketRecord(url=ws.url)
        try:
            record.page_url = ws.page.url
        except Exception:  # noqa: BLE001
            pass
        self.ws_records.append(record)
        logger.debug(f"捕获 WebSocket 连接: {ws.url}")

        def _frame(direction: str) -> Any:
            def handler(payload: Any) -> None:
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
                record.frames.append(
                    WebSocketFrame(
                        direction=direction,  # type: ignore[arg-type]
                        payload=truncate(str(payload), self.settings.network.max_body_size),
                        timestamp=now_ts(),
                    )
                )
            return handler

        ws.on("framesent", _frame("send"))
        ws.on("framereceived", _frame("recv"))
        ws.on("close", lambda: setattr(record, "closed", True))

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _append_record(self, record: NetworkRecord) -> None:
        """加入内存列表, 超出容量时丢弃最旧的。"""
        self.records.append(record)
        if len(self.records) > self.settings.network.max_records:
            self.records.pop(0)

    def _dump_line(self, record: NetworkRecord) -> None:
        """单条记录追加写入 JSON Lines 文件。"""
        try:
            path = Path(self.settings.network.jsonl_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(record.model_dump_json(exclude_none=True) + "\n")
        except OSError as exc:
            logger.warning(f"JSONL 落盘失败: {exc}")

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def query(
        self,
        url_pattern: Optional[str] = None,
        mime_contains: Optional[str] = None,
        status: Optional[int] = None,
        has_json: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> list[NetworkRecord]:
        """按条件过滤捕获记录: URL 正则 / MIME 包含 / 状态码 / 是否含 JSON。"""
        out: list[NetworkRecord] = []
        for r in reversed(self.records):  # 新记录优先
            if not r.matches(url_pattern=url_pattern, mime_contains=mime_contains, status=status):
                continue
            if has_json is True and r.body_json is None:
                continue
            if has_json is False and r.body_json is not None:
                continue
            out.append(r)
            if limit is not None and len(out) >= limit:
                break
        return out

    def json_records(self) -> list[NetworkRecord]:
        """所有响应体可解析为 JSON 的记录(json 模式提取的数据源)。"""
        return [r for r in self.records if r.body_json is not None]

    def stats(self) -> dict[str, Any]:
        """捕获统计摘要。"""
        from collections import Counter

        mime_counter = Counter(r.mime_type or "unknown" for r in self.records)
        return {
            "total": len(self.records),
            "with_json": sum(1 for r in self.records if r.body_json is not None),
            "failed": sum(1 for r in self.records if r.failed),
            "websockets": len(self.ws_records),
            "by_mime": dict(mime_counter.most_common(10)),
        }

    def to_jsonl(self, path: str) -> str:
        """把全部记录写入 JSON Lines 文件, 返回文件路径。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for r in self.records:
                f.write(r.model_dump_json(exclude_none=True) + "\n")
        logger.info(f"已导出 {len(self.records)} 条网络记录 -> {p}")
        return str(p)

    def clear(self) -> None:
        """清空内存中的记录(开始新任务前调用)。"""
        self.records.clear()
        self.ws_records.clear()
        self._pending.clear()


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------
def _headers_to_dict(headers: Any) -> dict[str, str]:
    """Playwright headers 可能为 dict 或 Headers 对象, 统一转 dict[str, str]。"""
    try:
        return {str(k): str(v) for k, v in dict(headers).items()}
    except Exception:  # noqa: BLE001
        return {}


def _safe_post_data(request: Request) -> Optional[str]:
    """安全读取 POST 数据(可能抛异常, 截断存储)。"""
    try:
        data = request.post_data
        if data is None:
            return None
        return truncate(data, 100_000)
    except Exception:  # noqa: BLE001
        return None
