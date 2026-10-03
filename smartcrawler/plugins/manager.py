"""
插件管理器 —— 发现、配置、执行。

配置持久化在 ``data/plugins.json``(而不是 ``.env``)。理由: 插件是用户自行增删的,
把每个插件的每个参数都摊成 ``SC_PLUGIN__xxx`` 环境变量会让 ``.env`` 迅速失控, 也会
污染「系统配置」页那份 54 项的稳定清单。插件配置独立存放后可以自由增删字段。

发现顺序:
1. **内置插件** —— ``smartcrawler/plugins/builtin/`` 下的模块(随框架分发);
2. **用户插件** —— 项目根的 ``plugins/`` 目录下任意 ``*.py``, 按文件名排序加载。

用户插件是**本机代码执行**, 因此: 加载失败只标记该插件而继续启动; 钩子抛异常只
记录到任务结果, 绝不影响主流程。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import pkgutil
import traceback
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..config import PROJECT_ROOT, Settings
from ..models import DownloadedFile
from .base import HOOK_NAMES, BasePlugin, PluginContext, PluginInfo, call_hook

#: 用户插件目录
USER_PLUGIN_DIR = PROJECT_ROOT / "plugins"
#: 插件配置持久化位置
PLUGIN_CONFIG_PATH = PROJECT_ROOT / "data" / "plugins.json"


class PluginManager:
    """插件注册表 + 配置 + 执行器。"""

    def __init__(self, settings: Settings, config_path: Optional[Path] = None) -> None:
        self.settings = settings
        self.config_path = Path(config_path) if config_path else PLUGIN_CONFIG_PATH
        self.output_dir = PROJECT_ROOT / "data" / "plugin_output"

        self._plugins: dict[str, BasePlugin] = {}
        self._infos: dict[str, PluginInfo] = {}
        self._state: dict[str, dict[str, Any]] = {}  # {plugin_id: {enabled, config}}
        self._downloads: list[DownloadedFile] = []
        self._errors: list[str] = []
        self._used: set[str] = set()
        self._reload: bool = True

    # ------------------------------------------------------------------
    # 发现与加载
    # ------------------------------------------------------------------
    def ensure_loaded(self, force: bool = False) -> None:
        """惰性加载插件清单(首次访问时执行)。"""
        if not self._reload and not force:
            return
        self._state = self._load_state()
        self._plugins.clear()
        self._infos.clear()
        self._load_builtins()
        self._load_user_plugins()
        self._reload = False

    def refresh(self) -> list[PluginInfo]:
        """重新扫描插件目录(用户新增/删除文件后调用)。"""
        self.ensure_loaded(force=True)
        return self.list()

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"插件配置读取失败, 将使用默认值: {exc}")
            return {}

    def _save_state(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"插件配置写入失败: {exc}")

    def _register(self, plugin: BasePlugin, *, source: str, file: str = "") -> None:
        try:
            info = plugin.info()
        except Exception as exc:  # noqa: BLE001 - 插件自描述出错不应中断加载
            logger.warning(f"插件 {plugin.__class__.__name__} 的 info() 失败: {exc}")
            return
        info.source = source  # type: ignore[assignment]
        if file:
            info.file = file

        saved = self._state.get(info.id, {})
        info.enabled = bool(saved.get("enabled", info.default_enabled))
        # 配置 = 默认值 <- 已保存值(兼容插件升级后新增字段)
        merged = {**info.default_config, **(saved.get("config") or {})}
        info.config = merged
        self._plugins[info.id] = plugin
        self._infos[info.id] = info

    def _load_builtins(self) -> None:
        from . import builtin as builtin_pkg

        for module_info in pkgutil.iter_modules(builtin_pkg.__path__):
            if module_info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{builtin_pkg.__name__}.{module_info.name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"内置插件模块 {module_info.name} 导入失败: {exc}")
                continue
            for plugin in _instantiate_plugins(module):
                self._register(plugin, source="builtin")

    def _load_user_plugins(self) -> None:
        """加载项目根 ``plugins/`` 下的用户插件。

        安全提示: 这等同于运行本机 Python 代码。界面在插件页显著位置提示
        "只加载你信任的插件", 并且框架只负责调用, 不提供沙箱。
        """
        if not USER_PLUGIN_DIR.exists():
            return
        for path in sorted(USER_PLUGIN_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _load_module_from_path(path)
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning(f"用户插件加载失败 {path.name}: {detail}")
                # 记录为"加载失败"条目, 让界面能提示用户而不是静默忽略
                self._infos[f"broken:{path.name}"] = PluginInfo(
                    id=f"broken:{path.name}",
                    name=path.stem,
                    description="插件加载失败",
                    source="user",
                    file=str(path),
                    load_error=detail,
                    category="other",
                )
                continue
            found = list(_instantiate_plugins(module))
            if not found:
                logger.debug(f"用户插件文件 {path.name} 中未找到 BasePlugin 子类")
            for plugin in found:
                self._register(plugin, source="user", file=str(path))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list(self) -> list[PluginInfo]:
        """列出全部插件(启用优先, 再按分类与名称排序)。"""
        self.ensure_loaded()
        infos = list(self._infos.values())
        order = {"download": 0, "anti-bot": 1, "cleanup": 2, "storage": 3, "other": 4}
        infos.sort(key=lambda i: (not i.enabled, order.get(i.category, 9), i.name))
        return infos

    def get_info(self, plugin_id: str) -> Optional[PluginInfo]:
        self.ensure_loaded()
        return self._infos.get(plugin_id)

    def get(self, plugin_id: str) -> Optional[BasePlugin]:
        self.ensure_loaded()
        return self._plugins.get(plugin_id)

    def enabled_plugins(self, only: Optional[list[str]] = None) -> list[tuple[BasePlugin, PluginInfo]]:
        """返回本次要执行的插件(启用 且 未被 only 排除)。"""
        self.ensure_loaded()
        out: list[tuple[BasePlugin, PluginInfo]] = []
        for plugin_id, plugin in self._plugins.items():
            info = self._infos[plugin_id]
            if not info.enabled:
                continue
            if only is not None and plugin_id not in only:
                continue
            out.append((plugin, info))
        return out

    # ------------------------------------------------------------------
    # 配置变更
    # ------------------------------------------------------------------
    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginInfo:
        info = self._require(plugin_id)
        info.enabled = enabled
        self._state.setdefault(plugin_id, {})["enabled"] = enabled
        self._save_state()
        logger.info(f"插件 {info.name} 已{'启用' if enabled else '停用'}")
        return info

    def set_config(self, plugin_id: str, patch: dict[str, Any]) -> PluginInfo:
        """更新插件配置(逐字段做类型归一化, 非法值直接报错)。"""
        info = self._require(plugin_id)
        coerced: dict[str, Any] = {}
        for key, value in patch.items():
            field = next((f for f in info.config_schema if f.key == key), None)
            if field is None:
                raise ValueError(f"插件 {info.name} 没有配置项 {key!r}")
            coerced[key] = _coerce(field.type, value, field)

        merged = {**info.config, **coerced}
        info.config = merged
        entry = self._state.setdefault(plugin_id, {})
        entry["config"] = {**(entry.get("config") or {}), **coerced}
        self._save_state()
        return info

    def reset_config(self, plugin_id: str) -> PluginInfo:
        info = self._require(plugin_id)
        info.config = dict(info.default_config)
        self._state[plugin_id] = {"enabled": info.enabled}
        self._save_state()
        return info

    def _require(self, plugin_id: str) -> PluginInfo:
        info = self.get_info(plugin_id)
        if info is None:
            raise KeyError(f"插件不存在: {plugin_id}")
        return info

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def begin_task(self) -> None:
        """每次任务开始时重置累积状态。"""
        self._downloads = []
        self._errors = []
        self._used = set()

    def run(self, hook: str, ctx: PluginContext, items: Optional[list] = None,
            only: Optional[list[str]] = None) -> list:
        """**同步**执行钩子的便捷入口(仅供无需 await 的场景, 如脚本/测试)。

        主流程请用 :meth:`run_async` —— 它才能正确 await 异步插件, 并保证钩子之间
        的顺序与失败隔离。
        """
        self.ensure_loaded()
        current = items
        for plugin, info in self.enabled_plugins(only):
            if not plugin.implements(hook):
                continue
            local_ctx = self._make_context(ctx, info)
            try:
                result = call_hook_sync(plugin, hook, local_ctx, current)
                if inspect.isawaitable(result):
                    raise RuntimeError(
                        f"插件 {info.id} 的 {hook} 是异步实现, 请改用 run_async()"
                    )
                self._used.add(info.id)
                info.run_count += 1
                if hook == "after_extract" and isinstance(result, list):
                    current = result
            except Exception as exc:  # noqa: BLE001
                self._record_plugin_error(info, hook, exc)
        return current if current is not None else (items or [])

    async def run_async(self, hook: str, ctx: PluginContext, items: Optional[list] = None,
                        only: Optional[list[str]] = None) -> list:
        """执行某个钩子的全部插件实现, 返回(可能被修改过的)items。

        钩子执行**失败隔离**: 单个插件抛异常只记入 ``plugin_errors`` 并继续下一个。
        对 ``after_extract`` 这类会改写数据的钩子, 若插件返回了列表则以返回值为准。
        """
        self.ensure_loaded()
        current = items
        for plugin, info in self.enabled_plugins(only):
            if not plugin.implements(hook):
                continue
            local_ctx = self._make_context(ctx, info)
            try:
                result = await call_hook(plugin, hook, local_ctx, current)
                self._used.add(info.id)
                info.run_count += 1
                if hook == "after_extract" and isinstance(result, list):
                    current = result
            except Exception as exc:  # noqa: BLE001 - 插件故障不得影响主流程
                self._record_plugin_error(info, hook, exc)
        return current if current is not None else (items or [])

    def _make_context(self, ctx: PluginContext, info: PluginInfo) -> PluginContext:
        """为单个插件派生上下文(注入它自己的配置与共享的产物列表)。"""
        return PluginContext(
            settings=self.settings,
            url=ctx.url,
            page=ctx.page,
            page_no=ctx.page_no,
            crawler=ctx.crawler,
            notify=ctx.notify,
            config=dict(info.config),
            data=ctx.data,
            downloads=self._downloads,
            output_dir=self.output_dir,
        )

    def _record_plugin_error(self, info: PluginInfo, hook: str, exc: Exception) -> None:
        detail = f"插件 {info.name}({info.id}) 在 {hook} 阶段出错: {type(exc).__name__}: {exc}"
        logger.warning(detail)
        logger.debug(traceback.format_exc())
        self._errors.append(detail)
        info.last_error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 结果汇总
    # ------------------------------------------------------------------
    @property
    def downloads(self) -> list[DownloadedFile]:
        return list(self._downloads)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def used(self) -> list[str]:
        return sorted(self._used)

    def add_download(self, record: DownloadedFile) -> None:
        self._downloads.append(record)

    def stats(self) -> dict[str, Any]:
        self.ensure_loaded()
        infos = list(self._infos.values())
        return {
            "total": len(infos),
            "enabled": sum(1 for i in infos if i.enabled),
            "user": sum(1 for i in infos if i.source == "user"),
            "builtin": sum(1 for i in infos if i.source == "builtin"),
            "broken": sum(1 for i in infos if i.load_error),
            "downloads": len(self._downloads),
            "errors": len(self._errors),
            "config_path": str(self.config_path),
            "user_dir": str(USER_PLUGIN_DIR),
            "output_dir": str(self.output_dir),
        }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _instantiate_plugins(module: Any):
    """从模块中找出所有 BasePlugin 子类并实例化(跳过基类与抽象类)。"""
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if not issubclass(obj, BasePlugin) or obj is BasePlugin:
            continue
        if obj.__module__ != module.__name__:
            continue  # 只处理本模块定义的类, 跳过 import 进来的
        try:
            yield obj()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"插件 {obj.__name__} 实例化失败: {exc}")


def _load_module_from_path(path: Path):
    """按文件路径加载 Python 模块(用户插件用)。"""
    module_name = f"smartcrawler_user_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 创建模块规格")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coerce(kind: str, value: Any, field) -> Any:
    """把界面传来的值归一化成配置声明的类型。"""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
    if kind in ("int", "float"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num = value
        else:
            text = str(value).strip()
            if text == "":
                raise ValueError(f"{field.label} 不能为空")
            num = float(text) if kind == "float" else int(float(text))
        if field.min is not None and num < field.min:
            raise ValueError(f"{field.label} 不能小于 {field.min}")
        if field.max is not None and num > field.max:
            raise ValueError(f"{field.label} 不能大于 {field.max}")
        return num
    if kind in ("list", "json"):
        if isinstance(value, (list, dict)):
            return value
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
        if kind == "list" and not isinstance(parsed, list):
            raise ValueError(f"{field.label} 需要一个列表")
        return parsed
    if kind == "enum":
        text = str(value).strip()
        if field.options and text not in [str(o) for o in field.options]:
            raise ValueError(f"{field.label} 只能是 {field.options}")
        return text
    return "" if value is None else str(value)
