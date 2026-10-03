"""
媒体下载的公共实现(图片 / 音频等插件共用)。

几个来自实战的取舍:

- **并发下载**: 用 ``asyncio.Semaphore`` 限流, 默认 6 路。太激进会被目标站点限速或
  直接封 IP, 毕竟我们默认还遵守 robots.txt 与 1~3 秒限速。
- **必须带 Referer**: 大量图床/资源站对没有 Referer 的请求返回 403 或占位图。
- **先 HEAD 再 GET 不划算**: 很多站点不支持 HEAD。改为直接 GET 并在流式读取时
  按 ``max_file_size`` 截断, 超限即放弃并删除半成品。
- **扩展名判定顺序**: Content-Type -> URL 路径 -> 兜底 ``.bin``。反过来会踩到
  ``/image?id=123`` 这类无扩展名的 URL。
- **落盘去重**: 同一 URL 只下一次(按 URL 哈希命名), 避免一页里重复引用同一张图
  造成几十次冗余请求。
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from ...models import DownloadedFile
from ..base import PluginContext

#: Content-Type -> 扩展名 的补充映射(标准库 mimetypes 对部分类型识别不准)
_EXTRA_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".weba",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
}


def safe_filename_from_url(url: str, fallback_ext: str = ".bin") -> str:
    """从 URL 推断文件名; 无扩展名时用 URL 哈希 + 兜底扩展名。"""
    parsed = urlparse(url)
    name = Path(parsed.path).name
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._")

    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]

    if not name:
        return f"{digest}{fallback_ext}"
    # 已经有像样的扩展名就直接用
    if re.search(r"\.[A-Za-z0-9]{1,5}$", name):
        stem = Path(name).stem[:60]
        ext = Path(name).suffix.lower()
        return f"{stem}_{digest}{ext}"
    return f"{name[:60]}_{digest}{fallback_ext}"


def guess_extension(content_type: str, url: str, default: str = ".bin") -> str:
    """按 Content-Type -> URL 的顺序推断扩展名。"""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _EXTRA_TYPES:
        return _EXTRA_TYPES[ctype]
    if ctype:
        guessed = mimetypes.guess_extension(ctype)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix or ""):
        return suffix
    return default


def absolute_url(base: str, candidate: str) -> str:
    """把可能是相对路径的资源地址补全为绝对 URL。"""
    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    if candidate.startswith("//"):
        return f"{urlparse(base).scheme or 'https'}:{candidate}"
    if candidate.startswith("data:"):
        return ""  # 内联 data URI 不下载(体积不可控且多是占位符)
    try:
        return urljoin(base, candidate)
    except (ValueError, TypeError):
        return ""


async def download_many(
    ctx: PluginContext,
    urls: Iterable[tuple[str, Optional[int]]],
    *,
    plugin_id: str,
    subdir: str,
    referer: str = "",
    max_file_size: int = 20 * 1024 * 1024,
    concurrency: int = 6,
    timeout: float = 30.0,
    allowed_types: Optional[tuple[str, ...]] = None,
    filename_prefix: str = "",
) -> list[DownloadedFile]:
    """并发下载一批资源并登记产物。

    :param urls: ``[(绝对URL, 来源记录下标或 None), ...]``
    :param allowed_types: 只允许这些 Content-Type 前缀(如 ``("image/",)``); 为空则放行
    :returns: 下载结果列表(同时也写入了 ``ctx.downloads``)
    """
    if not urls:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[DownloadedFile] = []
    lock = asyncio.Lock()
    headers = {
        "User-Agent": ctx.settings.browser.user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer

    async def one(client: httpx.AsyncClient, url: str, index: Optional[int]) -> None:
        async with sem:
            record = DownloadedFile(url=url, plugin_id=plugin_id, source_item_index=index)
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    record.mime_type = response.headers.get("Content-Type", "")
                    if response.status_code >= 400:
                        record.ok = False
                        record.error = f"HTTP {response.status_code}"
                        return
                    if allowed_types and not any(
                        record.mime_type.lower().startswith(t) for t in allowed_types
                    ):
                        record.ok = False
                        record.error = f"类型不符: {record.mime_type or '未知'}"
                        return

                    ext = guess_extension(record.mime_type, url)
                    filename = f"{filename_prefix}{safe_filename_from_url(url, ext)}"
                    if not Path(filename).suffix:
                        filename += ext
                    target = ctx.download_path(subdir, filename)

                    size = 0
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > max_file_size:
                                raise ValueError(f"超过大小上限 {max_file_size // 1024 // 1024}MB")
                            handle.write(chunk)

                    record.path = str(target)
                    record.relative_path = _relative(target)
                    record.filename = target.name
                    record.size = size
                    record.ok = True
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其他下载
                record.ok = False
                record.error = f"{type(exc).__name__}: {exc}"
                # 清理可能留下的半成品文件
                if record.path:
                    try:
                        Path(record.path).unlink(missing_ok=True)
                    except OSError:
                        pass
                    record.path = ""
                    record.size = 0
            finally:
                async with lock:
                    results.append(record)
                    ctx.downloads.append(record)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=timeout), follow_redirects=True
    ) as client:
        await asyncio.gather(*(one(client, url, idx) for url, idx in urls))

    ok = sum(1 for r in results if r.ok)
    if ok:
        logger.info(f"[{plugin_id}] 下载完成: 成功 {ok} / 共 {len(results)} 个文件 -> {ctx.output_dir / subdir}")
        ctx.notify("INFO", f"插件 {plugin_id}: 已下载 {ok}/{len(results)} 个文件")
    else:
        logger.warning(f"[{plugin_id}] 全部下载失败({len(results)} 个), 详见插件错误")
    return results


def _relative(path: Path) -> str:
    """转成相对项目根目录的路径, 便于界面显示。"""
    from ...config import PROJECT_ROOT

    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 从提取结果中挑出资源 URL
# ---------------------------------------------------------------------------
def urls_from_items(
    items: list[dict[str, Any]],
    field: str,
    allowed_ext: tuple[str, ...] = (),
) -> list[tuple[str, Optional[int]]]:
    """从提取出的记录里收集某个字段的 URL。

    支持字段值是列表(一个条目多张图)以及分号/逗号分隔的字符串。
    """
    out: list[tuple[str, Optional[int]]] = []
    seen: set[str] = set()
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        value = item.get(field)
        if value is None:
            continue
        candidates: list[str] = []
        if isinstance(value, (list, tuple, set)):
            candidates = [str(v) for v in value]
        elif isinstance(value, str):
            candidates = [p.strip() for p in re.split(r"[;,]", value)]
        else:
            continue
        for candidate in candidates:
            if not candidate:
                continue
            if allowed_ext and not urlparse(candidate).path.lower().endswith(allowed_ext):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            out.append((candidate, index))
    return out


async def urls_from_dom(
    page,
    selector: str,
    attribute: str = "src",
    *,
    limit: int = 500,
) -> list[str]:
    """用 CSS 选择器从当前 DOM 抓取资源 URL(用于页面级下载, 不依赖提取字段)。"""
    if page is None or not selector:
        return []
    try:
        found = await page.evaluate(
            """([sel, attr, limit]) => {
                const out = [];
                for (const el of document.querySelectorAll(sel)) {
                    let v = attr === 'srcset'
                        ? (el.getAttribute('srcset') || '').split(',')[0].trim().split(' ')[0]
                        : el.getAttribute(attr);
                    if (!v && attr === 'src') v = el.getAttribute('data-src') || el.getAttribute('data-original');
                    if (v && !out.includes(v)) out.push(v);
                    if (out.length >= limit) break;
                }
                return out;
            }""",
            [selector, attribute, limit],
        )
        return [str(u) for u in (found or []) if u]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"DOM 资源选择器执行失败 {selector!r}: {exc}")
        return []
