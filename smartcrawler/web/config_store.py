"""
配置读写与"配置 Schema"生成 —— Web 设置面板的后端支撑。

三件事:
1. :func:`build_config_schema` 从 Pydantic 配置模型**内省**出 UI 渲染所需的
   元数据(类型/默认值/范围/选项/说明/敏感标记)。也就是说, 以后往
   ``config.py`` 里加一个字段, 前端设置面板会自动多出一个控件, 无需改前端代码。
2. :class:`EnvConfigStore` 负责 ``.env`` 的读取 / 合并 / 原子写入, 并识别
   "真实环境变量遮挡 .env" 的情况(此时界面会提示该项已被环境变量锁定)。
3. :func:`coerce_value` 把界面传来的字符串/JSON 归一化成模型要求的类型。

不在这里做权限校验 —— 那是路由层的职责。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ..config import ENV_FILE, ENV_PREFIX, NEST_DELIMITER, Settings

# ---------------------------------------------------------------------------
# 敏感字段
# ---------------------------------------------------------------------------
# 这些字段的回传值会被掩码处理, 且前端以密码框呈现。
SENSITIVE_FIELDS = {"ai.api_key"}

# 修改后需要重启浏览器才生效的字段(界面会给出提示)
BROWSER_RESTART_FIELDS = {
    "browser.engine",
    "browser.headless",
    "browser.viewport_width",
    "browser.viewport_height",
    "browser.locale",
    "browser.timezone",
    "browser.stealth",
    "browser.storage_state",
    "browser.user_agent",
    "browser.max_pages",
    "anti_spider.proxies",
    "anti_spider.rotate_user_agent",
}

# 这些长文本字段在界面上用多行输入
TEXTAREA_FIELDS = {"anti_spider.proxies"}


def mask_secret(value: str, keep_tail: int = 4) -> str:
    """把密钥掩码成 ``sk-1****cdef`` 形式, 保留一点可辨识信息。"""
    if not value:
        return ""
    if len(value) <= keep_tail * 2:
        return "*" * len(value)
    return f"{value[:keep_tail]}{'*' * 8}{value[-keep_tail:]}"


def is_sensitive(path: str) -> bool:
    return path in SENSITIVE_FIELDS or path.endswith("api_key")


# ---------------------------------------------------------------------------
# 类型内省
# ---------------------------------------------------------------------------
def _unwrap_optional(annotation: Any) -> Any:
    """去掉 Optional[X] / Union[X, None] 外壳, 返回 X。

    关键点: ``get_args`` 对**任何**泛型都会返回参数 —— ``get_args(list[float])``
    是 ``(float,)``。因此必须先用 ``get_origin`` 确认这真的是一个 Union,
    否则 ``list[float]`` 会被错误地"剥"成 ``float``, 让列表字段在界面上变成
    单值数字输入框(这正是本项目踩过的坑)。
    """
    import types
    import typing

    origin = get_origin(annotation)
    if origin is not typing.Union and origin is not types.UnionType:
        return annotation

    args = [a for a in get_args(annotation) if a is not type(None)]  # noqa: E721
    if len(args) == 1:
        return args[0]
    if not args:
        return annotation
    # 仍有多个候选(如 Optional[Union[X, Y]]): 取第一个, 保持可预测
    return args[0]


def _literal_options(annotation: Any) -> Optional[list[Any]]:
    """提取 Literal[...] 的枚举候选(含被 Optional 包裹的情况)。"""
    import typing

    # 直接命中
    if get_origin(annotation) is typing.Literal:
        return list(get_args(annotation))

    # Literal 可能藏在 Union 里(如 Optional[Literal[...]])
    for arg in get_args(annotation):
        if get_origin(arg) is typing.Literal:
            return list(get_args(arg))
    return None


def _field_kind(annotation: Any) -> tuple[str, Optional[list[Any]]]:
    """把 Python 注解映射为界面控件类型。

    返回 (kind, options)。kind ∈ {bool,int,float,str,textarea,enum,list,json}
    """
    inner = _unwrap_optional(annotation)

    options = _literal_options(annotation)
    if options:
        return "enum", options

    origin = get_origin(inner)
    if origin in (list, tuple, set):
        args = get_args(inner)
        # list[str] / list[float] 用标签式输入; 更复杂的结构退化为 JSON 编辑器
        if args and all(a in (str, int, float) for a in args):
            return "list", None
        return "json", None
    if origin is dict:
        return "json", None
    if inner is bool:
        return "bool", None
    if inner is int:
        return "int", None
    if inner is float:
        return "float", None
    if inner is str:
        return "str", None
    return "json", None


def _constraints(field: FieldInfo) -> dict[str, Any]:
    """提取 ge/le/gt/lt 等数值约束, 供前端做即时校验。"""
    out: dict[str, Any] = {}
    for meta in field.metadata:
        for attr, key in (("ge", "min"), ("le", "max"), ("gt", "exclusive_min"), ("lt", "exclusive_max")):
            value = getattr(meta, attr, None)
            if value is not None:
                out[key] = value
    return out


def _options_for_list(default: Any) -> Optional[list[str]]:
    """少数列表字段有语义明确的取值(如代理协议), 目前仅用于占位提示。"""
    return None


def build_config_schema(settings: Settings) -> dict[str, Any]:
    """内省 Settings, 生成前端设置面板所需的完整 Schema。

    返回结构::

        {
          "sections": [
            {"key": "browser", "title": "浏览器", "fields": [
                {"path": "browser.headless", "type": "bool", "default": True, ...}
            ]}
          ]
        }
    """
    schema = settings.model_dump(mode="json")
    cls = type(settings)

    # 分组标题: 与各子配置模块一一对应
    titles = {
        "browser": ("浏览器引擎", "Playwright 引擎、无头模式、视口与会话持久化"),
        "network": ("网络监听", "XHR/fetch/WebSocket 抓包范围与落盘策略"),
        "ai": ("AI 辅助", "自然语言生成规则所用的模型服务(OpenAI 兼容 / Ollama)"),
        "anti_spider": ("反爬与合规", "限速、重试、UA 轮换、代理池与 robots.txt"),
        "storage": ("存储输出", "结果落盘格式、目录与 Webhook 推送"),
        "crawler": ("抓取行为", "分页深度、条数上限、网络静默等待与增量模式"),
        "api": ("调试服务", "FastAPI 调试接口的监听地址"),
    }
    icons = {
        "browser": "globe",
        "network": "activity",
        "ai": "sparkles",
        "anti_spider": "shield",
        "storage": "database",
        "crawler": "layers",
        "api": "plug",
    }

    sections: list[dict[str, Any]] = []
    for name, field in cls.model_fields.items():
        # 注意: 这里不能预先 _unwrap_optional —— 那会丢掉 list[...]/Literal[...]
        # 的泛型信息, 导致容器字段被误判成标量。容器判断用"去掉 None 后是否为
        # BaseModel 子类"来做。
        candidate = _unwrap_optional(field.annotation)

        # ---- 子配置块 -> 一个分组 ----
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            title, desc = titles.get(name, (name, ""))
            fields: list[dict[str, Any]] = []
            for sub_name, sub_field in candidate.model_fields.items():
                path = f"{name}.{sub_name}"
                kind, options = _field_kind(sub_field.annotation)
                if path in TEXTAREA_FIELDS:
                    kind = "textarea"
                fields.append(
                    {
                        "path": path,
                        "name": sub_name,
                        "type": kind,
                        "options": options,
                        "label": _LABELS.get(path, sub_name),
                        "description": sub_field.description or "",
                        "default": schema.get(name, {}).get(sub_name),
                        "sensitive": is_sensitive(path),
                        "restart_required": path in BROWSER_RESTART_FIELDS,
                        "value_options": _options_for_list(schema.get(name, {}).get(sub_name)),
                        **_constraints(sub_field),
                    }
                )
            sections.append(
                {"key": name, "title": title, "description": desc, "icon": icons.get(name, "settings"), "fields": fields}
            )
        else:
            # ---- 顶层标量字段 -> 归入"其他/日志"分组 ----
            kind, options = _field_kind(field.annotation)
            entry = {
                "key": "general",
                "title": "通用与日志",
                "description": "日志级别、日志文件与调试开关",
                "icon": "terminal",
                "fields": [],
            }
            bucket = next((s for s in sections if s["key"] == "general"), None)
            if bucket is None:
                bucket = entry
                sections.append(bucket)
            bucket["fields"].append(
                {
                    "path": name,
                    "name": name,
                    "type": kind,
                    "options": options,
                    "label": _LABELS.get(name, name),
                    "description": field.description or "",
                    "default": schema.get(name),
                    "sensitive": is_sensitive(name),
                    "restart_required": False,
                    **_constraints(field),
                }
            )

    return {"sections": sections, "env_file": str(ENV_FILE), "prefix": ENV_PREFIX}


# 中文字段标签(未覆盖的字段直接显示英文键名)
_LABELS: dict[str, str] = {
    # browser
    "browser.engine": "浏览器引擎",
    "browser.headless": "无头模式",
    "browser.timeout": "导航超时(秒)",
    "browser.user_agent": "自定义 User-Agent",
    "browser.viewport_width": "视口宽度",
    "browser.viewport_height": "视口高度",
    "browser.locale": "语言区域",
    "browser.timezone": "时区",
    "browser.max_pages": "并发页面上限",
    "browser.stealth": "指纹伪装注入",
    "browser.storage_state": "会话持久化文件",
    # network
    "network.capture_resource_types": "捕获的资源类型",
    "network.max_body_size": "响应体上限(字节)",
    "network.body_capture_timeout": "响应体抓取超时(秒)",
    "network.dump_jsonl": "抓包落盘 JSONL",
    "network.jsonl_path": "JSONL 路径",
    "network.ws_capture_frames": "捕获 WebSocket 帧",
    "network.max_records": "内存记录上限",
    # ai
    "ai.enabled": "启用 AI",
    "ai.offline": "强制离线模式",
    "ai.provider": "服务商类型",
    "ai.base_url": "接口地址 Base URL",
    "ai.model": "模型名称",
    "ai.api_key": "API Key",
    "ai.temperature": "采样温度",
    "ai.max_tokens": "最大输出 tokens",
    "ai.timeout": "单次调用超时(秒)",
    "ai.max_retries": "失败重试次数",
    "ai.cache_enabled": "启用响应缓存",
    "ai.cache_path": "缓存文件路径",
    # anti_spider
    "anti_spider.rotate_user_agent": "随机轮换 UA",
    "anti_spider.random_delay_range": "随机限速区间(秒)",
    "anti_spider.max_retries": "请求重试次数",
    "anti_spider.retry_backoff_base": "退避底数",
    "anti_spider.request_timeout": "请求超时(秒)",
    "anti_spider.proxies": "代理池",
    "anti_spider.proxy_failure_cooldown": "代理冷却时间(秒)",
    "anti_spider.respect_robots": "遵守 robots.txt",
    # storage
    "storage.default_format": "默认输出格式",
    "storage.output_dir": "输出目录",
    "storage.sqlite_path": "SQLite 路径",
    "storage.sqlite_table": "SQLite 表名",
    "storage.webhook_url": "结果 Webhook",
    # crawler
    "crawler.max_depth": "最大翻页数",
    "crawler.max_items": "单任务条数上限",
    "crawler.network_settle": "网络静默等待(秒)",
    "crawler.incremental": "增量抓取",
    "crawler.state_path": "增量状态文件",
    "crawler.seen_hash_capacity": "历史哈希容量",
    # api
    "api.host": "监听地址",
    "api.port": "监听端口",
    # general
    "log_level": "日志级别",
    "log_file": "日志文件",
    "debug": "调试模式",
}


# ---------------------------------------------------------------------------
# .env 读写
# ---------------------------------------------------------------------------
class EnvConfigStore:
    """``.env`` 文件的高保真读写(保留注释与顺序)。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else ENV_FILE

    # -- 读 --
    def raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def as_dict(self) -> dict[str, str]:
        """解析 .env 为 {SC_KEY: value}(忽略注释/空行)。"""
        out: dict[str, str] = {}
        for line in self.raw_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out

    # -- 写 --
    def write(self, updates: dict[str, str], removals: Iterable[str] = ()) -> None:
        """把 updates 合并进 .env, 并删除 removals 中的键。

        未涉及的注释与行序原样保留; 新键追加到文件末尾。
        """
        remove_set = {k.upper() for k in removals}
        lines = self.raw_lines()
        written: set[str] = set()
        out: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                out.append(line)
                continue
            key = stripped.partition("=")[0].strip()
            upper = key.upper()
            if upper in remove_set:
                continue  # 删除该行
            if upper in updates:
                out.append(f"{key}={updates[upper]}")
                written.add(upper)
            else:
                out.append(line)

        fresh = {k.upper(): v for k, v in updates.items() if k.upper() not in written}
        if fresh:
            if out and out[-1].strip():
                out.append("")
            out.append("# --- 由 Web 界面写入 ---")
            for key in sorted(fresh):
                out.append(f"{key}={fresh[key]}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")

    # -- 环境变量遮挡检测 --
    def shadowed_keys(self) -> set[str]:
        """返回同时存在于真实环境变量与 .env 的键(真实环境变量优先级更高)。

        这类键在界面上会被标记为"被环境变量锁定", 因为写入 .env 不会生效。
        """
        env_keys = {k.upper() for k in os.environ if k.upper().startswith(ENV_PREFIX)}
        return env_keys & {k.upper() for k in self.as_dict()}


