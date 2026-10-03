"""
示例用户插件 —— 复制本文件即可开始写自己的插件。

放在项目根的 ``plugins/`` 目录下的任意 ``*.py`` 都会被自动发现, 无需注册、无需重启框架
(在 Web 控制台「插件」页点"重新扫描"即可)。

⚠️ 安全提示: 用户插件等同于在本机运行的 Python 代码, 框架不提供沙箱。
   只加载你自己写的或完全信任的插件。

这个示例演示了四件事:
1. 用类属性声明元信息与配置项(界面会自动生成表单);
2. 从 ``ctx.config`` 读用户配置;
3. 在 ``after_extract`` 里改写数据;
4. 把文件写进 ``ctx.output_dir`` 并登记为可下载产物。

写完后在「插件」页把它启用即可。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from smartcrawler.models import DownloadedFile
from smartcrawler.plugins.base import BasePlugin, PluginContext


class ExampleEnrichPlugin(BasePlugin):
    """示例插件: 给每条记录补充来源域名与抓取时间戳, 并可选导出一份 JSON。"""

    # ---- 元信息(会显示在插件卡片上) ----
    id = "example-enrich"  # 唯一标识, 建议 kebab-case
    name = "示例: 补充字段"
    description = "给每条记录补充来源域名与时间戳, 可选导出一份 JSON(插件编写模板)"
    version = "1.0.0"
    author = "你的名字"
    category = "cleanup"  # download / anti-bot / cleanup / storage / other
    tags = ["示例", "模板", "清洗"]
    default_enabled = False

    # ---- 配置项(界面上自动生成对应控件) ----
    config_schema: list[dict[str, Any]] = [
        {
            "key": "field_name",
            "label": "域名写入的字段名",
            "type": "str",
            "default": "source_domain",
        },
        {
            "key": "add_timestamp",
            "label": "补充抓取时间",
            "type": "bool",
            "default": True,
        },
        {
            "key": "drop_empty",
            "label": "丢弃全空记录",
            "type": "bool",
            "default": False,
            "description": "所有字段都为空的记录直接剔除",
        },
        {
            "key": "export_json",
            "label": "额外导出一份 JSON",
            "type": "bool",
            "default": False,
        },
    ]

    # ------------------------------------------------------------------
    # 钩子: 提取到数据之后
    # 可用钩子(按需实现其一或全部):
    #   on_start(ctx)                    任务开始
    #   before_navigate(ctx)             导航前(可 await page.context.set_extra_http_headers(...))
    #   after_navigate(ctx)              导航后(可滚动/注入脚本/判断拦截页)
    #   before_extract(ctx)              提取前
    #   after_extract(ctx, items)        提取后(最常用, 返回新的 items)
    #   on_page(ctx, ...)                每翻完一页
    #   on_finish(ctx, items)            任务结束(汇总/额外导出)
    # 同步与异步写法都支持: 不涉及 await 就写成普通 def, 管理器会自动适配。
    # ------------------------------------------------------------------
    def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        field = str(ctx.config.get("field_name") or "source_domain").strip() or "source_domain"
        domain = urlparse(ctx.url).hostname or ""
        keep_timestamp = bool(ctx.config.get("add_timestamp", True))
        drop_empty = bool(ctx.config.get("drop_empty", False))

        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        out: list[dict[str, Any]] = []
        dropped = 0

        for item in items or []:
            if not isinstance(item, dict):
                continue
            if drop_empty and not any(v not in (None, "", [], {}) for v in item.values()):
                dropped += 1
                continue
            # setdefault: 不覆盖用户规则里已有的同名字段
            item.setdefault(field, domain)
            if keep_timestamp:
                item.setdefault("crawled_at", now)
            out.append(item)

        ctx.notify("INFO", f"示例插件: 已处理 {len(out)} 条记录(补充 {field}={domain})")
        if dropped:
            ctx.notify("INFO", f"示例插件: 丢弃了 {dropped} 条全空记录")
        return out

    def on_finish(self, ctx: PluginContext, items: list[dict[str, Any]]):
        """可选: 额外导出一份 JSON, 并登记为可下载产物。"""
        if not bool(ctx.config.get("export_json", False)) or not items:
            return items

        target = ctx.download_path("example-export", f"enriched_{_stamp()}.json")
        try:
            target.write_text(
                json.dumps(items, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            ctx.notify("WARNING", f"示例插件: 导出失败 {exc}")
            return items

        ctx.downloads.append(
            DownloadedFile(
                url=ctx.url,
                path=str(target),
                filename=target.name,
                size=target.stat().st_size,
                mime_type="application/json",
                plugin_id=self.id,
            )
        )
        ctx.notify("SUCCESS", f"示例插件: 已导出 {len(items)} 条 -> {target.name}")
        return items


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


__all__ = ["ExampleEnrichPlugin"]
