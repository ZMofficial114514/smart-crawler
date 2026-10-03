"""
SmartCrawler 配置模块。

配置优先级(从高到低):
1. 真实环境变量           SC_BROWSER__HEADLESS=false  (双下划线表示层级)
2. .env 文件              位于运行目录, 同样使用 SC_ 前缀
3. YAML 配置文件          通过环境变量 SC_CONFIG_FILE 或 Settings.load("cfg.yaml") 指定
4. 代码内默认值

敏感信息(API Key / 代理)一律从环境变量读取, 严禁硬编码。

设计说明:
- 嵌套配置块使用普通 Pydantic BaseModel(类型校验 + 默认值);
- Settings 继承 pydantic-settings 的 BaseSettings, 原生支持 .env 与环境变量;
- 引入 YAML 时, 手工按 优先级 合并后再 model_validate,
  并对"环境变量里写 JSON 数组"的情况做类型矫正(如 SC_ANTI_SPIDER__PROXIES='["http://..."]')。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # python-dotenv 是 pydantic-settings 的传递依赖, 这里显式使用
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - 极端环境降级
    dotenv_values = None

try:
    import yaml
except ImportError:  # pragma: no cover - 未装 PyYAML 时仅禁用 YAML 来源
    yaml = None

ENV_PREFIX = "SC_"
NEST_DELIMITER = "__"

# ---------------------------------------------------------------------------
# 项目根目录
# ---------------------------------------------------------------------------
# config.py 位于 <ROOT>/smartcrawler/config.py, 故上溯两级即为项目根。
# 用途: 让 .env / data / logs 等相对路径锚定在项目根, 而不是进程的工作目录 ——
# 否则从任意目录启动 Web 服务都读不到同一份 .env。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# 各子配置块
# ---------------------------------------------------------------------------
class BrowserConfig(BaseModel):
    """浏览器自动化配置 (browser.py)。"""

    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    timeout: float = 30.0  # 页面导航默认超时(秒)
    user_agent: str = ""  # 留空则由反爬模块随机选取
    viewport_width: int = 1366
    viewport_height: int = 768
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    max_pages: int = 4  # 同时打开的页面数上限(asyncio.Semaphore)
    stealth: bool = True  # 是否注入指纹伪装脚本
    #: 会话持久化文件(JSON)。默认指向 data/session.json —— 界面上的「登录一次」就保存在
    #: 这里, 因此用户登录完**不需要再改任何配置**就能复用。文件不存在时按匿名访问。
    #: 想改路径或彻底停用(留空)都可以。
    storage_state: str = "data/session.json"


class NetworkConfig(BaseModel):
    """网络监听配置 (network.py)。"""

    capture_resource_types: list[str] = Field(
        default_factory=lambda: ["xhr", "fetch", "websocket"],
        description="只捕获这些资源类型, 排除 image/css/font/script 等",
    )
    max_body_size: int = 1_000_000  # 单个响应体最大捕获字节数, 超出截断
    body_capture_timeout: float = 10.0  # 抓响应体的超时(秒)
    dump_jsonl: bool = False  # 是否把捕获记录落盘(JSON Lines)
    jsonl_path: str = "data/network.jsonl"
    ws_capture_frames: bool = True  # 是否捕获 WebSocket 帧
    max_records: int = 5000  # 内存中最多保留的记录数(防 OOM, 超出丢弃最旧)


class AIConfig(BaseModel):
    """AI 辅助配置 (ai.py)。

    provider="openai"  : 任意 OpenAI 兼容接口 —— OpenAI / DeepSeek / 通义千问 /
                         智谱 GLM / Moonshot / 硅基流动等, 改 base_url 即可。
    provider="ollama"  : 本地 Ollama, 默认 http://localhost:11434/v1。
    api_key 优先读 SC_AI__API_KEY, 其次回落到 OPENAI_API_KEY / DEEPSEEK_API_KEY 等通用变量。
    """

    enabled: bool = True  # 总开关
    offline: bool = False  # True=强制离线模式(纯规则引擎, 不发起任何 AI 调用)
    provider: Literal["openai", "ollama"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""  # 从环境变量读取, 不要硬编码
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout: float = 60.0  # 单次 AI 调用超时(秒)
    max_retries: int = 2
    cache_enabled: bool = True  # 缓存 AI 响应, 相同输入不重复计费
    cache_path: str = "data/ai_cache.json"

    def effective_base_url(self) -> str:
        """规范化 base_url: 补全缺失的 /v1 版本段, 去掉尾部斜杠。

        多数 OpenAI 兼容服务商同时提供 ``https://host`` 与 ``https://host/v1`` 两个
        入口, 而 ``/chat/completions`` 挂在版本段之下。用户复制控制台里的裸域名是
        很常见的失误, 这里统一纠正, 避免 404。
        """
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            return "https://api.openai.com/v1"
        # 已经带版本段(如 /v1、/v1beta、/compatible-mode/v1)则原样使用
        if re.search(r"/v\d+[a-z]*$", base):
            return base
        return f"{base}/v1"

    def resolve_api_key(self) -> str:
        """解析 API Key: 专用配置 -> 通用环境变量。"""
        if self.api_key:
            return self.api_key
        for name in (
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "DASHSCOPE_API_KEY",
            "ZHIPUAI_API_KEY",
            "MOONSHOT_API_KEY",
            "SILICONFLOW_API_KEY",
            "OLLAMA_API_KEY",
        ):
            v = os.getenv(name, "")
            if v:
                return v
        return ""


class AntiSpiderConfig(BaseModel):
    """反爬与稳定性配置 (anti_spider.py)。"""

    rotate_user_agent: bool = True  # 每次新建页面随机 UA
    random_delay_range: list[float] = Field(
        default_factory=lambda: [1.0, 3.0],
        description="每次导航前的随机延时区间(秒), 默认 1~3 秒限速",
    )
    max_retries: int = 3  # 请求最大重试次数
    retry_backoff_base: float = 2.0  # 指数退避底数: sleep = base ** attempt + jitter
    request_timeout: float = 30.0
    proxies: list[str] = Field(
        default_factory=list,
        description="代理池, 如 ['http://u:p@host:port', 'socks5://host:port']; 也支持环境变量 PROXY_POOL 逗号分隔",
    )
    proxy_failure_cooldown: float = 300.0  # 代理失败后的冷却秒数
    respect_robots: bool = True  # 是否遵守 robots.txt(合规开关, 默认遵守)


class StorageConfig(BaseModel):
    """存储与输出配置 (storage.py)。"""

    default_format: Literal["json", "jsonl", "csv", "sqlite"] = "json"
    output_dir: str = "data"
    sqlite_path: str = "data/smartcrawler.db"
    sqlite_table: str = "items"
    webhook_url: str = ""  # 抓取完成后 POST 结果(JSON), 留空不推送


class CrawlerConfig(BaseModel):
    """主爬虫编排配置 (crawler.py)。"""

    max_depth: int = Field(default=1, ge=1, description="分页抓取的最大页数")
    max_items: int = 500  # 单任务最大条数
    network_settle: float = 2.5  # 页面加载后额外等待网络请求的时间(秒, json 模式尤其重要)
    #: 抓取前模拟滚动以触发懒加载。现代 SPA 首屏只渲染骨架, 不滚就只能拿到"看起来完整
    #: 的空页面"(导航在、主内容区是空容器)。实测 pixiv 登录后首页: 不滚 0 个作品链接,
    #: 滚动后 70 个。关掉它可加快速度, 但会漏掉懒加载内容。
    lazy_load_scroll: bool = Field(default=True, description="抓取前模拟滚动触发懒加载")
    #: 第一段的滚动轮次预算。滚满后若内容仍在增长, 见下面的"询问是否继续"。
    lazy_load_max_rounds: int = Field(default=8, ge=1, le=60, description="懒加载滚动的最大轮次")
    #: 每轮的滚动步长(视口高度的倍数): 1.4 约等于"一屏多一点"
    lazy_load_step_ratio: float = Field(
        default=1.4, ge=0.3, le=5.0, description="每轮滚动步长(视口倍数)"
    )
    #: 滚满一段后内容仍在增长时, **询问用户是否继续**, 直到用户选择停止。
    #: 关掉它就退化为"只滚 lazy_load_max_rounds 轮, 然后如实报告仍在增长"。
    lazy_load_ask_continue: bool = Field(
        default=True, description="无上限时询问用户是否继续滚动(直到用户选择停止)"
    )
    #: 用户每次选择"继续"时追加的轮次
    lazy_load_continue_rounds: int = Field(
        default=10, ge=1, le=200, description="每次确认继续时追加的滚动轮次"
    )
    #: 等待用户回话的秒数; 超时按"停止"处理(用户可能已经离开页面)
    lazy_load_ask_timeout: float = Field(
        default=120.0, ge=5.0, le=1800.0, description="等待用户确认续滚的秒数"
    )
    incremental: bool = False  # 增量模式: 基于内容哈希只保留新增
    state_path: str = "data/state.json"  # 增量状态(已见哈希)落盘位置
    seen_hash_capacity: int = 20000  # 状态文件中最多保留多少历史哈希


class APIConfig(BaseModel):
    """FastAPI 调试服务配置 (api.py)。"""

    host: str = "127.0.0.1"
    port: int = 8322


# ---------------------------------------------------------------------------
# 根配置
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """SmartCrawler 根配置。"""

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    anti_spider: AntiSpiderConfig = Field(default_factory=AntiSpiderConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    api: APIConfig = Field(default_factory=APIConfig)

    log_level: str = "INFO"
    log_file: str = "logs/smartcrawler.log"
    debug: bool = False

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=NEST_DELIMITER,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # YAML + 环境变量合并加载
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, yaml_path: Union[str, Path, None] = None) -> "Settings":
        """加载配置: 默认值 < YAML < .env < 环境变量。

        未指定 yaml_path 时, 尝试读取环境变量 SC_CONFIG_FILE 指向的 YAML。
        """
        yaml_path = yaml_path or os.getenv("SC_CONFIG_FILE")
        if not yaml_path or yaml is None:
            return cls()  # 纯环境变量 / .env / 默认值

        file = Path(yaml_path)
        if not file.exists():
            raise FileNotFoundError(f"YAML 配置文件不存在: {file}")

        with file.open("r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

        merged = _deep_merge(yaml_data, _collect_layered_env())
        coerced = _coerce_for_model(cls, merged)
        return cls.model_validate(coerced)


# ---------------------------------------------------------------------------
# 内部工具: 深合并 / 环境变量收集 / 类型矫正
# ---------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典: override 优先。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _collect_from_mapping(mapping: dict) -> dict:
    """把 {SC_AI__MODEL: ...} 形式的扁平键还原成嵌套 dict。"""
    nested: dict = {}
    for key, value in mapping.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split(NEST_DELIMITER)
        node = nested
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return nested


def _collect_layered_env() -> dict:
    """收集环境变量层: 真实环境变量覆盖 .env 文件。"""
    dotenv_layer = {}
    if dotenv_values is not None and ENV_FILE.exists():
        dotenv_layer = _collect_from_mapping(
            {k.upper(): v for k, v in (dotenv_values(str(ENV_FILE)) or {}).items() if v is not None}
        )
    env_layer = _collect_from_mapping({k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)})
    return _deep_merge(dotenv_layer, env_layer)


def _coerce_for_model(model_cls: type[BaseModel], data: dict) -> dict:
    """环境变量中字符串形式的 list/dict -> JSON 解析(如代理列表)。"""
    from typing import get_origin

    out: dict = {}
    for key, value in data.items():
        field = model_cls.model_fields.get(key)
        if field is None:
            out[key] = value
            continue
        annotation = field.annotation
        origin = get_origin(annotation)
        if isinstance(value, str) and origin in (list, dict, tuple):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass  # 保留原值, 交给 Pydantic 报清晰的错误
        if isinstance(value, dict) and isinstance(annotation, type) and issubclass(annotation, BaseModel):
            value = _coerce_for_model(annotation, value)
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_settings: Optional[Settings] = None


def get_settings(yaml_path: Union[str, Path, None] = None, reload: bool = False) -> Settings:
    """获取全局配置单例; 测试或需要重载时传 reload=True。"""
    global _settings
    if _settings is None or reload or yaml_path:
        _settings = Settings.load(yaml_path)
    return _settings
