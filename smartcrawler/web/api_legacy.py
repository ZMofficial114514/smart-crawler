"""
SmartCrawler FastAPI 调试接口。

启动: python -m smartcrawler serve   (或 uvicorn smartcrawler.api:app --port 8322)

接口一览(文档见 /docs):
    GET  /health      健康检查
    GET  /requests    查看已捕获的请求列表(URL 正则/MIME/状态码过滤)
    GET  /structure   对指定 URL 做页面结构分析
    POST /extract     传 URL + 自然语言目标(或显式规则), 返回提取结果
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings, get_settings
from ..models import ExtractionRule, TaskResult
from ..utils import safe_json_loads, setup_logging


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_file)

    state: dict[str, Any] = {"crawler": None, "last_report": None, "last_rule": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动: 懒创建爬虫实例(浏览器在首个请求时才启动)
        from ..crawler import SmartCrawler

        state["crawler"] = SmartCrawler(settings)
        yield
        # 关闭: 释放浏览器
        crawler: SmartCrawler | None = state.get("crawler")
        if crawler:
            await crawler.close()

    app = FastAPI(
        title="SmartCrawler 调试接口",
        description="智能爬虫框架调试 API —— 仅限合法授权的数据采集",
        version=__version__,
        lifespan=lifespan,
    )

    def crawler() -> Any:
        assert state["crawler"] is not None
        return state["crawler"]

    # ------------------------------------------------------------------
    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "ai_available": crawler().ai.available,
            "proxy_pool_size": crawler().browser._proxy_pool.size,
        }

    @app.get("/requests", tags=["debug"])
    async def list_requests(
        url_pattern: Optional[str] = Query(default=None, description="URL 正则过滤"),
        mime: Optional[str] = Query(default=None, description="MIME 包含过滤"),
        status: Optional[int] = Query(default=None),
        has_json: Optional[bool] = Query(default=None, description="只看含 JSON 响应的请求"),
        limit: int = Query(default=50, le=500),
    ) -> dict[str, Any]:
        """查看本进程内捕获的网络请求(需先通过 /extract 或 /structure 触发一次抓取)。"""
        records = crawler().recorder.query(
            url_pattern=url_pattern, mime_contains=mime, status=status, has_json=has_json, limit=limit
        )
        return {
            "stats": crawler().recorder.stats(),
            "records": [r.model_dump(exclude_none=True) for r in records],
        }

    @app.get("/structure", tags=["debug"])
    async def structure(url: str) -> dict[str, Any]:
        """对 URL 做结构分析, 返回候选列表/分页/元数据/网络摘要。"""
        report, stats = await crawler().analyze_only(url)
        if report is None:
            raise HTTPException(status_code=502, detail=f"页面分析失败: {url}")
        state["last_report"] = report
        return {
            "report": report.model_dump(),
            "network_stats": stats,
        }

    @app.post("/extract", response_model=TaskResult, tags=["crawl"])
    async def extract(req: ExtractRequest) -> TaskResult:
        """传入 URL 和自然语言目标(或显式规则), 执行抓取并返回结果。"""
        rule: ExtractionRule | None = None
        if req.rule:
            data = req.rule if isinstance(req.rule, dict) else safe_json_loads(req.rule)
            if not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="rule 必须是对象或 JSON 字符串")
            try:
                rule = ExtractionRule.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"规则校验失败: {exc}") from exc

        result = await crawler().crawl(
            url=req.url,
            goal=req.goal,
            rule=rule,
            format=req.format,
            output=req.output,
            max_pages=req.max_pages,
            use_ai=req.use_ai,
            extra_wait=req.wait,
        )
        if result.rule:
            state["last_rule"] = result.rule
        return result

    return app


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    """POST /extract 请求体。"""

    url: str = Field(description="目标页面 URL")
    goal: Optional[str] = Field(default=None, description='自然语言目标, 如 "抓取所有商品名称和价格"')
    rule: Optional[dict[str, Any] | str] = Field(
        default=None, description="显式 ExtractionRule(对象或 JSON 字符串), 优先于 goal"
    )
    format: Optional[str] = Field(default=None, description="输出格式 json/jsonl/csv/sqlite; 不落盘则留空")
    output: Optional[str] = Field(default=None, description="输出路径")
    max_pages: Optional[int] = Field(default=None, ge=1, description="最大翻页数")
    use_ai: bool = Field(default=True, description="是否允许 AI 生成规则")
    wait: float = Field(default=0.0, description="页面加载后额外等待秒数")


# uvicorn 直接启动入口
app = create_app()
