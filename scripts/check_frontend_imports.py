"""
前端静态检查: 找出"用到了未导入的符号"这类只在运行时才炸的错误。

**为什么需要它**: ``node --check`` 只验证语法。像
``formatBytes(...)`` 忘了写进 ``import`` 这种问题语法完全合法, 只有真正执行到那一行
才会抛 ``ReferenceError`` —— 而它可能藏在"插件下载产物卡片"这种不常走的渲染分支里,
手工点测很容易漏掉。本项目就真的踩过一次。

做法: 用正则收集每个模块的 import 绑定名与顶层声明, 再扫描疑似标识符调用,
报告既不是导入、也不是声明、也不是 JS 内建/关键字的符号。

用法: python scripts/check_frontend_imports.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK, 输出 ✗ 会抛 UnicodeEncodeError, 先切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

FRONTEND = Path(__file__).resolve().parent.parent / "smartcrawler" / "web" / "frontend"

# JS 内建 / 浏览器全局 / 关键字(含常用的模块级工具函数误报白名单)
GLOBALS = {
    # 语言内建
    "console", "JSON", "Object", "Array", "String", "Number", "Boolean", "Math", "Date", "RegExp",
    "Error", "TypeError", "RangeError", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol",
    "Proxy", "Reflect", "BigInt", "Infinity", "NaN", "undefined", "null", "true", "false",
    "this", "super", "arguments", "globalThis", "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "structuredClone",
    "await", "async", "function", "return", "typeof", "instanceof", "new", "delete", "void", "in", "of",
    "if", "else", "for", "while", "do", "switch", "case", "break", "continue", "try", "catch",
    "finally", "throw", "class", "extends", "import", "export", "from", "default", "const", "let", "var",
    "yield", "static", "get", "set",
    # 浏览器 / DOM
    "window", "document", "navigator", "location", "history", "localStorage", "sessionStorage",
    "fetch", "Headers", "Request", "Response", "Blob", "File", "FileReader", "FormData", "URL",
    "URLSearchParams", "AbortController", "AbortSignal", "WebSocket", "TextEncoder", "TextDecoder",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "queueMicrotask", "alert", "confirm", "prompt", "getComputedStyle",
    "matchMedia", "IntersectionObserver", "ResizeObserver", "MutationObserver", "Event", "CustomEvent",
    "Node", "Element", "HTMLElement", "CSS", "DOMParser", "XMLHttpRequest", "crypto", "performance",
    "MediaQueryList", "Image", "Audio",
    # 本项目模块内的常见形参/局部名(避免正则误判)
    "el", "err", "error", "value", "item", "items", "node", "event", "task", "plugin", "info",
}

_IMPORT_RE = re.compile(
    r"import\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*(?:from\s*)?['\"]([^'\"]+)['\"]",
    re.MULTILINE | re.DOTALL,
)
_DECL_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)"
    r"|(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)"
    r"|(?:export\s*\{([^}]*)\})",
    re.MULTILINE,
)
# 疑似"调用了某个标识符": 行首/非属性访问的 name(
_CALL_RE = re.compile(r"(?<![\w.$])([a-z_$][\w$]*)\s*\(")


def collect_bound_names(source: str) -> set[str]:
    """收集该模块内所有已绑定的名字(import + 顶层声明 + export 列表)。"""
    names: set[str] = set()

    for default_name, named, _path in _IMPORT_RE.findall(source):
        if default_name:
            names.add(default_name)
        for part in (named or "").split(","):
            part = part.strip()
            if not part:
                continue
            # 处理 `a as b` 与 `a`
            alias = part.split(" as ")[-1].strip()
            if alias:
                names.add(alias)

    for func_name, var_name, export_list in _DECL_RE.findall(source):
        if func_name:
            names.add(func_name)
        if var_name:
            names.add(var_name)
        for part in (export_list or "").split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split(" as ")[-1].strip())

    # 解构赋值 / 箭头函数参数里出现的名字也一并放行(正则难以精确建模, 宁可少报)
    for match in re.finditer(r"(?:const|let|var)\s*[\[{]([^\]}]*)[\]}]\s*=", source):
        for part in re.split(r"[,:]", match.group(1)):
            token = part.strip().split("=")[0].strip().strip(".")
            if token and token.isidentifier():
                names.add(token)
    for match in re.finditer(r"(?:function\s*\w*|\(|,)\s*\(?([\w$,\s=]{0,120}?)\)\s*(?:=>|\{)", source):
        for part in match.group(1).split(","):
            token = part.strip().split("=")[0].strip()
            if token.isidentifier():
                names.add(token)
    for match in re.finditer(r"for\s*\(\s*(?:const|let|var)\s+([\w$]+)\s+of", source):
        names.add(match.group(1))
    for match in re.finditer(r"catch\s*\(\s*([\w$]+)", source):
        names.add(match.group(1))

    return names


_TEMPLATE_RE = re.compile(r"`(?:\\[\s\S]|[^`\\])*?`")


def _template_to_expressions(match: "re.Match[str]") -> str:
    """把模板字面量替换为其中 ``${...}`` 表达式的内容。

    **为什么不能直接整段删掉**: 模板里的 `${...}` 是**可执行代码**, 把整段模板当成
    普通字符串丢掉, 会让检查器看不见 ``${formatBytes(x)}`` 这类调用 —— 实测就是这样
    漏掉了 ``formatBytes`` 的漏导入。所以这里保留表达式文本, 只丢弃字面量部分。
    """
    body = match.group(0)[1:-1]
    out: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("${", i) and depth == 0:
            depth = 1
            current = []
            i += 2
            continue
        if depth > 0:
            char = body[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    out.append(" " + "".join(current) + " ")
                    i += 1
                    continue
            current.append(char)
        i += 1
    return "".join(out) if out else "``"


def strip_strings_and_comments(source: str) -> str:
    """去掉字符串、模板字面量与注释, 但**保留模板里的 ${} 表达式**。

    两条关键经验(都是踩过的坑):

    1. 模板字面量必须用 ``re.sub`` 的回调把 ``${...}`` 提取出来, 否则嵌在模板里的
       函数调用会被一并丢弃, 检查器会漏报漏导入。
    2. 模板匹配不能假设单行: 真实代码里有跨行的模板(含转义换行与嵌套 ``${}``),
       因此用非贪婪的 ``.*?`` 配 ``[\\s\\S]``, 并在 ``${}`` 内做花括号配平。
       同时字符类里排除裸反引号, 保证不会跨模板误吞。
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"(?<!:)//[^\n]*", " ", source)
    source = _TEMPLATE_RE.sub(_template_to_expressions, source)
    source = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", source)
    source = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', source)
    # 正则字面量: 常见于 replace/split/test 的参数。放在字符串处理之后,
    # 否则会把 URL 里的 / 误当正则起始。
    source = re.sub(r"(?<![\w)\]])\s*/(?:\\.|\[[^\]]*\]|[^/\\\n])+/\s*[gimsuy]*", " RE ", source)
    return source


