"""
路由聚合与依赖注入。

``get_service`` 是唯一的依赖来源: 它从 ``app.state`` 取出当前会话的
:class:`~smartcrawler.web.service.CrawlService`。用函数依赖而不是模块级全局变量,
是为了让测试可以构造独立实例(见 ``api.py`` 的 ``create_app(settings)``)。

**用法注意**: 必须写成 ``Depends(get_service)``, 不能写 ``get_service()``。
后者会在**模块导入时**就调用函数(此时既没有 Request 也没有 app.state), 直接抛
``TypeError``; 而且 FastAPI 只对默认值为 ``Depends(...)`` 的形参做依赖注入。
本模块还导出一个 :data:`ServiceDep` 别名, 让路由签名更短更统一。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..service import CrawlService


def get_service(request: Request) -> CrawlService:
    """FastAPI 依赖: 从应用状态中取出爬虫服务。"""
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - 正常启动路径不会发生
        raise RuntimeError("CrawlService 尚未初始化")
    return service


# 路由签名统一写 ``service: ServiceDep``, 避免每处重复 Depends(...)
ServiceDep = Annotated[CrawlService, Depends(get_service)]


__all__ = ["get_service", "ServiceDep"]
