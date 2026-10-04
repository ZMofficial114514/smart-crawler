"""
SmartCrawler 工具函数模块。

包含:
- loguru 日志初始化
- 稳定哈希(去重/增量)
- 迷你 JSONPath 解析器(零依赖, 覆盖常见语法)
- AI 返回文本中的 JSON 块鲁棒解析
- 文本/类型小工具
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from typing import Any, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
# 两个容易踩的坑, 都在这里一次性解决:
#
# 1) **不要用无参 logger.remove()**。它会清掉所有 handler, 包括别的模块自己挂的
#    sink —— Web 控制台的实时日志总线就是这样被干掉的: lifespan 先挂上总线 sink,
#    紧接着 CrawlService.startup() 调用本函数把它一并移除, 界面上的"实时日志"
#    从此永远是空的。所以只清理本函数自己添加的 handler。
#
# 2) **用 record 标记做幂等键, 而不是模块级变量**。同一份代码可能以两个模块名被
#    加载(例如 `smartcrawler.utils` 与 `utils`), 各自持有一份模块全局变量, 于是
#    每个副本都以为自己"还没配置过", 日志就被打印两遍。这里给自家 handler 打上
#    `_smartcrawler_owned` 标记, 以 loguru 进程级注册表为准做幂等判断。
_OWNED_MARK = "_smartcrawler_owned"


def _find_owned_handler(kind: str) -> int | None:
    """按标记查找本模块之前添加的 handler id。kind: 'console' / 'file'。"""
    for handler_id, handler in logger._core.handlers.items():  # noqa: SLF001 - loguru 无公开查询 API
        if getattr(handler, _OWNED_MARK, None) == kind:
            return handler_id
    return None


def _set_handler_level(handler_id: int, level: str) -> None:
    """就地调整已有 handler 的级别(避免重复 add 造成日志重复)。"""
    logger._core.handlers[handler_id].levelno = logger.level(level.upper()).no  # noqa: SLF001


def force_utf8_output() -> None:
    """把控制台输出切成 UTF-8。幂等, 可在任何入口**尽早**调用。

    存在的意义: Windows 控制台默认是 OEM 代码页(简体中文为 GBK), 直接 ``print()``
    中文在支持 UTF-8 的终端里会显示成乱码。原先只有 ``setup_logging()`` 做这件事, 而
    CLI 的启动横幅是在它**之前**打印的 —— 于是横幅永远乱码, 只有日志正常。把这段逻辑
    单独暴露出来, 让入口能在打印任何东西之前先调用它。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # 被重定向到不支持 reconfigure 的对象, 忽略


def setup_logging(level: str = "INFO", log_file: str = "logs/smartcrawler.log") -> None:
    """初始化 loguru: 控制台 + 滚动文件双输出。**幂等**。

    重复调用不会产生重复日志, 也不会影响其他模块挂载的 sink(如 Web 实时日志总线)。
    """
    force_utf8_output()

    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <7}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    existing = _find_owned_handler("console")
    if existing is None:
        handler_id = logger.add(
            sys.stderr,
            level=level.upper(),
            format=console_format,
            backtrace=False,
            diagnose=False,
        )
        # loguru 的 handler 对象允许附加自定义属性, 用作"这是我们的 handler"标记
        setattr(logger._core.handlers[handler_id], _OWNED_MARK, "console")  # noqa: SLF001
    else:
        _set_handler_level(existing, level)

    if not log_file:
        return

    existing_file = _find_owned_handler("file")
    if existing_file is not None:
        _set_handler_level(existing_file, level)
        return

    try:
        from pathlib import Path

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_id = logger.add(
            log_file,
            level=level.upper(),
            rotation="20 MB",
            retention=10,
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} - {message}",
            enqueue=True,
        )
        setattr(logger._core.handlers[file_id], _OWNED_MARK, "file")  # noqa: SLF001
    except OSError as e:  # 目录不可写时降级为仅控制台
        logger.warning(f"日志文件不可用({e}), 仅输出到控制台")


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now_ts() -> float:
    """当前 Unix 时间戳(秒)。"""
    import time

    return time.time()


