"""
插件接口: 列表 / 启停 / 配置 / 重新扫描 / 源码查看 / 模板 / 删除。

安全说明
--------
用户插件是本机代码执行, 因此本模块的**源码读取与删除**接口都做了路径白名单校验
(只允许 ``plugins/`` 目录下的 ``.py``), 避免借助这些接口读写任意文件。
框架不试图沙箱化插件 —— 那在 Python 里无法可靠做到; 取而代之的是界面上的明确
警示, 以及"写好模板再改"的引导。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...config import PROJECT_ROOT
from ...plugins.manager import USER_PLUGIN_DIR
from ..service import CrawlService
from . import get_service

router = APIRouter()

#: 用户插件目录(所有文件操作都被限制在这里)
_USER_DIR = USER_PLUGIN_DIR.resolve()

#: 新建插件的模板(与 plugins/example_enrich.py 保持一致的骨架)
TEMPLATE = '''"""
{name} —— 由 Web 控制台生成的插件骨架。

把它写完并在「插件」页启用即可。可用钩子:
    on_start(ctx) / before_navigate(ctx) / after_navigate(ctx)
    before_extract(ctx) / after_extract(ctx, items) / on_page(ctx) / on_finish(ctx, items)

同步与异步写法都支持: 不需要 await 就写普通 def。
"""

from __future__ import annotations

from typing import Any

from smartcrawler.plugins.base import BasePlugin, PluginContext


class {class_name}(BasePlugin):
    """{description}"""

    id = "{plugin_id}"
    name = "{name}"
    description = "{description}"
    version = "1.0.0"
    author = ""
    category = "cleanup"          # download / anti-bot / cleanup / storage / other
    tags = ["自定义"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {{
            "key": "example_option",
            "label": "示例选项",
            "type": "str",
            "default": "",
            "description": "在这里声明配置项, 界面会自动生成表单",
        }},
    ]

    def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        """在这里改写抓取到的数据。"""
        option = ctx.config.get("example_option")
        ctx.notify("INFO", f"{self.name}: 处理 {{len(items or [])}} 条记录 (option={{option!r}})")
        for item in items or []:
            if isinstance(item, dict):
                item.setdefault("plugin_mark", self.id)
        return items
'''


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class PluginToggleRequest(BaseModel):
    enabled: bool


class PluginConfigRequest(BaseModel):
    config: dict[str, Any] = Field(description="要更新的配置项(只需给出改动字段)")


class PluginCreateRequest(BaseModel):
    filename: str = Field(description="文件名, 如 my_plugin.py")
    name: str = Field(default="我的插件", description="插件显示名")
    description: str = Field(default="", description="插件说明")
    plugin_id: str = Field(default="", description="插件 id; 留空按文件名推导")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
@router.get("", summary="列出全部插件")
async def list_plugins(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    manager = service.crawler_plugins()
    return {
        "plugins": [info.model_dump() for info in manager.list()],
        "stats": manager.stats(),
        "notice": (
            "用户插件等同于在本机运行的 Python 代码, 框架不提供沙箱 —— "
            "请只加载你自己编写或完全信任的插件。"
        ),
    }


@router.post("/refresh", summary="重新扫描插件目录")
async def refresh_plugins(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    manager = service.crawler_plugins()
    plugins = manager.refresh()
    known = {info.id for info in plugins}
    # 扫描后清掉已经失效的插件选择, 避免界面引用不存在的 id
    service.prune_plugin_selection(known)
    return {
        "plugins": [info.model_dump() for info in plugins],
        "stats": manager.stats(),
    }


@router.get("/template", summary="获取插件模板源码")
async def plugin_template() -> dict[str, Any]:
    return {"template": TEMPLATE, "user_dir": str(_USER_DIR)}


@router.get("/source", summary="查看插件源码")
async def plugin_source(
    path: str = Query(description="插件文件路径"),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    resolved = _safe_plugin_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
    return {
        "path": str(resolved),
        "name": resolved.name,
        "size": resolved.stat().st_size,
        "content": text,
        "lines": text.count("\n") + 1,
    }


# ---------------------------------------------------------------------------
# 变更
# ---------------------------------------------------------------------------
@router.post("/{plugin_id}/toggle", summary="启用/停用插件")
async def toggle_plugin(
    plugin_id: str,
    req: PluginToggleRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    manager = service.crawler_plugins()
    try:
        info = manager.set_enabled(plugin_id, req.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "plugin": info.model_dump(), "stats": manager.stats()}


@router.post("/{plugin_id}/config", summary="更新插件配置")
async def update_plugin_config(
    plugin_id: str,
    req: PluginConfigRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    manager = service.crawler_plugins()
    try:
        info = manager.set_config(plugin_id, req.config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "plugin": info.model_dump()}


@router.post("/{plugin_id}/reset", summary="恢复插件默认配置")
async def reset_plugin_config(
    plugin_id: str, service: CrawlService = Depends(get_service)
) -> dict[str, Any]:
    manager = service.crawler_plugins()
    try:
        info = manager.reset_config(plugin_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "plugin": info.model_dump()}


@router.post("/create", summary="由模板创建用户插件")
async def create_plugin(
    req: PluginCreateRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    filename = Path(req.filename).name  # 去掉任何目录成分, 防目录穿越
    if not filename.endswith(".py"):
        filename += ".py"
    if not filename.replace(".py", "").replace("_", "").isalnum():
        raise HTTPException(
            status_code=400,
            detail="文件名只能包含字母、数字与下划线(例如 my_plugin.py)",
        )

    plugin_id = (req.plugin_id or Path(filename).stem).strip().lower().replace("_", "-")
    if not plugin_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="插件 id 只能包含字母、数字与连字符")

    class_name = "".join(part.capitalize() for part in Path(filename).stem.split("_")) + "Plugin"
    target = _USER_DIR / filename
    if target.exists():
        raise HTTPException(status_code=409, detail=f"{filename} 已存在, 请换个文件名")

    _USER_DIR.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(
            TEMPLATE.format(
                name=req.name,
                description=req.description or req.name,
                plugin_id=plugin_id,
                class_name=class_name,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入失败: {exc}") from exc

    service.crawler_plugins().refresh()
    return {
        "ok": True,
        "path": str(target),
        "filename": filename,
        "plugin_id": plugin_id,
        "message": f"已创建 {filename}, 编辑后点『重新扫描』即可启用",
    }


@router.delete("", summary="删除用户插件文件")
async def delete_plugin(
    path: str = Query(description="插件文件路径"),
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    resolved = _safe_plugin_path(path)
    try:
        resolved.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}") from exc
    manager = service.crawler_plugins()
    manager.refresh()
    service.prune_plugin_selection({info.id for info in manager.list()})
    return {"ok": True, "removed": str(resolved)}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _safe_plugin_path(raw: str) -> Path:
    """把路径限制在 ``plugins/`` 目录内, 越界即拒绝。

    这个接口接收用户传入的路径, 若不校验就是一个任意文件读取/删除漏洞。
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"路径无法解析: {exc}") from exc

    if resolved != _USER_DIR and _USER_DIR not in resolved.parents:
        raise HTTPException(
            status_code=403,
            detail=f"只允许访问用户插件目录: {_USER_DIR}",
        )
    if resolved.suffix != ".py":
        raise HTTPException(status_code=403, detail="只允许访问 .py 插件文件")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return resolved
