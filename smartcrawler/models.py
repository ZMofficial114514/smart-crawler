"""
SmartCrawler 数据模型模块。

全部基于 Pydantic v2 定义, 覆盖框架内部流转的所有核心数据结构:
- 网络捕获: NetworkRecord / WebSocketRecord
- 页面结构: PageStructureReport / ListCandidate
- 提取规则: ExtractionRule / ListRule / PaginationRule / FieldSpec
- 输出结果: ExtractedItem / TaskResult

规则模型同时承担两个职责:
1. AI 生成的提取规则的"落点"(AI 输出 JSON -> 校验为 ExtractionRule);
2. 规则引擎 / 手写规则的统一格式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 1. 网络监听相关
# ---------------------------------------------------------------------------
class NetworkRecord(BaseModel):
    """单条被捕获的 XHR/fetch 请求 + 响应记录。"""

    id: str = Field(description="记录唯一 ID")
    timestamp: float = Field(description="请求发起时的 Unix 时间戳(秒)")

    # ---- 请求侧 ----
    url: str
    method: str = "GET"
    resource_type: str = Field(default="xhr", description="资源类型: xhr/fetch/websocket")
    request_headers: dict[str, str] = Field(default_factory=dict)
    post_data: Optional[str] = Field(default=None, description="POST 请求体")
    page_url: str = Field(default="", description="发起请求所在的页面 URL")
    frame_url: str = Field(default="", description="发起请求所在的 frame URL")
    trigger_selector: Optional[str] = Field(
        default=None, description="触发元素选择器(Playwright 不直接暴露, 预留字段)"
    )

    # ---- 响应侧 ----
    status: Optional[int] = Field(default=None, description="HTTP 状态码")
    mime_type: str = ""
    response_headers: dict[str, str] = Field(default_factory=dict)
    body_text: Optional[str] = Field(default=None, description="响应体原文(JSON/文本)")
    body_json: Any = Field(default=None, description="响应体自动解析出的 JSON(若可解析)")
    duration_ms: Optional[float] = Field(default=None, description="请求耗时(毫秒)")
    failed: bool = Field(default=False, description="请求是否失败")
    error: Optional[str] = Field(default=None, description="失败原因")

    def matches(
        self,
        url_pattern: Optional[str] = None,
        mime_contains: Optional[str] = None,
        status: Optional[int] = None,
    ) -> bool:
        """简单的过滤谓词, 供查询接口复用。"""
        import re

        if url_pattern and not re.search(url_pattern, self.url):
            return False
        if mime_contains and mime_contains.lower() not in self.mime_type.lower():
            return False
        if status is not None and self.status != status:
            return False
        return True


class WebSocketFrame(BaseModel):
    """WebSocket 单帧消息。"""

    direction: Literal["send", "recv"] = "recv"
    payload: str = ""
    timestamp: float = 0.0


class WebSocketRecord(BaseModel):
    """一条 WebSocket 连接及其全部帧。"""

    url: str
    page_url: str = ""
    frames: list[WebSocketFrame] = Field(default_factory=list)
    closed: bool = False


# ---------------------------------------------------------------------------
# 2. 页面结构报告
# ---------------------------------------------------------------------------
class ListCandidate(BaseModel):
    """自动识别出的"疑似列表"候选区。"""

    item_selector: str = Field(description="列表项候选 CSS 选择器")
    container_selector: str = Field(default="", description="列表容器选择器")
    count: int = Field(default=0, description="识别到的重复项数量")
    sample_fields: list[dict[str, Any]] = Field(
        default_factory=list,
        description="从首个样本项推断的字段 [{name, selector, attribute}]",
    )
    sample_html: str = Field(default="", description="首个样本项的 HTML 片段(截断)")


class PaginationInfo(BaseModel):
    """自动识别出的分页器信息。"""

    next_selector: Optional[str] = Field(default=None, description="下一页元素选择器")
    next_text: Optional[str] = None
    next_href: Optional[str] = None


class PageStructureReport(BaseModel):
    """页面结构分析报告(JSON 可直接落盘)。"""

    url: str
    title: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    simplified_tree: str = Field(default="", description="简化 DOM 树(文本形式, 有节点数上限)")
    candidate_lists: list[ListCandidate] = Field(default_factory=list, description="候选列表区")
    pagination: Optional[PaginationInfo] = None
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="结构化元数据: json_ld / open_graph / microdata",
    )
    dom_stats: dict[str, int] = Field(default_factory=dict, description="DOM 统计: 节点数/链接数等")
    access_issue: Optional["AccessIssue"] = Field(
        default=None,
        description="访问受限诊断(登录/权限/风控/验证码…), 含页面实际文本, 用于在界面上提醒用户",
    )
    login_state: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "登录状态判定(logged_in/anonymous/unknown + 置信度 + 依据)。"
            "有些站点登录前后页面结构完全不同, 若不区分会把匿名引导页误判成「没有列表」"
        ),
    )
    challenge: Optional[dict[str, Any]] = Field(
        default=None, description="页面是否正在要求人机验证(界面据此提供手动过验证入口)"
    )
    lazy_load: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "懒加载滚动的结果(轮次/新增节点/是否仍在增长)。"
            "infinite=True 表示内容没有上限, 界面应询问用户是否继续向下滚动"
        ),
    )


# ---------------------------------------------------------------------------
# 2.5 页面文本元素
# ---------------------------------------------------------------------------
class TextElement(BaseModel):
    """重定向后页面上的一个文本元素(用于给用户看"你到底被带到了哪一页")。"""

    tag: str = Field(default="", description="标签名, 如 h1/form/button")
    selector: str = Field(default="", description="该元素的选择器")
    text: str = Field(default="", description="可见文本(已截断)")
    role: str = Field(default="", description="语义角色: heading/form/button/message/captcha")


# ---------------------------------------------------------------------------
# 2.6 访问受限诊断
# ---------------------------------------------------------------------------
#: 访问受限的类型。区分这些类型的价值在于: 它们的表象都是"0 条数据", 但处置方式
#: 完全不同 —— 把"没有权限"误判成"需要登录"会让用户往完全错误的方向排查。
ACCESS_ISSUE_TYPES = (
    "login_required",      # 需要登录
    "permission_denied",   # 已登录但无权限(如洛谷训练页: HTTP 401 + "没有权限请求此资源。")
    "risk_control",        # 风控/安全策略拦截
    "captcha",             # 人机验证
    "rate_limited",        # 请求过于频繁
    "server_error",        # 5xx
    "not_found",           # 404
    "spa_shell",           # 内容未渲染(空壳)
    "empty_page",          # 页面正常但没有列表
    "unknown",             # 有异常但无法归类
)


class AccessIssue(BaseModel):
    """访问受限诊断: 分类 + 置信度 + **页面实际显示的内容**。

    设计意图: 用户看到"0 条"时最需要知道两件事 ——
    ①到底属于哪类问题(决定怎么修); ②页面究竟写了什么(用于人工判断)。
    因此这里既给分类与逐条依据, 也把页面文本、关键元素、错误码、元数据一并带上。
    """

    detected: bool = Field(default=False, description="是否判定存在访问受限")
    issue_type: str = Field(default="unknown", description="类型: " + " / ".join(ACCESS_ISSUE_TYPES))
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="置信度 0~1")

    # ---- 请求与结果 ----
    requested_url: str = ""
    final_url: str = ""
    redirected: bool = Field(default=False, description="是否发生实质跳转(忽略锚点变化)")
    http_status: Optional[int] = None

    # ---- 结论 ----
    title: str = Field(default="", description="人类可读的结论标题")
    explanation: str = Field(default="", description="一句话解释这是什么问题")
    reasons: list[str] = Field(default_factory=list, description="判定依据逐条列出")
    scores: dict[str, float] = Field(default_factory=dict, description="各候选类型的得分(便于理解归因)")
    clues: dict[str, Any] = Field(default_factory=dict, description="结构性线索: 密码框/表单/验证码等")
    suggestions: list[str] = Field(default_factory=list, description="按类型给出的处置建议")

    # ---- 页面实际内容(排查问题的关键) ----
    page_title: str = Field(default="", description="document.title")
    visible_text: str = Field(default="", description="整页可见正文(已截断)")
    main_text: str = Field(default="", description="剥离导航/页脚后的主内容区文本")
    text_elements: list[TextElement] = Field(
        default_factory=list, description="关键文本元素(标题/错误块/表单/按钮/验证码…)"
    )
    error_codes: dict[str, str] = Field(default_factory=dict, description="提取到的错误码/请求 ID")
    metadata: dict[str, Any] = Field(default_factory=dict, description="meta 标签与页面初始化变量")
    dom_nodes: int = Field(default=0, description="DOM 节点数(用于判断空壳)")
    html_length: int = Field(default=0, description="HTML 长度")

    def summary(self) -> str:
        """一句话摘要, 便于写进日志或任务错误列表。"""
        if not self.detected:
            return "未检测到访问受限"
        return f"{self.title or self.issue_type}({self.confidence:.0%})"


# ---------------------------------------------------------------------------
# 2.7 插件产物
# ---------------------------------------------------------------------------
class DownloadedFile(BaseModel):
    """插件下载到本地的文件(图片/音频/附件等)。"""

    url: str
    path: str = Field(default="", description="本地绝对路径")
    relative_path: str = Field(default="", description="相对项目根目录的路径, 便于界面展示")
    filename: str = ""
    size: int = Field(default=0, description="字节数")
    mime_type: str = ""
    ok: bool = Field(default=True)
    error: Optional[str] = None
    plugin_id: str = Field(default="", description="产生该文件的插件")
    source_item_index: Optional[int] = Field(default=None, description="来自第几条记录")


# ---------------------------------------------------------------------------
# 3. 提取规则
# ---------------------------------------------------------------------------
class FieldSpec(BaseModel):
    """单个字段的提取定义。

    selector 的解释方式由前缀决定(与具体提取器约定一致):
    - CSS 选择器: 默认, 如 ".price_color"
    - XPath: 以 "//"、"(//"、"./" 或 "xpath=" 开头, 如 "//span[@class='price']"
    - JSONPath: 以 "$" 开头, 如 "$.data.list[*].title" (仅 mode="json" 时使用)
    - 正则: 以 "re:" 开头, 对外层文本做正则提取
    """

    name: str = Field(description="字段名(映射到输出 dict 的 key)")
    selector: str = Field(description="CSS/XPath/JSONPath 选择器")
    attribute: Optional[str] = Field(
        default=None, description="取属性值: None=textContent, 'href'/'src'/'html' 等"
    )
    transform: list[str] = Field(
        default_factory=list,
        description="清洗管线, 可选: strip/int/float/price/date/url/lower/upper/json/regex:xxx",
    )
    required: bool = Field(default=False, description="是否必填(缺失则丢弃整条记录)")


class ListRule(BaseModel):
    """列表页提取规则。"""

    item_selector: str = Field(description="列表项选择器(CSS 或 XPath; json 模式下为 JSONPath)")
    fields: list[FieldSpec] = Field(default_factory=list)


class PaginationRule(BaseModel):
    """分页规则。"""

    next_selector: Optional[str] = Field(default=None, description="下一页按钮选择器")
    max_pages: int = Field(default=1, ge=1, description="最多翻页数(含首页)")


class ExtractionRule(BaseModel):
    """一次抓取任务的完整提取规则。"""

    mode: Literal["dom", "json"] = Field(
        default="dom", description="dom=在页面 DOM 上提取; json=在捕获的 XHR JSON 上提取"
    )
    list_rule: Optional[ListRule] = None
    pagination: Optional[PaginationRule] = None
    source: Literal["ai", "rule", "manual"] = Field(
        default="manual", description="规则来源: ai=AI 生成 / rule=规则引擎 / manual=手写"
    )
    notes: str = Field(default="", description="规则说明(AI 会在这里写推理理由)")


# ---------------------------------------------------------------------------
# 4. 输出结果
# ---------------------------------------------------------------------------
class ExtractedItem(BaseModel):
    """单条提取结果(带溯源信息与内容哈希, 用于去重/增量)。"""

    data: dict[str, Any]
    source_url: str = ""
    captured_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    content_hash: str = ""


class TaskResult(BaseModel):
    """一次 crawl 任务的完整结果(含调试信息)。"""

    task_id: str
    url: str
    goal: Optional[str] = None
    success: bool = False
    items: list[dict[str, Any]] = Field(default_factory=list)
    item_count: int = 0
    pages_crawled: int = 0
    network_record_count: int = 0
    websocket_record_count: int = 0
    rule: Optional[ExtractionRule] = None
    structure_report: Optional[PageStructureReport] = None
    errors: list[str] = Field(default_factory=list)
    saved_to: Optional[str] = None
    started_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: Optional[str] = None
    duration_ms: float = 0.0

    # ---- 访问诊断与插件 ----
    access_issue: Optional[AccessIssue] = Field(
        default=None, description="访问受限诊断(界面据此弹出醒目提示)"
    )
    login_state: Optional[dict[str, Any]] = Field(
        default=None, description="登录状态判定(匿名/已登录), 界面据此询问是否登录"
    )
    challenge: Optional[dict[str, Any]] = Field(
        default=None,
        description="人机验证/风控挑战(需用户手动完成), 界面据此提供『手动过验证』入口",
    )
    lazy_load: Optional[dict[str, Any]] = Field(
        default=None, description="懒加载滚动结果(是否仍在增长/新增了多少内容)"
    )
    overlays: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "登录浮层/遮罩的自动关闭结果。很多站点(堆糖典型)弹登录框, 但内容其实已经在"
            "页面里 —— 关掉遮罩才抓得到东西"
        ),
    )
    plugins_used: list[str] = Field(default_factory=list, description="本次实际执行的插件 id")
    downloads: list[DownloadedFile] = Field(
        default_factory=list, description="插件下载的文件(图片/音频等)"
    )
    plugin_errors: list[str] = Field(default_factory=list, description="插件执行失败信息")
