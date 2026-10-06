"""
Web API 的请求/响应模型。

集中放在这里的好处: FastAPI 会自动生成 OpenAPI 文档(/docs), 字段约束(ge/le、
正则)在进入路由之前就完成校验, 前端也能从同一份定义里对齐语义。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class CrawlRequest(BaseModel):
    """POST /api/crawl —— 一次抓取任务的全部可调参数。"""

    url: str = Field(description="目标页面 URL")
    scope: str = Field(
        default="",
        description=(
            "限定分析区域的选择器 —— **只在这个区域内找候选列表并生成规则**, 用于"
            "\"我只想爬页面的一部分\"。留空表示整页。写法: CSS 选择器(如 "
            "`div#content`), 或界面里点选元素后回填的选择器。"
        ),
    )
    goal: Optional[str] = Field(default=None, description="自然语言抓取目标(启用 AI 时生效)")
    rule: Optional[dict[str, Any] | str] = Field(
        default=None, description="显式提取规则(对象或 JSON 字符串), 优先级高于 goal"
    )
    format: Optional[Literal["json", "jsonl", "csv", "sqlite"]] = Field(
        default=None, description="输出格式; 留空则不下发到磁盘(仍可在界面预览与下载)"
    )
    output: Optional[str] = Field(default=None, description="输出路径; 留空则按时间自动命名到输出目录")
    max_pages: Optional[int] = Field(default=None, ge=1, le=200, description="最多翻页数")
    incremental: bool = Field(default=False, description="增量模式: 只输出新增内容")
    use_ai: bool = Field(default=True, description="允许 AI 生成提取规则")
    wait: float = Field(default=0.0, ge=0.0, le=120.0, description="页面加载后额外等待秒数")
    plugin_ids: Optional[list[str]] = Field(
        default=None,
        description="只运行这些插件; 留空则按插件自身的启用配置执行",
    )
    deep_scroll: int = Field(
        default=0,
        ge=0,
        le=200,
        description=(
            "额外的懒加载滚动轮次。用于无限流页面: 首次抓取按配置上限滚完后若内容"
            "仍在增长, 界面会询问用户, 用户选择继续时把轮次传进来"
        ),
    )
    #: 用户自己设定的滚动轮数(留空 = 用配置里的 lazy_load_max_rounds)。
    #: 0 表示完全不滚动。
    scroll_rounds: Optional[int] = Field(
        default=None, ge=0, le=200, description="滚动加载轮数(0=不滚动; 留空用配置默认)"
    )
    #: 用户选择"继续"时每次追加的轮数(留空用配置默认)
    scroll_continue_rounds: Optional[int] = Field(
        default=None, ge=1, le=200, description="续滚时每次追加的轮数"
    )
    #: 是否在"滚完仍增长"时询问用户是否继续(默认开)
    ask_scroll: bool = Field(
        default=True, description="内容无上限时询问用户是否继续滚动(直到用户选择停止)"
    )
    #: 本次任务下载多少张图片/多少个音频(图片下载器、音乐下载器共用)。
    #: 留空则: 先从抓取目标里解析("爬取前三张"), 再退回插件自身配置。
    media_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=5000,
        description="本次任务最多下载几张图片/几个音频(留空则按抓取目标或插件配置)",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("URL 不能为空")
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        return value


class AnalyzeRequest(BaseModel):
    """POST /api/analyze —— 页面结构分析。"""

    url: str
    scope: str = Field(
        default="",
        description=(
            "限定分析区域的选择器。—— 只在该区域内找候选列表、只画该区域的结构树。"
            "用于\"我只想爬页面的一部分\": 页头导航/侧栏推荐/页脚链接不会再混进候选。"
            "留空 = 整页。选择器没匹配到元素时会按整页分析, 并在报告的 scope_matched 标记。"
        ),
    )
    deep_scroll: int = Field(
        default=0,
        ge=0,
        le=200,
        description="额外的懒加载滚动轮次(无限流页面由用户选择继续时传入)",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        return value


class RequestsRequest(BaseModel):
    """POST /api/requests —— 网络抓包(找隐藏 API 用)。"""

    url: str
    wait: float = Field(default=5.0, ge=0.5, le=120.0, description="捕获等待秒数")
    scroll: bool = Field(default=True, description="先模拟滚动触发懒加载")
    pattern: Optional[str] = Field(default=None, description="URL 正则过滤")
    mime: Optional[str] = Field(default=None, description="MIME 包含过滤")
    status: Optional[int] = Field(default=None, description="HTTP 状态码过滤")
    has_json: bool = Field(default=False, description="只看返回 JSON 的请求")
    resource: Optional[str] = Field(default=None, description="资源类型过滤: xhr/fetch/websocket")
    limit: int = Field(default=200, ge=1, le=2000, description="返回条数上限")

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://", "file://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        return value


class RuleValidateRequest(BaseModel):
    """POST /api/rules/validate —— 规则校验(不执行抓取)。"""

    rule: dict[str, Any] | str


class RuleRepairRequest(BaseModel):
    """POST /api/rules/repair —— 让 AI 修复失效的选择器。"""

    url: str
    selector: str = Field(description="失效的选择器")
    goal: Optional[str] = Field(default=None, description="该字段的语义, 帮助 AI 理解意图")


class ConfigPatchRequest(BaseModel):
    """POST /api/config —— 应用一批配置改动。"""

    patch: dict[str, Any] = Field(description='形如 {"browser.headless": false}')
    persist: bool = Field(default=True, description="是否写入 .env(否则仅本次会话生效)")


class CleanDataRequest(BaseModel):
    """POST /api/ai/clean —— 用 AI 清洗/归一化已有记录。"""

    items: list[dict[str, Any]] = Field(description="待清洗的原始记录")
    schema_: dict[str, str] = Field(
        alias="schema", description='目标字段说明, 如 {"title": "商品标题", "price": "价格数字"}'
    )

    model_config = {"populate_by_name": True}