def iso_now() -> str:
    """当前时间的 ISO 字符串(本地时区)。"""
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSON 相关
# ---------------------------------------------------------------------------
def safe_json_loads(text: Optional[str]) -> Any:
    """尽力把文本解析为 JSON, 失败返回 None(不抛异常)。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def extract_json_block(text: Optional[str]) -> Any:
    """从 LLM 返回的文本中提取第一个平衡的 {...} 或 [...] JSON 块。

    LLM 输出常带 markdown 代码围栏或解释性文字, 这里做鲁棒提取:
    1. 优先剥离 ```json ... ``` 围栏;
    2. 否则扫描平衡的大括号/中括号(忽略字符串字面量内部)。
    解析失败返回 None。
    """
    if not text:
        return None

    # 1) markdown 围栏
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if m:
        parsed = safe_json_loads(m.group(1))
        if parsed is not None:
            return parsed

    # 2) 直接整体解析
    parsed = safe_json_loads(text.strip())
    if parsed is not None:
        return parsed

    # 3) 平衡扫描
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    parsed = safe_json_loads(candidate)
                    if parsed is not None:
                        return parsed
                    break
    return None


# ---------------------------------------------------------------------------
# 迷你 JSONPath
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(
    r"\.\.([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff\-]*)"  # ..key (递归下降)
    r"|\.([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff\-]*)"  # .key
    r"|\[\s*(-?\d+)\s*\]"  # [0] / [-1]
    r"|\[\s*\*\s*\]"  # [*]
    r"|\[\s*'([^']*)'\s*\]"  # ['key']
    r'|\[\s*"([^"]*)"\s*\]',  # ["key"]
    re.DOTALL,
)


def jsonpath_get(obj: Any, expr: str) -> list[Any]:
    """迷你 JSONPath 查询, 返回所有匹配值组成的列表(不匹配返回 [])。

    支持语法(覆盖 API 响应提取的绝大多数场景):
        $              根节点(可省略)
        .key  ['key']  成员访问
        [0]   [-1]     数组索引
        [*]            数组全部元素
        ..key          递归查找任意深度的 key
    示例:
        jsonpath_get(data, "$.data.list[*].title")
        jsonpath_get(data, "$..price")
    """
    if obj is None:
        return []
    tokens = _tokenize_jsonpath(expr)
    return _resolve_jsonpath(obj, tokens)


def _tokenize_jsonpath(expr: str) -> list[tuple[str, Any]]:
    expr = expr.strip()
    if expr.startswith("$"):
        expr = expr[1:]
    tokens: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            # 跳过无法识别的字符(容错)
            pos += 1
            continue
        pos = m.end()
        if m.group(1) is not None:
            tokens.append(("recursive", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("key", m.group(2)))
        elif m.group(3) is not None:
            tokens.append(("index", int(m.group(3))))
        elif m.group(0).startswith("[*"):
            tokens.append(("star", None))
        elif m.group(4) is not None:
            tokens.append(("key", m.group(4)))
        elif m.group(5) is not None:
            tokens.append(("key", m.group(5)))
    if not tokens and expr.strip():
        # 纯键名(如 "name", 不带 $. 前缀)按成员访问处理 —— 字段相对路径的友好写法
        return [("key", expr.strip())]
    return tokens


def _resolve_jsonpath(current: Any, tokens: list[tuple[str, Any]]) -> list[Any]:
    if not tokens:
        return [current]
    kind, arg = tokens[0]
    rest = tokens[1:]
    results: list[Any] = []

    if kind == "key":
        if isinstance(current, dict) and arg in current:
            results.extend(_resolve_jsonpath(current[arg], rest))
    elif kind == "index":
        if isinstance(current, list):
            try:
                idx = arg if arg >= 0 else len(current) + arg
                results.extend(_resolve_jsonpath(current[idx], rest))
            except IndexError:
                pass
    elif kind == "star":
        if isinstance(current, list):
            for item in current:
                results.extend(_resolve_jsonpath(item, rest))
        elif isinstance(current, dict):
            for item in current.values():
                results.extend(_resolve_jsonpath(item, rest))
    elif kind == "recursive":
        # ..key: 深度优先搜索子树中所有名为 key 的节点(外层匹配先于内层);
        # key 已并入本 token, rest 是 key 之后的后缀路径

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if arg in node:
                    results.extend(_resolve_jsonpath(node[arg], rest))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(current)
    return results


# ---------------------------------------------------------------------------
# 哈希与去重
# ---------------------------------------------------------------------------
def stable_hash(data: Any) -> str:
    """对任意可 JSON 化数据计算稳定 MD5(键排序, 与字典插入顺序无关)。"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 选择器判断
# ---------------------------------------------------------------------------
def is_xpath(selector: str) -> bool:
    """判断选择器是否为 XPath。"""
    s = selector.strip()
    return s.startswith(("xpath=", "//", "(//", "./", ".."))


def parse_regex_selector(selector: str) -> Optional[re.Pattern[str]]:
    """解析 're:<pattern>' 形式的正则选择器, 非正则返回 None。"""
    if selector.startswith("re:"):
        try:
            return re.compile(selector[3:])
        except re.error as e:
            logger.warning(f"非法正则选择器 {selector!r}: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------
def truncate(text: Optional[str], limit: int) -> str:
    """安全截断字符串用于日志/Prompt。

    **保留头尾, 不只保留开头。** 这一点很重要, 而且踩过:

    简化 DOM 树的结构是"页面框架在前、正文在后"(侧边栏/页头写在前面, 作品卡片在末尾)。
    早先这里只取 ``text[:limit]``, 于是喂给 AI 的 4000 字符**全是导航** —— 正文一张图
    一个作品链接都进不了 prompt, AI 自然生成不出截图里那种列表规则。
    改成头尾各留一半后, 正文样本得以保留。

    对日志类调用(URL、报错文本)同样安全: 它们通常远短于 limit, 走的是原样返回分支。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    marker = f"\n...[中间省略 {len(text) - limit} 字符, 已保留头尾]...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def ensure_dir(path: str | "Path") -> "Path":  # type: ignore[name-defined]
    """确保目录存在并返回 Path。"""
    from pathlib import Path

    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def urljoin_safe(base: str, url: str) -> str:
    """urljoin 的容错包装: base 非法时原样返回。"""
    from urllib.parse import urljoin

    try:
        return urljoin(base, url)
    except (ValueError, TypeError):
        return url
