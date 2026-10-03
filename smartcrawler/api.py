"""
FastAPI 应用入口(兼容层)。

原本这个文件是项目里唯一的 HTTP 接口, 只有四个临时性的调试端点。现在完整的
控制台后端已经迁移到 :mod:`smartcrawler.web`, 因此这里退化为一个**转发模块**:

- 保留 ``smartcrawler.api:app`` 与 ``create_app()`` 的旧路径, 老脚本/书签不会失效;
- 旧的四端点调试接口移到 :mod:`smartcrawler.web.api_legacy`, 需要时可按下面的
  注释挂载到新应用上(默认不挂, 以免与 /api 下的同名路由冲突)。

新的启动方式::

    python -m smartcrawler web           # 图形化控制台
    python -m smartcrawler serve         # 纯 API + /api/docs
"""

from __future__ import annotations

from typing import Any

from .web.api import FRONTEND_DIR, SpaStaticFiles, create_app

__all__ = ["app", "create_app", "SpaStaticFiles", "FRONTEND_DIR", "legacy_app"]


def legacy_app() -> Any:
    """按需构造旧版调试应用。

    用法(把旧端点挂到新应用上, 用于兼容既有集成)::

        from fastapi import FastAPI
        from smartcrawler.api import create_app, legacy_app

        app: FastAPI = create_app()
        app.mount("/legacy", legacy_app())
    """
    from .web.api_legacy import create_app as _create_legacy

    return _create_legacy()


# uvicorn 直接启动入口: uvicorn smartcrawler.api:app
app = create_app()
