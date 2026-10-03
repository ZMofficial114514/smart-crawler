"""
任务接口: 发起抓取 / 结构分析 / 网络抓包, 查询状态, 取消, 下载结果。

所有耗时操作都**立即返回任务句柄**(``202 Accepted``), 真正的进度与结果通过
``/ws/tasks/{task_id}`` 推送; 这样界面既不会因为长请求超时, 也能实时渲染时间线。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..schemas import AnalyzeRequest, CleanDataRequest, RequestsRequest, CrawlRequest, RuleRepairRequest, RuleValidateRequest
from ..service import ConfigError, CrawlService
from . import get_service

router = APIRouter()


def _submit(service: CrawlService, starter, params: dict[str, Any]) -> dict[str, Any]:
    """启动任务并统一返回句柄; 参数类错误转成 400 而不是 500。"""
    try:
        task = starter(params)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": task.to_dict(include_result=False)}


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
@router.post("/crawl", status_code=status.HTTP_202_ACCEPTED, summary="发起抓取任务")
async def start_crawl(req: CrawlRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    params = req.model_dump()
    # 规则先做一次预校验, 让明显的 JSON 错误在提交时就报出来
    if params.get("rule"):
        service._parse_rule(params["rule"])  # noqa: SLF001 - 同一模块内的前置校验
    return _submit(service, service.start_crawl, params)


@router.post("/analyze", status_code=status.HTTP_202_ACCEPTED, summary="发起页面结构分析")
async def start_analyze(req: AnalyzeRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return _submit(service, service.start_analyze, req.model_dump())


@router.post("/requests", status_code=status.HTTP_202_ACCEPTED, summary="发起网络抓包")
async def start_requests(req: RequestsRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return _submit(service, service.start_requests, req.model_dump())


# ---------------------------------------------------------------------------
# 任务查询与取消
# ---------------------------------------------------------------------------
@router.get("/tasks", summary="任务列表")
async def list_tasks(
    limit: int = Query(default=30, ge=1, le=200),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    return {"tasks": service.tasks.list(limit=limit), "active": service.tasks.active()}


# 注意: 字面量路径必须**排在** `/tasks/{task_id}` 之前, 否则会被后者当成长度为
# "by-url" 的 task_id 吞掉, 直接返回 404。FastAPI 按注册顺序匹配, 这里不能图省事。
@router.get("/tasks/by-url", summary="按目标 URL 归类任务与产出文件")
async def tasks_by_url(
    limit: int = Query(default=200, ge=1, le=2000),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    return {"groups": service.list_url_groups(limit=limit)}


@router.delete("/tasks/by-url", summary="按 URL 删除全部记录与产出文件")
async def delete_url_group(
    url: str = Query(description="要清理的目标 URL(与新增时填写的完全一致)"),
    remove_files: bool = Query(default=True, description="是否同时删除产出文件"),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    result = service.delete_url_group(url, remove_files=remove_files)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "删除失败")
    return result


@router.get("/tasks/{task_id}", summary="任务详情")
async def task_detail(task_id: str, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    task = service.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已被回收")
    return task.to_dict()


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
async def cancel_task(task_id: str, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    task = service.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ("queued", "running"):
        return {"ok": False, "message": f"任务已处于 {task.status} 状态"}
    ok = service.tasks.cancel(task_id)
    return {"ok": ok, "message": "已发送取消信号" if ok else "任务已结束"}


class ScrollAnswerRequest(BaseModel):
    """POST /api/tasks/{id}/scroll —— 回答"是否继续向下滚动"。"""

    continue_scroll: bool = Field(
        default=False, alias="continue", description="true=继续滚动, false=停止(按当前内容出结果)"
    )
    rounds: int = Field(default=0, ge=0, le=500, description="本次追加的滚动轮次; 0 表示用默认值")

    model_config = {"populate_by_name": True}


@router.post("/tasks/{task_id}/scroll", summary="回答是否继续向下滚动(无限流页面)")
async def answer_scroll(
    task_id: str,
    req: ScrollAnswerRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    return service.answer_scroll(task_id, req.continue_scroll, req.rounds)


@router.delete("/tasks/{task_id}", summary="删除任务并清理其产出文件")
async def delete_task(
    task_id: str,
    remove_files: bool = Query(default=True, description="是否同时删除产出文件"),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    result = service.delete_task(task_id, remove_files=remove_files)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "删除失败")
    return result


@router.get("/tasks/{task_id}/download", summary="下载任务完整结果")
async def download_task_result(
    task_id: str,
    which: str = Query(default="artifact", description="artifact=任务结果 JSON / export=导出文件"),
    service: CrawlService = Depends(get_service),
) -> FileResponse:
    task = service.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.artifacts:
        raise HTTPException(status_code=404, detail="该任务没有可下载的产物")

    if which == "export":
        candidates = [a for a in task.artifacts if a["kind"] != "json"]
    else:
        candidates = [a for a in task.artifacts if a["kind"] == "json"]
    target = candidates[-1] if candidates else task.artifacts[-1]

    path = Path(target["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="产物文件已不存在")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# 规则工具
# ---------------------------------------------------------------------------
@router.post("/rules/validate", summary="校验提取规则(不执行抓取)")
async def validate_rule(req: RuleValidateRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    try:
        rule = service._parse_rule(req.rule)  # noqa: SLF001
    except ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    if rule is None:
        return {"ok": False, "error": "规则为空"}
    return {
        "ok": True,
        "rule": rule.model_dump(mode="json"),
        "field_count": len(rule.list_rule.fields) if rule.list_rule else 0,
        "notes": ["规则结构合法。注意: 选择器是否命中目标元素需要实际抓取才能验证。"],
    }


@router.post("/rules/repair", summary="AI 修复失效选择器")
async def repair_rule(req: RuleRepairRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    crawler = await service._get_crawler(interactive=True)  # noqa: SLF001
    if not service._ai_available():  # noqa: SLF001
        raise HTTPException(status_code=400, detail="AI 未就绪, 无法修复选择器")

    page = await crawler.browser.new_page()
    try:
        if await crawler.browser.goto(page, req.url) is None:
            raise HTTPException(status_code=502, detail="无法打开目标页面")
        try:
            html = await page.content()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"读取页面 HTML 失败: {exc}") from exc
        hits = await crawler.browser.highlight(page, req.selector)
        new_selector = await crawler.ai.repair_selector(req.selector, html, req.goal or "")
    finally:
        await crawler.browser.close_page(page)

    if not new_selector:
        return {"ok": False, "message": "AI 未能给出新的选择器", "original_hits": hits}
    return {
        "ok": True,
        "selector": new_selector,
        "original_hits": hits,
        "message": f"建议新选择器: {new_selector}",
    }


@router.post("/ai/clean", summary="AI 清洗与归一化数据")
async def clean_data(req: CleanDataRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    crawler = await service._get_crawler(interactive=True)  # noqa: SLF001
    if not service._ai_available():  # noqa: SLF001
        raise HTTPException(status_code=400, detail="AI 未就绪, 无法清洗数据")
    cleaned = await crawler.ai.clean_data(req.items[:200], req.schema_)
    if cleaned is None:
        raise HTTPException(status_code=502, detail="AI 清洗失败, 详见实时日志")
    return {"ok": True, "items": cleaned, "count": len(cleaned)}


# ---------------------------------------------------------------------------
# 历史任务里的产物(按 task_id 直接取, 不经过白名单)
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/items", summary="分页读取任务结果条目")
async def task_items(
    task_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    task = service.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    items: Optional[list[dict[str, Any]]] = task.result.get("items_preview")
    if items is None:
        # 完整结果在落盘文件里
        json_artifacts = [a for a in task.artifacts if a["kind"] == "json"]
        if not json_artifacts:
            raise HTTPException(status_code=404, detail="该任务没有条目数据")
        import json

        payload = json.loads(Path(json_artifacts[-1]["path"]).read_text(encoding="utf-8"))
        items = (payload.get("result") or {}).get("items") or []
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": items[offset : offset + limit],
    }