# ---------------------------------------------------------------------------
# 值归一化
# ---------------------------------------------------------------------------
def path_to_env_key(path: str) -> str:
    """``ai.base_url`` -> ``SC_AI__BASE_URL``。"""
    return ENV_PREFIX + path.replace(".", NEST_DELIMITER).upper()


def flatten_settings(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套的配置 dict 摊平成 ``{"ai.base_url": ...}``。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_settings(value, path))
        else:
            out[path] = value
    return out


def coerce_value(kind: str, value: Any) -> Any:
    """按 Schema 声明的类型把界面输入归一化。

    字符串输入支持 JSON 字面量(``[1, 3]`` / ``["a","b"]``)与逗号分隔列表
    (``http://a,http://b``), 前者优先。
    """
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "是")

    if kind in ("int", "float"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value) if kind == "int" else float(value)
        text = str(value).strip()
        if not text:
            raise ValueError("数值不能为空")
        return int(float(text)) if kind == "int" else float(text)

    if kind in ("list", "json"):
        if isinstance(value, (list, dict)):
            return value
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 退化为逗号/换行分隔
            parts = [p.strip().strip('"').strip("'") for p in text.replace("\n", ",").split(",")]
            parsed = [p for p in parts if p]
        if kind == "list":
            if not isinstance(parsed, list):
                raise ValueError("该字段需要一个列表")
            return parsed
        return parsed

    if kind == "enum":
        return str(value).strip()

    return "" if value is None else str(value)


def encode_env_literal(kind: str, value: Any) -> str:
    """把规范化后的值编码成 .env 里的字面量。

    list/dict 用 JSON(pydantic-settings 会优先按 JSON 解析, 见 config._coerce_for_model);
    bool 用 true/false; 其余原样。
    """
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("list", "json"):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
