"""
SmartCrawler 数据提取与结构化模块。

Extractor 职责:
1. DOM 提取: 按 ExtractionRule 在页面上执行 JS 批量提取(单次 evaluate, 高效);
   支持 CSS / XPath(以 // 或 xpath= 开头)/ 相对选择器;
2. JSON 提取: 在网络监听捕获的 XHR 响应体上按 JSONPath 提取(迷你 JSONPath 引擎);
3. 清洗管线: strip / int / float / price / date / url / regex:xxx 等内置变换;
4. 结构化: 映射到 Pydantic 模型校验、必填检查、内容哈希去重、增量过滤。

设计要点: 字段的 required=True 时该行缺失即丢弃; "url" 变换需要 base_url 做相对地址补全。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Type, TypeVar

from loguru import logger
from playwright.async_api import Page
from pydantic import BaseModel, ValidationError

from .config import Settings
from .models import ExtractionRule, NetworkRecord
from .utils import jsonpath_get, parse_regex_selector, stable_hash, urljoin_safe

TModel = TypeVar("TModel", bound=BaseModel)

# ---------------------------------------------------------------------------
# 页面内批量提取脚本(单次 evaluate)
# ---------------------------------------------------------------------------
_EXTRACT_DOM_JS = r"""
(rule) => {
    const isXPath = (s) => /^(xpath=|\/\/|\(\/\/|\.\/|\.\.\/)/.test(s.trim());

    const evalXPathAll = (expr, ctx) => {
        expr = expr.trim().replace(/^xpath=/, '');
        const snap = document.evaluate(expr, ctx, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const out = [];
        for (let i = 0; i < snap.snapshotLength; i++) out.push(snap.snapshotItem(i));
        return out;
    };

    const getVal = (item, f) => {
        let el = null;
        try {
            if (isXPath(f.selector)) {
                const nodes = evalXPathAll(f.selector, item);
                el = nodes[0] || null;
            } else {
                el = item.querySelector(f.selector);
            }
        } catch (e) { return null; }
        if (!el) return null;
        if (f.attribute === 'html') return el.innerHTML;
        if (f.attribute && f.attribute !== 'text') {
            const v = el.getAttribute(f.attribute);
            return v === null ? null : v.trim();
        }
        return (el.textContent || '').trim();
    };

    let itemEls;
    const itemSel = rule.list_rule.item_selector.trim();
    if (isXPath(itemSel)) itemEls = evalXPathAll(itemSel, document);
    else itemEls = Array.from(document.querySelectorAll(itemSel));

    const items = [];
    for (const item of itemEls) {
        const row = {};
        for (const f of rule.list_rule.fields) row[f.name] = getVal(item, f);
        items.push(row);
    }
    return items;
};
"""


class Extractor:
    """数据提取与结构化引擎。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # DOM 提取
    # ------------------------------------------------------------------
    @staticmethod
    def resolve_frame(page: Page, frame_name: str = "") -> Any:
        """按名称取 frame; 取不到时回退到主文档。

        存在的意义: 分析阶段可能是在某个同源 iframe 里完成的(外壳 + 内嵌 iframe 的站点),
        提取必须落在**同一个** frame 上。否则分析与提取看的是两份不同的文档,
        表现为"分析时识别出 150 条, 抓取却 0 条"。
        """
        if not frame_name:
            return page
        try:
            for f in page.frames:
                if (getattr(f, "name", "") or "") == frame_name:
                    return f
        except Exception:  # noqa: BLE001
            pass
        logger.warning(f"未找到 frame {frame_name!r}, 回退到主文档提取")
        return page

    async def extract_with_rule(
        self,
        page: Page,
        rule: ExtractionRule,
        *,
        frame_name: str = "",
    ) -> list[dict[str, Any]]:
        """在当前页面上按 dom 模式规则提取条目。

        ``frame_name``: 内容所在 iframe 的名称(来自结构报告的 ``content_frame``)。
        """
        if rule.mode != "json" and rule.list_rule:
            rule_data = rule.model_dump(mode="json")
            target = self.resolve_frame(page, frame_name)
            try:
                items = await target.evaluate(_EXTRACT_DOM_JS, rule_data)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"DOM 提取失败: {type(exc).__name__}: {exc}")
                return []
            base_url = page.url
            return [self.post_process_row(row, rule, base_url) for row in items or []]
        return []

    # ------------------------------------------------------------------
    # JSON(网络捕获)提取
    # ------------------------------------------------------------------
    def extract_json_with_rule(
        self, records: list[NetworkRecord], rule: ExtractionRule
    ) -> list[dict[str, Any]]:
        """在捕获的网络响应(JSON)上按 json 模式规则提取条目。"""
        if rule.mode != "json" or not rule.list_rule:
            return []
        assert rule.list_rule is not None
        items: list[dict[str, Any]] = []
        for rec in records:
            if rec.body_json is None:
                continue
            matches = jsonpath_get(rec.body_json, rule.list_rule.item_selector)
            for m in matches:
                # 目标可能直接是对象, 也可能是对象数组
                candidates = m if isinstance(m, list) else [m]
                for entry in candidates:
                    if not isinstance(entry, (dict, list)):
                        continue
                    row: dict[str, Any] = {}
                    for f in rule.list_rule.fields:
                        vals = jsonpath_get(entry, f.selector)
                        row[f.name] = vals[0] if vals else None
                    items.append(self.post_process_row(row, rule, base_url=rec.page_url))
        return items

    # ------------------------------------------------------------------
    # 清洗与校验
    # ------------------------------------------------------------------
    def post_process_row(
        self, row: dict[str, Any], rule: ExtractionRule, base_url: str
    ) -> dict[str, Any]:
        """对单行应用 transform 清洗管线 + 必填检查(必填缺失返回空 dict, 由调用方过滤)。"""
        assert rule.list_rule is not None
        out: dict[str, Any] = {}
        for f in rule.list_rule.fields:
            value = row.get(f.name)
            value = self.apply_transforms(value, f.transform, base_url=base_url)
            if f.required and (value is None or value == ""):
                return {}
            out[f.name] = value
        return out

    @staticmethod
    def apply_transforms(
        value: Any, transforms: Optional[list[str]], base_url: str = ""
    ) -> Any:
        """按顺序应用清洗变换; 未识别的变换忽略并告警一次。"""
        if value is None or not transforms:
            return value
        text = value if isinstance(value, str) else str(value)
        for t in transforms:
            t = t.strip()
            if not t:
                continue
            try:
                if t == "strip":
                    text = text.strip()
                elif t == "int":
                    m = re.search(r"-?\d+", text.replace(",", ""))
                    value = int(m.group()) if m else None
                elif t in ("float", "number"):
                    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
                    value = float(m.group()) if m else None
                elif t == "price":
                    # 提取金额: 支持 1,234.56 / ¥89.00 / 45元
                    cleaned = text.replace(",", "").replace("，", "")
                    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
                    value = float(m.group(1)) if m else None
                elif t == "date":
                    value = _normalize_date(text)
                elif t == "url":
                    value = _to_url(text, base_url) if text else text
                elif t == "lower":
                    text = text.lower()
                elif t == "upper":
                    text = text.upper()
                elif t == "json":
                    import json as _json

                    try:
                        value = _json.loads(text)
                    except _json.JSONDecodeError:
                        pass
                elif t.startswith("regex:"):
                    pattern = parse_regex_selector(t)
                    if pattern:
                        m = pattern.search(text)
                        if m:
                            value = m.group(1) if m.groups() else m.group(0)
                else:
                    logger.debug(f"未识别的 transform: {t!r}(已忽略)")
                    continue
            except Exception as exc:  # noqa: BLE001 - 单个变换失败不拖垮整行
                logger.debug(f"transform {t!r} 执行失败: {exc}")
                continue
        return value

    # ------------------------------------------------------------------
    # 去重 / 增量 / 模型映射
    # ------------------------------------------------------------------
    @staticmethod
    def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """基于内容哈希去重(保持首次出现顺序)。"""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            if not item:
                continue
            h = stable_hash(item)
            if h in seen:
                continue
            seen.add(h)
            out.append(item)
        return out

    @staticmethod
    def map_to_model(
        items: list[dict[str, Any]], model_cls: Type[TModel]
    ) -> tuple[list[TModel], list[str]]:
        """把 dict 列表校验映射为 Pydantic 模型列表, 返回 (有效记录, 错误列表)。"""
        valid: list[TModel] = []
        errors: list[str] = []
        for i, item in enumerate(items):
            try:
                valid.append(model_cls.model_validate(item))
            except ValidationError as exc:
                errors.append(f"第 {i} 条记录校验失败: {exc.errors()[:2]}")
        return valid, errors


def _to_url(text: str, base_url: str) -> str:
    """把字段值变成可用的绝对 URL。

    处理三种"不是裸 URL"的常见形态, 否则下游的图片/音频下载插件会拿到一段没法用的字符串:

    - ``style`` 属性: ``background-image: url('a.jpg')`` -> 取出括号里的地址;
    - ``srcset``: ``a.jpg 1x, b.jpg 2x`` -> 取第一个候选;
    - 懒加载占位: 有些站点留 ``data:image/gif;base64,...``, 这种没有下载价值, 原样返回让
      调用方自己判断(不在这里悄悄改成空, 否则字段会整列消失)。
    """
    value = (text or "").strip()
    if not value:
        return value

    # background-image: url(...) / url("...") / url('...')
    if "url(" in value.lower():
        match = re.search(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", value, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
        else:
            return value

    # srcset: 取第一个候选地址
    if "," in value and re.search(r"\s+\d+(\.\d+)?[wx]\s*$", value):
        first = value.split(",")[0].strip().split(" ")[0]
        if first:
            value = first

    return urljoin_safe(base_url, value)


def _normalize_date(text: str) -> str:
    """尝试把常见日期格式归一化为 ISO(失败原样返回)。"""
    text = text.strip()
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).isoformat(timespec="seconds")
    except ValueError:
        return text
