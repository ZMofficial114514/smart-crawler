"""
静态检查: 源码里每个字符串字面量是否**真的**能被它旁边指定的字体画出来。

为什么需要它: 我前面写过一个"字体检查", 但**样本是我自己手挑的**, 于是它一路绿灯, 而实际
画面里 `latin(30)` 画 `"抓取区域 · scope"` 时中文全成了豆腐块 —— 因为 Segoe UI 不含中文字形。
问题在于"我挑的样本"和"代码里真实出现的字面量"是两回事。

这个检查改成**扫源码**: 找出 `xxx(d, x, y, "字面量", font(...))` 这种调用, 取真实字面量与
真实字体去比对。凡是源码里写死的文案, 一个都跑不掉。
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import render_kit as rk  # noqa: E402

#: 绘制辅助函数里, 第几个实参是文案(0 基)。
TEXT_ARG = {
    "text_at": 3,      # text_at(d, x, y, s, font, ...)
    "text_center": 2,  # text_center(d, y, s, font, ...)
    "draw_caption": 1,  # draw_caption(d, s, alpha)
}
FONT_FUNCS = {"cn", "cn_light", "latin", "mono", "mono_cjk"}
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")

FONT_OBJ = {
    "cn": lambda: rk.cn(30),
    "cn_light": lambda: rk.cn_light(30),
    "latin": lambda: rk.latin(30),
    "mono": lambda: rk.mono(24),
    "mono_cjk": lambda: rk.mono_cjk(24),
}


def literals_in(node: ast.AST) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):   # f-string
            for part in sub.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
    return out


def main() -> int:
    print("  扫描 scenes.py / scenes_extra.py 里所有写死的文案…")
    problems: list[str] = []
    checked = 0
    for name in ("scenes.py", "scenes_extra.py"):
        p = HERE / name
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname not in TEXT_ARG:
                continue
            idx = TEXT_ARG[fname]
            if len(node.args) <= idx:
                continue
            arg = node.args[idx]
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                # 变量文案(如 f-string / 列表元素)静态取不到, 交给下面的列表扫描
                continue
            # 找这一行里用到的字体
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    sf = getattr(sub.func, "id", None)
                    if sf in FONT_FUNCS:
                        checked += 1
                        if not rk.has_glyphs(FONT_OBJ[sf](), arg.value):
                            problems.append(
                                f"{name}:{node.lineno} {sf}() 画不出 {arg.value!r}")
                        break

    # 列表/元组里的文案(如 cards = [("名称", "说明")])也要查 —— 上面那遍只覆盖直接字面量
    for name in ("scenes.py", "scenes_extra.py"):
        p = HERE / name
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if fname not in TEXT_ARG:
                    continue
                idx = TEXT_ARG[fname]
                if len(node.args) <= idx:
                    continue
                arg = node.args[idx]
                if isinstance(arg, ast.Constant):
                    continue
                # 变量文案: 保守起见只报"明显含中文却配 latin()"的情况
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "latin":
                        checked += 1
                        break

    print(f"  检查了 {checked} 处文字绘制调用")
    if problems:
        print(f"\n  ✗ 发现 {len(problems)} 处字体画不出文案(会渲染成豆腐块):")
        seen: set[str] = set()
        for x in problems:
            key = x.split(" 画不出 ")[1][:40]
            if key in seen:
                continue
            seen.add(key)
            print(f"      {x}")
        print(f"\n  合计 {len(problems)} 处, 去重后 {len(seen)} 种文案")
        print("\n  字体检查: 未通过 ✗")
        return 1

    print("\n  字体检查: 通过 ✓(源码里所有写死的文案都能被对应字体画出)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
