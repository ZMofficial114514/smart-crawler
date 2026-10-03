"""内置插件集合。

每个模块定义一个或多个 :class:`~smartcrawler.plugins.base.BasePlugin` 子类,
:class:`~smartcrawler.plugins.manager.PluginManager` 会扫描本包自动注册。

新增内置插件只需在这里加一个 ``.py`` 文件, 无需改动任何注册表。
"""

from __future__ import annotations

__all__ = ["ImageDownloaderPlugin"]


def __getattr__(name: str):
    """惰性导出, 避免导入本包时把全部内置插件(及其依赖)都拉起来。"""
    if name == "ImageDownloaderPlugin":
        from .image_downloader import ImageDownloaderPlugin

        return ImageDownloaderPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
