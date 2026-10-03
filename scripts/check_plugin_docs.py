"""
插件文档事实核查 —— 防止 `docs/plugins.md` 与代码脱节。

文档比代码更容易腐烂: 名字改了、参数变了, 文档还停在旧样子, 而**照着文档写的人会直接踩坑**。
所以这里不只检查"文档里提没提", 而是:

1. 文档里点名的**类、函数、参数必须真的存在**(接口存在性);
2. **钩子签名必须与代码一致** —— 特别是 `on_page` 只接收 `ctx` 这一条, 文档里写错过;
3. 把 `plugins/example_enrich.py` 真的**导入并跑一遍**, 确认文档推荐的做法能跑通;
4. 检查文档承诺的辅助函数确实能被 import 到。

用法: python scripts/check_plugin_docs.py
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "plugins.md"

failures: list[str] = []
total = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def main() -> int:
    text = DOC.read_text(encoding="utf-8")

    # ==================================================================
    print("=== 1) 文档点名的接口是否真的存在 ===")
    from smartcrawler.plugins.base import (  # noqa: PLC0415
        HOOK_NAMES,
        ITEM_HOOKS,
        BasePlugin,
        PluginConfigField,
        PluginContext,
        PluginInfo,
    )

    for cls in (BasePlugin, PluginContext, PluginInfo, PluginConfigField):
        check(cls is not None, f"smartcrawler.plugins.base.{cls.__name__} 存在")
    for name in HOOK_NAMES:
        check(name in text, f"文档提及钩子 {name}")

    # PluginContext 的字段文档化情况
    ctx_fields = {f.name for f in __import__("dataclasses").fields(PluginContext)}
    # download_path / notify 是**方法**, 不是 dataclass 字段, 所以要连类属性一起看
    ctx_attrs = ctx_fields | {n for n in dir(PluginContext) if not n.startswith("_")}
    for field in ("settings", "url", "page", "page_no", "crawler", "config", "data",
                  "downloads", "output_dir", "notify", "download_path"):
        check(field in ctx_attrs, f"PluginContext 有字段/方法 {field}")
        check(f"ctx.{field}" in text, f"文档写了 ctx.{field}")

    # ==================================================================
    print("\n=== 2) 辅助函数的签名与文档一致 ===")
    from smartcrawler.plugins.builtin import _media, _media_transform  # noqa: PLC0415

    specs = [
        (_media, "download_many", "ctx"),
        (_media, "urls_from_items", "items"),
        (_media, "urls_from_dom", "page"),
        (_media, "absolute_url", "base"),
        (_media, "guess_extension", "content_type"),
        (_media, "safe_filename_from_url", "url"),
        (_media_transform, "build_rules", "raw"),
        (_media_transform, "to_original", "url"),
        (_media_transform, "derive_rule", "thumb"),
        (_media_transform, "resolve_limit", "ctx"),
        (_media_transform, "parse_count_from_goal", "goal"),
    ]
    for module, name, first_param in specs:
        func = getattr(module, name, None)
        check(callable(func), f"{module.__name__.split('.')[-1]}.{name} 存在")
        if not callable(func):
            continue
        params = list(inspect.signature(func).parameters)
        check(bool(params) and params[0] == first_param,
              f"{name} 首个参数是 {first_param}", str(params[:2]))
        check(name in text, f"文档提及 {name}")

    # download_many 的关键关键字参数
    dm_params = set(inspect.signature(_media.download_many).parameters)
    for kw in ("plugin_id", "subdir", "referer", "allowed_types", "concurrency",
               "max_file_size", "filename_prefix"):
        check(kw in dm_params, f"download_many 支持关键字 {kw}")
        check(kw in text, f"文档写了 {kw}")

    # urls_from_items 的关键字参数
    check("allowed_ext" in inspect.signature(_media.urls_from_items).parameters,
          "urls_from_items 支持 allowed_ext")

    # ==================================================================
    print("\n=== 3) 钩子签名: 文档与代码必须一致 ===")
    # 这是曾经写错的地方: 文档把 on_page 写成接收 page_no, 实际只有 ctx。
    # 直接读管理器/基类里的调用约定来核对。
    manager_src = (ROOT / "smartcrawler" / "plugins" / "manager.py").read_text(encoding="utf-8")
    base_src = (ROOT / "smartcrawler" / "plugins" / "base.py").read_text(encoding="utf-8")

    check("ITEM_HOOKS" in base_src, "base.py 用 ITEM_HOOKS 区分带 items 的钩子")
    item_hooks = set(ITEM_HOOKS)
    check(item_hooks == {"after_extract", "on_finish"},
          "只有 after_extract / on_finish 接收 items", str(sorted(item_hooks)))

    # 文档里的签名表
    for hook in HOOK_NAMES:
        if hook in item_hooks:
            expect = f"{hook}(ctx, items)"
        else:
            expect = f"{hook}(ctx)"
        # 允许出现"`on_page` | `on_page(ctx)`"这种表格写法
        check(expect.replace(" ", "") in text.replace(" ", ""),
              f"文档给出的 {hook} 签名正确", expect)

    # ---- 用 AST 检查文档里**代码块内的钩子定义** ----
    # 比字符串匹配可靠: 只看真正的 def, 不会被正文里的说明文字误伤。
    # 这条来自一个真实事故: 文档曾把 on_page 写成接收 page_no, 照着写的人必然踩坑。
    import re as _re  # noqa: PLC0415

    code_blocks = _re.findall(r"```python\n(.*?)```", text, _re.S)
    check(bool(code_blocks), "文档里有可解析的 python 代码块", f"{len(code_blocks)} 块")

    hook_defs = 0
    for block in code_blocks:
        try:
            tree = ast.parse(block)
        except SyntaxError:
            # 片段式代码(如只有一行 await ...)不参与 AST 检查, 不算错误
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in HOOK_NAMES:
                continue
            hook_defs += 1
            # 参数: self + 钩子自身参数
            params = [a.arg for a in node.args.args]
            body = [p for p in params if p != "self"]
            expected = 2 if node.name in item_hooks else 1
            check(
                len(body) == expected,
                f"**文档代码块里 {node.name} 的参数个数正确**",
                f"实际 {params} -> 期望 self + {expected} 个",
            )
    check(hook_defs > 0, "文档代码块里确实演示了钩子定义", f"{hook_defs} 处")

    # ==================================================================
    print("\n=== 4) 真的导入并运行示例插件 ===")
    example = ROOT / "plugins" / "example_enrich.py"
    check(example.exists(), "示例插件文件存在")
    if example.exists():
        spec = importlib.util.spec_from_file_location("_doc_check_example", example)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            check(True, "示例插件可以正常导入")
        except Exception as exc:  # noqa: BLE001
            check(False, "示例插件可以正常导入", f"{type(exc).__name__}: {exc}")
            module = None

        if module is not None:
            plugin_cls = next(
                (v for k, v in vars(module).items()
                 if isinstance(v, type) and issubclass(v, BasePlugin) and v is not BasePlugin),
                None,
            )
            check(plugin_cls is not None, "示例插件里能找到一个 BasePlugin 子类")
            if plugin_cls is not None:
                info = plugin_cls().info()
                check(bool(info.id), "示例插件声明了 id", info.id)
                check(info.default_enabled is False,
                      "示例插件 default_enabled=False(用户插件不该默认开启)")
                check(bool(info.config_schema), "示例插件声明了配置项",
                      f"{len(info.config_schema)} 项")

                # 用最小上下文真跑一次 after_extract
                from smartcrawler.config import get_settings  # noqa: PLC0415

                ctx = PluginContext(
                    settings=get_settings(),
                    url="https://example.com/list",
                    config={f.key: f.default for f in info.config_schema},
                )
                items = [{"title": "a"}, {"title": "b"}]
                out = plugin_cls().after_extract(ctx, items)
                check(isinstance(out, list) and len(out) == 2,
                      "**示例插件的 after_extract 能跑通并返回记录**", f"{out}")
                if isinstance(out, list) and out:
                    field = next(iter(info.config_schema)).key
                    check(any(k for k in out[0]), "返回值仍是记录字典", str(list(out[0])[:4]))

    # ==================================================================
    print("\n=== 5) 文档结构与关键小节 ===")
    for section in ("## 2. 三条硬性准则", "## 14. 提交前自检清单",
                    "resolve_limit", "setdefault", "plugin_errors"):
        check(section in text, f"文档含 {section!r}")

    check("不提供沙箱" in text or "不提供沙箱" in text.replace("**", ""),
          "文档明确提示了安全风险")

    print("\n" + "=" * 62)
    if failures:
        print(f"插件文档核查: 未通过 ✗ ({len(failures)}/{total})")
        for f in failures:
            print(f"  - {f}")
    else:
        print(f"插件文档核查: 通过 ✓ ({total}/{total})")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
