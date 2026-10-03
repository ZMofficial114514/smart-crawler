"""
插件系统 —— 核心抽象。

设计目标
--------
让用户在**不改动框架代码**的前提下扩展采集能力: 图片/音乐下载、新的反爬策略、
自定义清洗与存储。为此提供两条并行的扩展路径:

1. **Python 单文件插件** —— 往 ``plugins/`` 丢一个 ``.py`` 即可被识别。
   能力上限最高(可写任意逻辑), 但等同于在本机运行代码, 所以加载前会明确提示
   "只加载你信任的插件", 并且单个插件报错不会拖垮整条抓取链路。

2. **声明式配置插件** —— 不写代码, 只在界面上配置"已知能力":
   ``headers``(加请求头) / ``user_agent``(换 UA) / ``delay_multiplier``(限速倍率) /
   ``download``(按 URL 模式下载) / ``stealth``(指纹伪装) / ``export``(另存一份结果)。
   对只想调参的用户更安全, 也更容易复用。

生命周期钩子
------------
::

    on_start(ctx)          任务开始
      ├─ before_navigate(ctx)   每次导航前(可加请求头)
      ├─ after_navigate(ctx)    页面加载后(可滚动/注入脚本)
      ├─ before_extract(ctx)    提取前
      ├─ after_extract(ctx, items)  提取到数据后(下载/清洗/补充字段) ← 最常用
      └─ on_page(ctx, page_no)  每翻完一页
    on_finish(ctx, items, result)  任务结束(汇总产物)

钩子可以是同步函数也可以是 ``async def``: 管理器会检测返回值是否为 awaitable,
因此"只加一个请求头"这种简单插件不必被迫写成异步。

所有钩子都做**失败隔离**: 抛异常只记录到 ``plugin_errors``, 不向上冒泡。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from pydantic import BaseModel, Field

from ..config import PROJECT_ROOT, Settings
from ..models import DownloadedFile

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注, 避免运行期循环导入
    from playwright.async_api import Page

    from ..crawler import SmartCrawler

# ---------------------------------------------------------------------------
# 插件配置模型
# ---------------------------------------------------------------------------
class PluginInfo(BaseModel):
    """插件的自描述信息(界面据此渲染卡片)。"""

    id: str = Field(description="唯一标识, 建议用 kebab-case")
    name: str = Field(description="显示名")
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    category: str = Field(default="other", description="download / anti-bot / storage / cleanup / other")
    # 来源: builtin=框架内置 / user=plugins/ 目录下的用户插件
    source: str = Field(default="builtin", description="builtin / user")
    file: str = Field(default="", description="用户插件的文件路径")
    requires: list[str] = Field(default_factory=list, description="依赖的第三方包(仅作提示)")
    tags: list[str] = Field(default_factory=list)

    # 配置项声明: 界面据此动态生成表单
    config_schema: list["PluginConfigField"] = Field(
        default_factory=list, description="该插件可配置的字段"
    )
    config: dict[str, Any] = Field(default_factory=dict, description="当前生效的配置值")
    default_config: dict[str, Any] = Field(default_factory=dict, description="默认配置值")

    enabled: bool = Field(default=False, description="是否启用")
    default_enabled: bool = Field(default=False, description="框架建议的默认开关")
    # 用户插件无法在加载前知道其钩子, 这里给出声明的能力提示
    hooks: list[str] = Field(default_factory=list, description="实现了哪些钩子")
    load_error: Optional[str] = Field(default=None, description="加载失败原因")
    run_count: int = Field(default=0, description="本次会话被调用的次数")
    last_error: Optional[str] = Field(default=None, description="最近一次运行错误")


class PluginConfigField(BaseModel):
    """插件的一个可配置字段(与核心配置面板的控件类型保持一致)。"""

    key: str
    label: str
    type: str = Field(default="str", description="bool/int/float/str/textarea/list/json/enum")
    description: str = ""
    default: Any = None
    options: Optional[list[Any]] = None
    min: Optional[float] = None
    max: Optional[float] = None


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------
@dataclass
class PluginContext:
    """传递给插件钩子的运行时上下文。"""

    settings: Settings
    url: str = ""
    page: Optional["Page"] = None
    page_no: int = 1
    crawler: Optional["SmartCrawler"] = None
    # 进度播报: 插件可以调用它把信息推给界面的实时日志
    notify: Callable[[str, str], None] = field(default=lambda level, message: None)
    # 本次任务的插件配置(由 PluginManager 注入)
    config: dict[str, Any] = field(default_factory=dict)
    # 任务开始时记录, 供 before_navigate 注请求头等使用
    data: dict[str, Any] = field(default_factory=dict)
    # 下载产物累积区(与管理器共享同一个 list)
    downloads: list[DownloadedFile] = field(default_factory=list)
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "plugin_output")

    def download_path(self, subdir: str, filename: str) -> Path:
        """按插件名归入子目录, 避免不同插件的产物混在一起。"""
        target = self.output_dir / subdir
        target.mkdir(parents=True, exist_ok=True)
        return target / filename


# ---------------------------------------------------------------------------
# 插件基类
# ---------------------------------------------------------------------------
class BasePlugin:
    """所有插件(内置与用户)的基类。

    子类至少要覆盖 :meth:`info` 或类属性 ``PLUGIN``。最简写法::

        from smartcrawler.plugins.base import BasePlugin

        class MyPlugin(BasePlugin):
            id = "my-plugin"
            name = "我的插件"
            description = "只做一件事"
            category = "cleanup"

            def after_extract(self, ctx, items):
                for item in items:
                    item["tag"] = "hello"
                return items
    """

    # ---- 子类可直接覆盖这些类属性(比实现 info() 更省事) ----
    id: str = ""
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    category: str = "other"
    requires: list[str] = []
    tags: list[str] = []
    default_enabled: bool = False
    config_schema: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def info(self) -> PluginInfo:
        """返回插件元信息(默认由类属性构造, 需要动态值时可在子类覆盖)。"""
        return PluginInfo(
            id=self.id or self.__class__.__name__.lower(),
            name=self.name or self.__class__.__name__,
            description=(self.description or (self.__doc__ or "")).strip().split("\n")[0],
            version=self.version,
            author=self.author,
            category=self.category,
            source="builtin",
            requires=list(self.requires),
            tags=list(self.tags),
            default_enabled=self.default_enabled,
            config_schema=[PluginConfigField(**f) for f in self.config_schema],
            default_config={f["key"]: f.get("default") for f in self.config_schema},
            hooks=[name for name in HOOK_NAMES if self.implements(name)],
        )

    def implements(self, hook: str) -> bool:
        """判断子类是否真的实现了某个钩子(基类里这些方法都不存在)。"""
        return callable(getattr(self, hook, None))

    # ---- 生命周期钩子(基类不定义, 由子类按需实现) ----
    #   on_start / before_navigate / after_navigate / before_extract
    #   after_extract / on_page / on_finish


#: 钩子名与调用顺序(管理器据此遍历)
HOOK_NAMES: tuple[str, ...] = (
    "on_start",
    "before_navigate",
    "after_navigate",
    "before_extract",
    "after_extract",
    "on_page",
    "on_finish",
)

#: 需要传入 items 参数的钩子
ITEM_HOOKS: frozenset[str] = frozenset({"after_extract", "on_finish"})


async def call_hook(plugin: BasePlugin, hook: str, ctx: PluginContext, items: Optional[list] = None):
    """调用插件钩子, 自动兼容同步/异步实现。

    返回钩子的原始返回值(``after_extract`` 可返回新的 items 列表)。
    """
    func = getattr(plugin, hook, None)
    if not callable(func):
        return None
    result = func(ctx, items) if hook in ITEM_HOOKS else func(ctx)
    if inspect.isawaitable(result):
        result = await result
    return result


def call_hook_sync(plugin: BasePlugin, hook: str, ctx: PluginContext, items: Optional[list] = None):
    """同步调用钩子; 若插件是异步实现则返回 coroutine 交由调用方判断。

    存在的意义: 让 ``PluginManager.run`` 能在不引入 await 的前提下复用同一套
    "取方法 -> 传参" 逻辑, 同时对异步插件给出明确报错而不是静默丢弃 coroutine。
    """
    func = getattr(plugin, hook, None)
    if not callable(func):
        return None
    return func(ctx, items) if hook in ITEM_HOOKS else func(ctx)
