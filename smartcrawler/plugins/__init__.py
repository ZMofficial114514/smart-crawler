"""
插件系统。

- :mod:`base`    —— 抽象与配置模型(:class:`BasePlugin` / :class:`PluginContext`)
- :mod:`manager` —— 发现、配置持久化与执行(:class:`PluginManager`)
- :mod:`builtin` —— 随框架分发的内置插件(图片下载器等)

用户插件放在项目根的 ``plugins/`` 目录, 放入 ``.py`` 文件即被 :class:`PluginManager`
自动发现。插件配置持久化在 ``data/plugins.json``。
"""

from __future__ import annotations

__all__ = ["BasePlugin", "PluginContext", "PluginManager", "PluginInfo", "PluginConfigField"]


def __getattr__(name: str):
    """惰性导出, 避免仅 import 子模块时把管理器与内置插件都加载起来。"""
    if name in ("BasePlugin", "PluginContext"):
        from . import base

        return getattr(base, name)
    if name in ("PluginManager", "PluginInfo", "PluginConfigField"):
        from . import manager

        return getattr(manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
