"""
FastAPI 应用工厂 —— Web 控制台的装配点。

    uvicorn smartcrawler.web.api:app --port 8322
    或  python -m smartcrawler serve

装配顺序有讲究:
1. 挂 loguru 日志总线, 让后续所有日志都能被 WebSocket 看见;
2. 建 ``CrawlService`` 并放进 ``app.state``(路由通过依赖注入取用);
3. 注册 API 路由(都带 ``/api`` 前缀);
4. **最后**挂载前端静态目录 —— 根路径挂载会兜住所有未匹配请求, 必须在 API 之后。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .. import __version__
from ..config import Settings, get_settings
from .logs import LogBus
from .routes import config as config_routes
from .routes import plugins as plugin_routes
from .routes import session as session_routes
from .routes import system as system_routes
from .routes import tasks as task_routes
from .service import CrawlService
from .ws import router as ws_router

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

# 本机开发常用的来源(前端与后端同源时其实用不到 CORS, 但方便用 Live Server 调试页面)
DEV_ORIGINS = [
    "http://localhost:8322",
    "http://127.0.0.1:8322",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class SpaStaticFiles(StaticFiles):
    """静态目录: 命中真实文件就返回文件, 否则回落到 index.html(便于前端路由)。

    **必须显式禁用强缓存**。之前没设 `Cache-Control`, 浏览器就按启发式规则自行缓存,
    连条件请求都不发 —— 结果改完前端、重启服务, 用户刷新页面看到的**还是旧样式**,
    而且完全看不出哪里不对(服务端发的确实是新文件)。这个坑真实发生过, 所以这里:

      - ``Cache-Control: no-cache`` —— 注意它**不是**"不缓存", 而是"每次都要先问服务器";
        配合 ETag, 没变就回 304(省流量), 变了立刻拿到新的(不会看到旧界面);
      - ``no-store`` 只给 index.html 用: 它是入口, 里面引用的资源名可能变, 最不该被留用。
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            index = Path(self.directory) / "index.html"  # type: ignore[arg-type]
            if index.exists():
                response = FileResponse(index)
        # 统一打上"必须回源校验"的头
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        if str(path).endswith("index.html") or path in ("", ".", "/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """构造 FastAPI 应用(可注入设置, 便于测试)。"""
    resolved = settings or get_settings()
    log_bus = LogBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 0) 清理上次崩溃留下的**孤儿**浏览器进程。
        #    **只清孤儿**, 不做无差别清扫: 同一项目下可能同时跑着别的服务实例,
        #    一启动就全清会把别人的浏览器也杀掉(实测复现过: 新实例一启动, 老实例的
        #    4 个浏览器瞬间归零)。"还有活着的祖先管着"的进程一律不动。
        try:
            from ..runtime import kill_orphan_processes

            orphaned = kill_orphan_processes(reason="启动时清理孤儿进程")
            if orphaned:
                logger.info(f"启动时清理了 {orphaned} 个上次残留的孤儿浏览器进程")
        except Exception as exc:  # noqa: BLE001 - 清理失败不该挡住启动
            logger.debug(f"启动清理跳过: {exc}")

        # 1) 绑定事件循环并挂载日志 sink
        # level=INFO: 客户端按级别过滤, 但没必要把 DEBUG 的细节推过网络
        log_bus.bind_loop(asyncio.get_running_loop())
        log_bus.attach(level="INFO")

        # 2) 服务实例
        service = CrawlService(resolved)
        app.state.service = service
        app.state.log_bus = log_bus
        await service.startup()

        # 3) 装上"退出即清理": 关掉服务(或命令行)时把 Playwright 的浏览器进程一起带走。
        #    这一步不做的话, 关了窗口浏览器还在后台跑, 下次启动还会撞端口。
        #    注意清理范围限定为**自己这一支**(runtime 内部用 os.getpid() 限定) ——
        #    同项目下可能同时跑着别的实例, 退出时不该把别人的浏览器也关了。
        try:
            from ..runtime import install_shutdown_cleanup

            install_shutdown_cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"注册退出清理失败: {exc}")

        try:
            yield
        finally:
            await service.shutdown()
            log_bus.detach()

    app = FastAPI(
        title="SmartCrawler 控制台",
        description=(
            "智能爬虫框架的图形化控制台 —— 自然语言抓取 / 结构分析 / 网络抓包 / 规则编辑 / 配置管理。\n\n"
            "⚠️ 仅限合法授权的数据采集场景; 框架默认遵守 robots.txt 并限速。"
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3) API 路由
    app.include_router(system_routes.router, prefix="/api", tags=["system"])
    app.include_router(config_routes.router, prefix="/api/config", tags=["config"])
    app.include_router(plugin_routes.router, prefix="/api/plugins", tags=["plugins"])
    app.include_router(session_routes.router, prefix="/api/session", tags=["session"])
    app.include_router(task_routes.router, prefix="/api", tags=["tasks"])
    # WebSocket 路由自带 /ws 前缀
    app.include_router(ws_router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception(f"未处理的请求异常 {request.url.path}: {exc}")
        return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {type(exc).__name__}"})

    # 4) 前端静态资源(必须最后挂载)
    if FRONTEND_DIR.exists():
        app.mount("/", SpaStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    else:  # pragma: no cover - 只在前端文件缺失时触发
        @app.get("/")
        async def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=500,
                content={"detail": f"前端资源缺失: {FRONTEND_DIR}"},
            )

    return app


# uvicorn 直接启动入口
app = create_app()
