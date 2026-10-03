"""
系统信息与运维接口: 健康状态、AI/代理自检、缓存与状态维护、文件下载。

统一约定: 所有响应体都是 JSON 对象; 需要服务实例的接口通过 ``Depends(get_service)``
注入, 依赖定义在 ``routes/__init__.py`` 以避免循环导入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger

from ...config import PROJECT_ROOT, Settings, get_settings
from ..service import CrawlService
from . import get_service

router = APIRouter()


@router.get("/health", summary="服务与依赖健康状态")
async def health(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.health()


@router.post("/ai/test", summary="实测 AI 连通性")
async def test_ai(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.test_ai()


@router.post("/proxies/test", summary="探测代理池可用性")
async def test_proxies(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.test_proxies()


@router.post("/cache/clear", summary="清空 AI 响应缓存")
async def clear_cache(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.clear_ai_cache()


@router.post("/shutdown", summary="关闭服务(并清理框架子进程)")
async def shutdown(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    """让界面上的"关闭服务"按钮能真的把服务停掉。

    比让用户去任务管理器杀进程友好得多, 而且**会顺手清掉 Playwright 的浏览器进程** ——
    否则服务停了、浏览器还在后台跑, 下次启动就会撞端口。
    """
    from ...runtime import kill_our_processes, shutdown_controller

    # **只清自己这一支**: 同一个项目可能同时跑着多个服务实例(比如另一个端口上的
    # 调试实例), 无差别清理会把别人的浏览器一起杀掉。root_pid 限定"进程链上挂着本
    # 服务"的进程, 从而只影响自己。
    #
    # 顺序也重要: 先扫再收。若先 await service.shutdown(), 爬虫会优雅地自己关掉浏览器,
    # 之后再去"找残留"就什么都找不到, killed_processes 会永远是 0。
    import os as _os

    mine = _os.getpid()
    killed = kill_our_processes(reason="界面请求关闭服务", root_pid=mine)

    try:
        await service.shutdown()
    except Exception as exc:  # noqa: BLE001 - 收尾失败也要让退出继续
        logger.warning(f"关闭服务时清理爬虫失败: {exc}")

    accepted = shutdown_controller.request()
    total_killed = killed + kill_our_processes(reason="关闭服务(补扫)", root_pid=mine)
    return {
        "ok": True,
        "accepted": accepted,
        "killed_processes": total_killed,
        "message": "服务正在关闭, 框架子进程已清理" if accepted else "关闭请求此前已提交",
    }


@router.post("/state/reset", summary="重置增量抓取状态")
async def reset_state(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.reset_incremental_state()


# ---------------------------------------------------------------------------
# 文件浏览与下载
# ---------------------------------------------------------------------------
def _allowed_roots() -> list[Path]:
    """允许访问的目录白名单。

    文件下载接口接收路径参数, 必须限制在白名单内, 否则就是一个任意文件读取漏洞。
    """
    roots = [PROJECT_ROOT / "data", PROJECT_ROOT / "logs"]
    settings: Settings = get_settings()
    out = Path(settings.storage.output_dir)
    roots.append(out if out.is_absolute() else PROJECT_ROOT / out)
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:  # pragma: no cover
            continue
    return resolved


def _safe_path(raw: str) -> Path:
    """把用户传入的路径解析到白名单目录内, 越界即拒绝。"""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"路径无法解析: {exc}") from exc

    for root in _allowed_roots():
        if resolved == root or root in resolved.parents:
            if resolved.is_file():
                return resolved
            raise HTTPException(status_code=404, detail="文件不存在")
    raise HTTPException(status_code=403, detail="该路径不在允许访问的目录内")


@router.get("/files", summary="列出输出与任务文件")
async def list_files(
    limit: int = Query(default=100, ge=1, le=1000),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    return {"files": service.list_output_files(limit=limit)}


@router.get("/files/download", summary="下载文件")
async def download_file(path: str = Query(description="文件绝对路径或相对项目根目录的路径")) -> FileResponse:
    resolved = _safe_path(path)
    return FileResponse(
        resolved,
        filename=resolved.name,
        media_type="application/octet-stream",
    )


@router.get("/files/text", summary="读取文本文件预览")
async def read_text(
    path: str = Query(),
    max_chars: int = Query(default=20000, ge=100, le=500000),
) -> dict[str, Any]:
    resolved = _safe_path(path)
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
    return {
        "path": str(resolved),
        "name": resolved.name,
        "size": resolved.stat().st_size,
        "truncated": len(text) > max_chars,
        "content": text[:max_chars],
    }


@router.delete("/files", summary="删除输出文件")
async def delete_file(path: str = Query()) -> dict[str, Any]:
    resolved = _safe_path(path)
    try:
        size = resolved.stat().st_size
        resolved.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}") from exc
    return {"ok": True, "removed": str(resolved), "bytes": size}


@router.get("/env", summary="读取 .env 原文(密钥已掩码)")
async def read_env(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    from ..service import mask_secret  # 复用同一套掩码规则

    lines = service.store.raw_lines()
    masked: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            if "api_key" in key.lower() and value.strip():
                line = f"{key}={mask_secret(value.strip())}"
        masked.append(line)
    return {"path": str(service.store.path), "lines": masked, "count": len(masked)}


@router.get("/usage", summary="框架能力与端点清单(给界面做帮助面板)")
async def usage() -> dict[str, Any]:
    return {
        "capabilities": [
            {
                "title": "自然语言抓取",
                "detail": "填写目标页面 + 用中文描述要抓什么, AI 会结合页面结构报告生成提取规则。",
                "endpoint": "POST /api/crawl",
            },
            {
                "title": "手写规则",
                "detail": "在规则编辑器中给出 item_selector 与字段选择器, 不依赖 AI, 结果最可控。",
                "endpoint": "POST /api/crawl (rule)",
            },
            {
                "title": "结构分析",
                "detail": "识别重复列表区、唯一选择器、分页器与 JSON-LD/OG 元数据。",
                "endpoint": "POST /api/analyze",
            },
            {
                "title": "网络抓包",
                "detail": "捕获 XHR/fetch/WebSocket, 自动解析 JSON —— 用接口拿数据比解析 HTML 更稳。",
                "endpoint": "POST /api/requests",
            },
            {
                "title": "选择器自愈",
                "detail": "页面改版导致选择器失效时, 让 AI 依据新 DOM 推荐新选择器。",
                "endpoint": "POST /api/rules/repair",
            },
            {
                "title": "AI 数据清洗",
                "detail": "对已抓到的记录做实体识别与字段归一化。",
                "endpoint": "POST /api/ai/clean",
            },
        ],
        "cli": [
            "python -m smartcrawler crawl <url> --goal \"...\" --format csv",
            "python -m smartcrawler analyze <url>",
            "python -m smartcrawler requests <url> --pattern api",
            "python -m smartcrawler serve --port 8322",
        ],
        "notice": "默认遵守 robots.txt、默认随机限速 1~3 秒。仅限合法授权的数据采集场景。",
    }
