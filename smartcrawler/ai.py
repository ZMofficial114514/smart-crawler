"""
SmartCrawler AI 辅助模块。

分层设计:
- AIClient : 底层 OpenAI 兼容 HTTP 客户端(httpx 直连)。
    * 支持 OpenAI / DeepSeek / 通义千问 / 智谱 GLM / Moonshot / 硅基流动 等国产模型
      (均为 OpenAI 兼容协议, 修改 base_url 即可切换);
    * 支持本地 Ollama(http://localhost:11434/v1);
    * 全部调用带超时 + 重试 + 指数退避;
    * 响应缓存(相同输入不重复计费)。
- AIEngine : 面向爬虫的四大能力, 输入输出均为强类型 Pydantic 模型:
    * analyze_page(html/结构报告, goal)      -> ExtractionRule(CSS/XPath/JSONPath)
    * extract_from_json(json_data, goal)     -> 字段映射(JSONPath)
    * repair_selector(old_selector, new_html)-> 修复后的选择器
    * clean_data(raw_data, schema)           -> 实体识别/分类/归一化后的数据

离线模式: AIConfig.offline=True 或未配置 Key 或调用失败时, 各方法返回 None,
由 crawler 降级到规则引擎(structure.build_rule_from_structure)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from .config import AIConfig, Settings
from .models import ExtractionRule, FieldSpec, ListRule, PageStructureReport, PaginationRule
from .utils import extract_json_block, truncate

# ---------------------------------------------------------------------------
# 底层客户端
# ---------------------------------------------------------------------------
class AIClient:
    """OpenAI 兼容接口客户端(含缓存)。"""

    def __init__(self, config: AIConfig) -> None:
        self.config = config
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded = False

    # -- 缓存 --
    @staticmethod
    def _cache_key(
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float,
        model: str = "",
        base_url: str = "",
        max_tokens: Optional[int] = None,
    ) -> str:
        """缓存键。

        必须把 model / base_url / max_tokens 一并纳入: 同一组 messages 在不同模型或
        不同 token 预算下会得到不同回复。尤其不应让"被 max_tokens 截断的空回复"去污染
        同一提示词的正常调用。
        """
        raw = json.dumps(
            {
                "m": messages,
                "j": json_mode,
                "t": temperature,
                "model": model,
                "base": base_url.rstrip("/"),
                "mt": max_tokens,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> None:
        if self._cache_loaded or not self.config.cache_enabled:
            self._cache_loaded = True
            return
        path = Path(self.config.cache_path)
        if path.exists():
            try:
                self._cache = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"AI 缓存文件读取失败(将重建): {exc}")
        self._cache_loaded = True

    def _save_cache(self) -> None:
        if not self.config.cache_enabled:
            return
        try:
            path = Path(self.config.cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=0), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"AI 缓存写入失败: {exc}")

    def clear_cache(self) -> None:
        """清空 AI 响应缓存。"""
        self._cache.clear()
        self._save_cache()

    # -- 对话 --
    async def chat(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """调用 chat/completions; 返回助手文本, 失败返回 None(不抛异常)。"""
        cfg = self.config
        temp = cfg.temperature if temperature is None else temperature
        budget = max_tokens or cfg.max_tokens
        # 规范化 base_url(补全 /v1), 避免用户填裸域名导致 404
        base = cfg.effective_base_url()
        cache_k = self._cache_key(messages, json_mode, temp, cfg.model, base, budget)
        self._load_cache()
        if cfg.cache_enabled and cache_k in self._cache:
            logger.debug("AI 响应命中缓存")
            return str(self._cache[cache_k].get("content", "")) or None

        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = cfg.resolve_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": budget,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        logger.debug(f"AI 调用: {cfg.provider} {cfg.model} @ {truncate(base, 60)}")
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            last_error = ""
            for attempt in range(cfg.max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    # 部分服务不支持 response_format, 自动降级重试一次
                    if resp.status_code == 400 and json_mode and "response_format" in resp.text:
                        payload.pop("response_format", None)
                        json_mode = False
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    # 空回复不写缓存: 推理模型在 max_tokens 被 reasoning 耗尽时会返回
                    # content="" + finish_reason=length。缓存它会让同一提示词此后永远
                    # 命中空回复, 即便调大预算也无法恢复。
                    if cfg.cache_enabled and content:
                        self._cache[cache_k] = {"ts": time.time(), "content": content}
                        self._save_cache()
                    finish = data["choices"][0].get("finish_reason")
                    if not content and finish == "length":
                        logger.warning(
                            "AI 回复为空且 finish_reason=length: max_tokens "
                            f"({budget}) 被推理过程耗尽, 请调大预算或改用非推理模型"
                        )
                    logger.debug(f"AI 调用成功, 返回 {len(content)} 字符")
                    return content
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP {exc.response.status_code}: {truncate(exc.response.text, 200)}"
                    if exc.response.status_code in (401, 403):
                        logger.error(f"AI 认证失败, 请检查 API Key: {last_error}")
                        return None
                except (httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                if attempt < cfg.max_retries:
                    wait = 2.0**attempt
                    logger.warning(f"AI 调用失败({last_error}), {wait:.0f}s 后重试")
                    await asyncio.sleep(wait)
            logger.error(f"AI 调用最终失败: {last_error}")
            return None


# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------
_SYSTEM_ANALYST = (
    "你是一名资深网页数据提取专家。用户会给出: 1) 抓取目标(自然语言); 2) 页面结构报告"
    "(候选列表区/简化DOM树/分页信息/元数据)。请输出一个严格的 JSON 提取规则, 不要输出任何其他内容。\n"
    "JSON 结构:\n"
    "{\n"
    '  "mode": "dom 或 json",\n'
    '  "notes": "简短推理说明",\n'
    '  "list_rule": {\n'
    '    "item_selector": "列表项选择器",\n'
    '    "fields": [{"name":"字段名","selector":"选择器","attribute":"属性名或null","transform":["strip"],"required":false}]\n'
    "  },\n"
    '  "pagination": {"next_selector":"下一页选择器或null","max_pages":数字}\n'
    "}\n"
    "选择器约定:\n"
    "- mode=dom: item_selector 用 CSS(或以 // 开头的 XPath); 字段 selector 相对列表项, CSS 优先, "
    "取属性用 attribute(\"href\"/\"src\"), attribute=\"html\" 取 innerHTML; 目标是 XHR JSON 数据时用 mode=json。\n"
    "- mode=json: item_selector 是定位条目数组的 JSONPath(如 $.data.list[*]), 字段 selector 是相对条目的 JSONPath。\n"
    "transform 可选值: strip/int/float/price/date/url/lower/upper/json/regex:模式; 金额价格务必加 \"price\"。\n"
    "字段名用简洁英文小写下划线。若页面存在多个候选列表, 选择最符合用户目标的一个。"
)

_SYSTEM_JSON_EXTRACT = (
    "你是数据提取专家。用户给出一段 API 响应 JSON 样本和抓取目标, 请输出严格 JSON(无其他内容):\n"
    "{\n"
    '  "item_path": "定位条目数组的 JSONPath, 如 $.data.list[*]",\n'
    '  "fields": [{"name":"字段名","jsonpath":"相对单个条目的 JSONPath","transform":["strip"]}],\n'
    '  "notes": "简短说明"\n'
    "}\n"
    "transform 可选: strip/int/float/price/date/url/lower/upper/json。若目标是单对象而非列表, item_path 填 \"$\"。"
)

_SYSTEM_REPAIR = (
    "你是网页结构专家。原 CSS 选择器在新版页面 HTML 中失效了。请根据新 HTML 与原选择器的语义, "
    "推荐一个新的、能在该 HTML 中唯一定位同一元素的 CSS 选择器。只输出 JSON: "
    '{"selector":"新选择器","reason":"简短原因"}'
)

_SYSTEM_CLEAN = (
    "你是数据清洗专家。对输入的原始记录数组做实体识别、分类与归一化, 使其符合给定 schema 的字段。"
    "只输出 JSON: {\"items\": [清洗后的记录数组]}, 记录数与输入保持一致, 无法确定的字段填 null。"
)


# ---------------------------------------------------------------------------
# 高层引擎
# ---------------------------------------------------------------------------
class AIEngine:
    """面向爬虫任务的 AI 能力封装(全部方法失败安全, 失败返回 None)。"""

    def __init__(self, settings: Settings, client: Optional[AIClient] = None) -> None:
        self.settings = settings
        self.config = settings.ai
        self.client = client or AIClient(self.config)

    @property
    def available(self) -> bool:
        """AI 是否可用: 总开关开启、非离线、且已配置 Key(Ollama 本地无需 Key)。"""
        if not self.config.enabled or self.config.offline:
            return False
        if self.config.provider == "ollama":
            return True
        return bool(self.config.resolve_api_key())

    # ------------------------------------------------------------------
    # 1. analyze_page: 结构报告 + 目标 -> 提取规则
    # ------------------------------------------------------------------
    async def analyze_page(
        self,
        report: PageStructureReport,
        goal: str,
        html_sample: str = "",
    ) -> Optional[ExtractionRule]:
        """根据自然语言目标与页面结构报告生成提取规则。"""
        if not self.available:
            return None
        candidates = [c.model_dump() for c in report.candidate_lists[:5]]
        for c in candidates:
            c["sample_html"] = truncate(c.get("sample_html", ""), 1500)
        payload = {
            "goal": goal,
            "page_url": report.url,
            "page_title": report.title,
            "candidate_lists": candidates,
            "pagination": report.pagination.model_dump() if report.pagination else None,
            "metadata_json_ld": truncate(json.dumps(report.metadata.get("json_ld", []), ensure_ascii=False), 2000),
            # 8000 字符(此前 4000): 树里现在带着正文的图片/作品链接, 是生成
            # 列表规则与 image 字段的主要依据, 给太少就只能看到导航。
            # truncate 已改为**保留头尾**, 所以即使超限也不会把正文整段丢掉。
            "simplified_dom_tree": truncate(report.simplified_tree, 8000),
            "html_sample": truncate(html_sample, 2000),
        }
        messages = [
            {"role": "system", "content": _SYSTEM_ANALYST},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        text = await self.client.chat(messages, json_mode=True)
        data = extract_json_block(text)
        if not isinstance(data, dict):
            logger.warning("AI analyze_page: 返回内容无法解析为 JSON 规则")
            return None
        return self._parse_rule(data, fallback_goal=goal)

    # ------------------------------------------------------------------
    # 2. extract_from_json: API 响应 -> 字段映射
    # ------------------------------------------------------------------
    async def extract_from_json(self, json_data: Any, goal: str) -> Optional[ExtractionRule]:
        """从 API 响应 JSON 中识别有用字段, 返回 json 模式提取规则。"""
        if not self.available:
            return None
        sample = json.dumps(json_data, ensure_ascii=False, default=str)
        messages = [
            {"role": "system", "content": _SYSTEM_JSON_EXTRACT},
            {"role": "user", "content": json.dumps(
                {"goal": goal, "json_sample": truncate(sample, 8000)}, ensure_ascii=False
            )},
        ]
        text = await self.client.chat(messages, json_mode=True)
        data = extract_json_block(text)
        if not isinstance(data, dict) or "item_path" not in data:
            logger.warning("AI extract_from_json: 返回内容无法解析")
            return None
        fields = [
            FieldSpec(name=str(f.get("name", "field")), selector=str(f.get("jsonpath", "$")),
                      transform=f.get("transform") or ["strip"])
            for f in data.get("fields", [])
            if isinstance(f, dict) and f.get("name")
        ]
        if not fields:
            return None
        return ExtractionRule(
            mode="json",
            list_rule=ListRule(item_selector=str(data["item_path"]), fields=fields),
            source="ai",
            notes=str(data.get("notes", "")),
        )

    # ------------------------------------------------------------------
    # 3. repair_selector: 选择器自愈
    # ------------------------------------------------------------------
    async def repair_selector(self, old_selector: str, new_html: str, goal: str = "") -> Optional[str]:
        """选择器失效时, 根据新 HTML 推荐新选择器。"""
        if not self.available:
            return None
        messages = [
            {"role": "system", "content": _SYSTEM_REPAIR},
            {"role": "user", "content": json.dumps({
                "old_selector": old_selector,
                "goal": goal,
                "new_html": truncate(new_html, 6000),
            }, ensure_ascii=False)},
        ]
        text = await self.client.chat(messages, json_mode=True)
        data = extract_json_block(text)
        if isinstance(data, dict) and data.get("selector"):
            logger.info(f"选择器修复: {old_selector!r} -> {data['selector']!r}")
            return str(data["selector"])
        logger.warning(f"AI repair_selector 失败, 原选择器: {old_selector!r}")
        return None

    # ------------------------------------------------------------------
    # 4. clean_data: 非结构化文本清洗
    # ------------------------------------------------------------------
    async def clean_data(self, raw_data: list[dict[str, Any]], schema: dict[str, str]) -> Optional[list[dict[str, Any]]]:
        """按 schema 对原始记录做实体识别/归一化。schema 形如 {"title": "商品标题", "price": "价格数字"}。"""
        if not self.available:
            return None
        messages = [
            {"role": "system", "content": _SYSTEM_CLEAN},
            {"role": "user", "content": json.dumps({
                "schema": schema,
                "raw_data": truncate(json.dumps(raw_data, ensure_ascii=False, default=str), 12000),
            }, ensure_ascii=False)},
        ]
        text = await self.client.chat(messages, json_mode=True)
        data = extract_json_block(text)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        logger.warning("AI clean_data: 返回内容无法解析")
        return None

    # ------------------------------------------------------------------
    # 内部: 规则解析
    # ------------------------------------------------------------------
    def _parse_rule(self, data: dict[str, Any], fallback_goal: str) -> Optional[ExtractionRule]:
        """把 AI 返回的 dict 校验为 ExtractionRule, 容错处理常见问题。"""
        list_rule_raw = data.get("list_rule") or {}
        fields_raw = list_rule_raw.get("fields") or []
        fields: list[FieldSpec] = []
        for f in fields_raw:
            if not isinstance(f, dict) or not f.get("name") or not f.get("selector"):
                continue
            transform = f.get("transform") or []
            if isinstance(transform, str):
                transform = [t.strip() for t in transform.split(",") if t.strip()]
            fields.append(
                FieldSpec(
                    name=str(f["name"]).strip(),
                    selector=str(f["selector"]).strip(),
                    attribute=f.get("attribute") if f.get("attribute") not in ("null", "", None) else None,
                    transform=[str(t) for t in transform],
                    required=bool(f.get("required", False)),
                )
            )
        item_selector = str(list_rule_raw.get("item_selector", "")).strip()
        if not item_selector or not fields:
            logger.warning(f"AI 规则缺少 item_selector 或字段(目标: {fallback_goal})")
            return None

        pagination_raw = data.get("pagination") or {}
        pagination = None
        if isinstance(pagination_raw, dict) and pagination_raw.get("next_selector"):
            pagination = PaginationRule(
                next_selector=str(pagination_raw["next_selector"]),
                max_pages=int(pagination_raw.get("max_pages") or 1),
            )

        mode = data.get("mode", "dom")
        if mode not in ("dom", "json"):
            mode = "dom"
        return ExtractionRule(
            mode=mode,
            list_rule=ListRule(item_selector=item_selector, fields=fields),
            pagination=pagination,
            source="ai",
            notes=str(data.get("notes", "")),
        )
