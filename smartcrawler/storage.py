"""
SmartCrawler 存储与输出模块。

Storage 提供统一 save(data, format, path) 接口:
- json   : 缩进 JSON 数组
- jsonl  : JSON Lines(每行一条, 适合追加与流式处理)
- csv    : Excel 友好(utf-8-sig), 嵌套值自动序列化为 JSON 字符串
- sqlite : 零依赖 sqlite3, 按 content_hash 幂等写入(天然去重), 后台线程执行不阻塞事件循环
- webhook: 抓取完成后把结果 POST 到配置的 Webhook URL

扩展点: MySQL/MongoDB 等按需继承 StorageBackend 实现(接口与 save 一致)。
另附增量抓取的状态文件读写(已见内容哈希)。
"""

from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from loguru import logger
from pydantic import BaseModel

from .config import StorageConfig
from .utils import stable_hash


class Storage:
    """统一存储出口。"""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # 统一接口
    # ------------------------------------------------------------------
    async def save(
        self,
        data: Union[list[dict[str, Any]], list[BaseModel]],
        format: Optional[str] = None,
        path: Optional[str] = None,
        table: Optional[str] = None,
        source_url: str = "",
        skip_webhook: bool = False,
    ) -> str:
        """保存数据, 返回实际写入位置(文件路径或 sqlite 描述)。

        data 支持 list[dict] 或 list[Pydantic 模型]。
        format 缺省取配置 storage.default_format; path 缺省按时间自动命名到 output_dir。
        """
        fmt = (format or self.config.default_format).lower()
        rows = _to_rows(data)
        if path is None:
            out_dir = Path(self.config.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = {"json": "json", "jsonl": "jsonl", "csv": "csv"}.get(fmt, "txt")
            path = str(out_dir / f"result_{stamp}.{ext}")

        if fmt == "json":
            await asyncio.to_thread(self._write_json, rows, path)
        elif fmt == "jsonl":
            await asyncio.to_thread(self._write_jsonl, rows, path)
        elif fmt == "csv":
            await asyncio.to_thread(self._write_csv, rows, path)
        elif fmt == "sqlite":
            path = await asyncio.to_thread(
                self._save_sqlite, rows, table or self.config.sqlite_table, path, source_url
            )
        else:
            # MySQL / MongoDB 等预留扩展点
            raise ValueError(
                f"不支持的输出格式: {fmt!r}。可选: json/jsonl/csv/sqlite; "
                "MySQL/MongoDB 请继承 Storage 实现(见 storage.py 顶部说明)"
            )

        logger.info(f"已保存 {len(rows)} 条数据 -> {fmt.upper()}: {path}")
        if not skip_webhook and self.config.webhook_url:
            await self._post_webhook(rows, source_url)
        return path

    # ------------------------------------------------------------------
    # 各格式实现(同步函数, 由 asyncio.to_thread 调用)
    # ------------------------------------------------------------------
    @staticmethod
    def _write_json(rows: list[dict[str, Any]], path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    @staticmethod
    def _write_jsonl(rows: list[dict[str, Any]], path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _write_csv(rows: list[dict[str, Any]], path: str) -> None:
        if not rows:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8-sig")
            return
        # 列 = 所有行键的并集(保持首次出现顺序)
        fieldnames: list[str] = []
        for row in rows:
            for k in row:
                if k not in fieldnames:
                    fieldnames.append(k)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {k: _csv_cell(v) for k, v in row.items()}
                )

    def _save_sqlite(
        self, rows: list[dict[str, Any]], table: str, path: str, source_url: str
    ) -> str:
        """写入 SQLite: (content_hash 唯一) + JSON 全文, 重复数据幂等忽略。"""
        if not path or path == "sqlite":
            path = self.config.sqlite_path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
        try:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT UNIQUE,
                    source_url TEXT,
                    created_at TEXT,
                    data TEXT
                )
                """
            )
            now = datetime.now().isoformat(timespec="seconds")
            for row in rows:
                h = stable_hash(row)
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} (content_hash, source_url, created_at, data) "
                    "VALUES (?, ?, ?, ?)",
                    (h, source_url, now, json.dumps(row, ensure_ascii=False, default=str)),
                )
            conn.commit()
            logger.info(f"SQLite 写入完成: {p} (表 {table}, 共 {len(rows)} 条, 重复自动忽略)")
        finally:
            conn.close()
        return str(p)

    async def _post_webhook(self, rows: list[dict[str, Any]], source_url: str) -> None:
        """把结果推送到 Webhook(尽力而为, 失败仅告警)。"""
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source_url": source_url,
            "count": len(rows),
            "items": rows[:100],  # 推送上限 100 条, 防止超大包
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.config.webhook_url, json=payload)
            logger.info(f"Webhook 推送完成: HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Webhook 推送失败: {exc}")

    # ------------------------------------------------------------------
    # SQLite 查询辅助
    # ------------------------------------------------------------------
    def fetch_sqlite(self, table: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        """读取 SQLite 中最近的抓取结果(调试用)。"""
        conn = sqlite3.connect(str(self.config.sqlite_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                f"SELECT * FROM {table or self.config.sqlite_table} ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning(f"SQLite 查询失败: {exc}")
            return []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 增量抓取状态
# ---------------------------------------------------------------------------
def load_state(path: str) -> dict[str, Any]:
    """读取增量状态文件(不存在/损坏返回空结构)。"""
    p = Path(path)
    if not p.exists():
        return {"seen_hashes": [], "last_run": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(f"状态文件损坏, 将重建: {path}")
        return {"seen_hashes": [], "last_run": None}


def save_state(path: str, state: dict[str, Any]) -> None:
    """写入增量状态文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def update_seen_hashes(state: dict[str, Any], new_hashes: list[str], capacity: int = 20000) -> dict[str, Any]:
    """把本轮内容哈希并入历史(容量受限, 先进先出)。"""
    seen = list(dict.fromkeys(state.get("seen_hashes", []) + new_hashes))
    state["seen_hashes"] = seen[-capacity:]
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    return state


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _to_rows(data: Union[list[dict[str, Any]], list[BaseModel]]) -> list[dict[str, Any]]:
    """Pydantic 模型列表 -> dict 列表(dict 原样通过)。"""
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, BaseModel):
            out.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            out.append(item)
        else:
            out.append({"value": item})
    return out


def _csv_cell(value: Any) -> Any:
    """CSV 单元格序列化: 嵌套结构转 JSON 字符串, 其余原样。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value