# 函数 / 类方法声明:
#   function foo(...)   async function foo(...)
#   foo(...) {          async foo(...) {        get foo() {
# 它们在语法上和调用长得很像, 必须显式排除。
_FUNC_DECL_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*[\w$]*\s*\(")
_METHOD_DECL_RE = re.compile(r"^\s*(?:static\s+|async\s+|get\s+|set\s+)*([\w$]+)\s*\(")


def _is_declaration(line: str) -> bool:
    """判断这一行是不是"声明"而不是"调用"。

    调用形如 ``name(args);`` / ``= name(args)``; 声明形如 ``name(args) {``。

    难点: 声明的形参可能有默认值(``async downloadFile(path, name = 'x') {``),
    所以不能简单地认为"含 = 就是赋值"。这里改用括号位置判断 ——
    赋值语句里的 ``=`` 一定出现在首个 ``(`` 之前(如 ``const x = f(``),
    而默认值里的 ``=`` 一定在括号内。
    """
    stripped = line.strip()
    if _FUNC_DECL_RE.match(line):
        return True
    if not stripped.endswith("{"):
        return False
    if stripped.startswith(("return", "await")):
        return False

    open_paren = stripped.find("(")
    if open_paren < 0:
        return False
    head = stripped[:open_paren]
    if "=" in head:
        return False  # 形如 `const x = foo(` —— 是赋值/调用
    return bool(_METHOD_DECL_RE.match(line))


def check_file(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    bound = collect_bound_names(raw)
    code = strip_strings_and_comments(raw)

    problems: list[str] = []
    for lineno, line in enumerate(code.splitlines(), start=1):
        if _is_declaration(line):
            continue  # 声明不是调用
        for name in _CALL_RE.findall(line):
            if name in bound or name in GLOBALS:
                continue
            # 属性访问 / 对象字面量的 key 已在正则里排除; 这里再放行全大写常量等
            if name.isupper():
                continue
            problems.append(f"第 {lineno} 行: 调用了未导入/未声明的 `{name}()`  ← {line.strip()[:90]}")
    return problems


def main() -> int:
    files = sorted(FRONTEND.rglob("*.js"))
    if not files:
        print(f"未找到前端 JS 文件: {FRONTEND}")
        return 2

    total = 0
    for path in files:
        problems = check_file(path)
        if problems:
            total += len(problems)
            print(f"\n✗ {path.relative_to(FRONTEND)}")
            for problem in problems:
                print(f"    {problem}")

    print(f"\n检查了 {len(files)} 个模块, 发现 {total} 处疑似未定义调用")
    if total:
        print("提示: 若确为动态/全局注入的符号, 请加入脚本顶部的 GLOBALS 白名单。")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
