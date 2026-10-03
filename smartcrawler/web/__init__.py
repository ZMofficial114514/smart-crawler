"""
SmartCrawler Web 控制台。

- ``api``      : FastAPI 应用工厂与 uvicorn 入口
- ``service``  : 界面与核心框架之间的服务层(任务编排)
- ``state``    : 任务状态模型与事件总线
- ``config_store`` : 配置内省、.env 读写、类型归一化
- ``logs``     : loguru -> WebSocket 的实时日志总线
- ``routes``   : HTTP 路由(CrawlRequest / 任务 / 配置 / 系统)
- ``ws``       : WebSocket 实时通道(日志 / 任务进度 / 健康)
- ``frontend`` : 零构建的静态前端(原生 ESM + CSS)
"""

from __future__ import annotations

# 在任何子模块导入之前把临时目录指向项目内的可写目录。
# Playwright 启动浏览器时会在此创建 playwright-artifacts-* 目录, 受限环境下
# 系统 TEMP 不可写会直接导致 EPERM。这里做一次幂等的准备, 用户无需关心。
from .__main__ import prepare_temp_dir as _prepare_temp_dir

_prepare_temp_dir()

__all__ = ["create_app", "CrawlService", "LogBus"]


def __getattr__(name: str):
    """惰性导入: 避免只用到 frontend 路径时也加载 FastAPI。"""
    if name == "create_app":
        from .api import create_app

        return create_app
    if name == "CrawlService":
        from .service import CrawlService

        return CrawlService
    if name == "LogBus":
        from .logs import LogBus

        return LogBus
    raise AttributeError(f"module 'smartcrawler.web' has no attribute {name!r}")
