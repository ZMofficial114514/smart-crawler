"""
配置接口: 读取当前生效配置、批量应用改动、列出可下载文件之外的运维动作入口。

安全约定: 配置可以改, 但**不允许**通过接口把任意值写进 .env —— 只有出现在
Pydantic Schema 里的字段才可写(见 ``CrawlService.apply_patch`` 的未知键校验)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..schemas import ConfigPatchRequest
from ..service import ConfigError, CrawlService
from . import get_service

router = APIRouter()


@router.get("", summary="读取当前配置(含字段 Schema 与来源标注)")
async def read_config(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.read_config()


@router.post("", summary="应用一批配置改动")
async def patch_config(req: ConfigPatchRequest, service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    try:
        result = service.apply_patch(req.patch, persist=req.persist)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.post("/reload", summary="从 .env 重新加载配置")
async def reload_config(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    """放弃运行时改动, 回到 .env + 环境变量描述的状态。"""
    from ...config import ENV_FILE, Settings

    try:
        service.settings = Settings.load(ENV_FILE if ENV_FILE.exists() else None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"重新加载失败: {exc}") from exc
    service.mark_dirty(interactive=False)
    service.mark_dirty(interactive=True)
    return {"ok": True, "values": service.read_config()["values"], "message": "已从 .env 重新加载"}
