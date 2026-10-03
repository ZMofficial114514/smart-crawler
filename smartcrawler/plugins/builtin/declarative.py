"""
内置插件: 声明式扩展(默认关闭)。

面向"不想写代码"的用户 —— 把几种常用扩展做成**配置**而不是类:

- ``headers``: 附加请求头;
- ``download_pattern``: 用正则匹配页面/记录里的 URL 并批量下载(万能下载器兜底);
- ``export_csv`` / ``export_jsonl``: 在任务结束时另存一份结果;
- ``add_fields``: 给每条记录补充静态字段(如来源站点、批次号), 便于多任务汇总。

这正好与"Python 单文件插件"形成两条互补路径: 常见需求靠配置解决, 特殊需求才写代码。
"""

from __future__ import annotations

import csv
import json
import re
from typing import Any, Optional

from ...config import PROJECT_ROOT
from ...models import DownloadedFile
from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, guess_extension, safe_filename_from_url

#: 匹配 URL 的正则(用于 download_pattern)
_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]]+", re.IGNORECASE)


class DeclarativePlugin(BasePlugin):
    """用配置(而非代码)完成下载、加请求头、补字段与额外导出(默认关闭)。"""

    id = "declarative"
    name = "声明式扩展"
    description = "不写代码即可配置: 附加请求头 / 正则批量下载 / 补充字段 / 另存结果"
    version = "1.0.0"
    author = "SmartCrawler"
    category = "other"
    tags = ["免代码", "下载", "导出"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {
            "key": "headers",
            "label": "附加请求头",
            "type": "textarea",
            "default": "",
            "description": 'JSON {"Referer":"https://x.com"} 或每行 "Key: Value"',
        },
        {
            "key": "download_pattern",
            "label": "下载 URL 正则",
            "type": "str",
            "default": "",
            "description": r"例如 \.(pdf|zip|docx)$ —— 字段与页面文本中匹配到的 URL 都会被下载",
        },
        {
            "key": "download_subdir",
            "label": "下载保存目录",
            "type": "str",
            "default": "files",
        },
        {
            "key": "download_limit",
            "label": "最多下载数",
            "type": "int",
            "default": 50,
            "min": 1,
            "max": 2000,
        },
        {
            "key": "add_fields",
            "label": "补充字段",
            "type": "json",
            "default": "{}",
            "description": '给每条记录追加固定字段, 如 {"source": "books", "batch": 1}',
        },
        {
            "key": "export_format",
            "label": "额外导出格式",
            "type": "enum",
            "default": "none",
            "options": ["none", "csv", "jsonl"],
            "description": "任务结束时把结果另存到 data/plugin_output/exports/",
        },
    ]

    # ------------------------------------------------------------------
    async def before_navigate(self, ctx: PluginContext) -> None:
        headers = _parse_headers(ctx.config.get("headers"))
        if not headers or ctx.page is None:
            return
        context = getattr(ctx.page, "context", None)
        if context is None:
            return
        try:
            await context.set_extra_http_headers(headers)
        except Exception as exc:  # noqa: BLE001
            ctx.notify("WARNING", f"声明式扩展: 注入请求头失败 {exc}")

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        """补字段 + 按正则下载。"""
        extra = _parse_json(ctx.config.get("add_fields"))
        if isinstance(extra, dict) and extra and items:
            for item in items:
                if isinstance(item, dict):
                    for key, value in extra.items():
                        item.setdefault(key, value)

        pattern = str(ctx.config.get("download_pattern") or "").strip()
        if pattern and items:
            await self._download_matched(ctx, items, pattern)
        return items

    async def on_finish(self, ctx: PluginContext, items: list[dict[str, Any]]):
        """额外导出结果。"""
        fmt = str(ctx.config.get("export_format") or "none").lower()
        if fmt == "none" or not items:
            return items
        target_dir = ctx.output_dir / "exports"
        target_dir.mkdir(parents=True, exist_ok=True)

        import time

        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            if fmt == "csv":
                path = target_dir / f"export_{stamp}.csv"
                columns: list[str] = []
                for row in items:
                    for key in row:
                        if key not in columns:
                            columns.append(key)
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for row in items:
                        writer.writerow(
                            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()}
                        )
            else:
                path = target_dir / f"export_{stamp}.jsonl"
                with path.open("w", encoding="utf-8") as handle:
                    for row in items:
                        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            ctx.notify("SUCCESS", f"声明式扩展: 已额外导出 {len(items)} 条 -> {path.name}")
        except OSError as exc:
            ctx.notify("WARNING", f"声明式扩展: 导出失败 {exc}")
        return items

    async def _download_matched(
        self, ctx: PluginContext, items: list[dict[str, Any]], pattern: str
    ) -> None:
        """在记录值与页面文本里按正则找 URL 并下载。"""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            ctx.notify("WARNING", f"声明式扩展: 下载正则非法 ({exc})")
            return

        limit = int(ctx.config.get("download_limit") or 50)
        found: list[tuple[str, Optional[int]]] = []
        seen: set[str] = set()

        def harvest(text: str, index: Optional[int]) -> None:
            for match in _URL_RE.findall(text):
                if not regex.search(match):
                    continue
                url = absolute_url(ctx.url, match)
                if url and url not in seen:
                    seen.add(url)
                    found.append((url, index))

        for index, item in enumerate(items):
            if isinstance(item, dict):
                harvest(json.dumps(item, ensure_ascii=False, default=str), index)

        if ctx.page is not None:
            try:
                html = await ctx.page.content()
                harvest(html, None)
            except Exception:  # noqa: BLE001
                pass

        if not found:
            ctx.notify("DEBUG", f"声明式扩展: 没有 URL 匹配 {pattern!r}")
            return

        await download_many(
            ctx,
            found[:limit],
            plugin_id=self.id,
            subdir=str(ctx.config.get("download_subdir") or "files"),
            referer=ctx.url,
            concurrency=4,
            max_file_size=100 * 1024 * 1024,
        )


def _parse_json(raw: Any) -> Any:
    """宽松解析 JSON 配置项(兼容已是 dict 的情况与空值)。"""
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _parse_headers(raw: Any) -> dict[str, str]:
    """解析附加请求头(JSON 或每行 Key: Value)。"""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return {}
    parsed = _parse_json(text)
    if isinstance(parsed, dict) and parsed:
        return {str(k): str(v) for k, v in parsed.items()}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip('"').strip("'")
        value = value.strip().strip('"').strip("'").rstrip(",")
        if key:
            out[key] = value
    return out


__all__ = ["DeclarativePlugin"]
