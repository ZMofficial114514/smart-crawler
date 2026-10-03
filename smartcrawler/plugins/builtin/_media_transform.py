"""
媒体地址处理: 缩略图 -> 原图, 以及"本次任务最多下载几张"的统一约束。

## 为什么需要"缩略图 -> 原图"

图站列表页给的都是**缩略图**地址, 直接下载只能拿到小图。原图地址有规律可循, 但每个站
不一样。实测两组(用户提供):

```
堆糖  缩略图: https://c-ssl.dtstatic.com/uploads/blog/202502/04/XxS9PMLVcQX8QlO.thumb.400_0.jpeg
      原  图: https://c-ssl.dtstatic.com/uploads/blog/202502/04/XxS9PMLVcQX8QlO.thumb.1000_0.jpeg
                            -> 只是把尺寸段 400_0 换成 1000_0

pixiv 缩略图: https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/10/02/12/40/45/150355142_p0_square1200.jpg
      原  图: https://i.pximg.net/img-master/img/2026/10/02/12/40/45/150355142_p0_master1200.jpg
                            -> 去掉 /c/<尺寸>/ 前缀, 且文件名尾巴 square1200 -> master1200
```

规律不好穷举, 所以这里**不写死任何站点规则**, 而是提供两种让用户/程序给出规律的方式:

1. ``derive_rule``: 给一对"缩略图 + 原图", 自动推出一个替换规则(找差异跨度);
2. ``build_rules``: 把用户填的若干对 URL 编译成可反复套用的规则。

规则是**正则替换**, 形如 ``{"name": ..., "pattern": ..., "replacement": ...}``,
用 ``re.sub`` 套用。这样既能表达"换尺寸段"(堆糖), 也能表达"删前缀"(pixiv)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from loguru import logger


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix_len(a: str, b: str, prefix_len: int) -> int:
    """公共后缀长度(不与前缀重叠)。"""
    n = min(len(a), len(b)) - prefix_len
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def _token_char(ch: str) -> bool:
    """URL 里"词"的组成字符。差异跨度要扩展到词的边界, 否则会匹配到别处。"""
    return ch.isalnum() or ch in "_-"


def _expand_to_token(text: str, start: int, end: int) -> tuple[int, int]:
    """把 [start, end) 向外扩到词边界。

    **为什么必须扩**: 堆糖的缩略图是 `.thumb.400_0.jpeg`、原图是 `.thumb.1000_0.jpeg`,
    最小差异跨度只是那个 `4`。用 `'4' -> '10'` 做替换, 会**误伤 URL 里别的 4** ——
    实测把日期 `202502/04` 变成了 `202502/010`, 生成出一条根本不存在的地址。
    扩到词边界得到 `400_0 -> 1000_0`, 就唯一且安全了。
    """
    while start > 0 and _token_char(text[start - 1]):
        start -= 1
    while end < len(text) and _token_char(text[end]):
        end += 1
    return start, end


@dataclass
class TransformRule:
    """一条"缩略图 -> 原图"的替换规则。"""

    name: str
    pattern: str
    replacement: str
    #: 是否启用
    enabled: bool = True

    def apply(self, url: str) -> str:
        if not self.enabled or not url:
            return url
        try:
            return re.sub(self.pattern, self.replacement, url)
        except re.error:
            return url

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "replacement": self.replacement,
            "enabled": self.enabled,
        }


def derive_rule(thumb: str, full: str, name: str = "自定义") -> Optional[TransformRule]:
    """从一对 URL 推出替换规则。推不出来返回 None。

    思路: 找到两者**最短的差异跨度**, 把它换成另一边的对应内容。这样:
    - 堆糖那种"只差一小段尺寸"-> 规则短且精确;
    - pixiv 那种"前缀不同 + 尾巴不同"-> 退化为整段的差异跨度(仍然能用)。
    """
    thumb, full = thumb.strip(), full.strip()
    if not thumb or not full or thumb == full:
        return None

    prefix_len = _common_prefix_len(thumb, full)
    suffix_len = _common_suffix_len(thumb, full, prefix_len)

    # 差异跨度先扩到词边界, 避免"只差一个数字"时误伤 URL 里其它位置的同一个数字
    mid_start, mid_end = _expand_to_token(thumb, prefix_len, len(thumb) - suffix_len or len(thumb))
    full_start, full_end = _expand_to_token(full, prefix_len, len(full) - suffix_len or len(full))

    thumb_mid = thumb[mid_start:mid_end]
    full_mid = full[full_start:full_end]
    if not thumb_mid and not full_mid:
        return None

    # **关键**: 模式只匹配"差异的那一段", 不要把公共前后缀吸进来。
    # 一旦把前缀也写进模式, re.sub 就会把整条 URL 换掉 —— 替换串里只有差异段,
    # 结果就是输出一个残缺的字符串。
    # 让公共前后缀留在原地不动, 只替换中间, 才是最稳的写法。
    try:
        pattern = re.escape(thumb_mid) if thumb_mid else re.escape(thumb)
    except re.error:  # pragma: no cover - escape 不会失败
        return None
    # 替换串里的反斜杠会被 re.sub 当成转义, 需要转义一次
    replacement = full_mid.replace("\\", r"\\") if full_mid else ""
    return TransformRule(name=name, pattern=pattern, replacement=replacement)


def build_rules(raw: Any, *, auto_pairs: Optional[list[tuple[str, str]]] = None) -> list[TransformRule]:
    """把配置编译成规则列表。

    支持两种输入(可混用):

    - ``raw``: 规则数组, 每项 ``{"name","pattern","replacement"}`` 或 ``"缩略图|原图"``;
    - ``auto_pairs``: (缩略图, 原图) 对, 由 ``derive_rule`` 自动推规则。
    """
    rules: list[TransformRule] = []

    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = [line for line in text.splitlines() if line.strip()]

    if isinstance(raw, dict):
        raw = [raw]

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                pattern = str(entry.get("pattern") or "").strip()
                if not pattern:
                    continue
                rules.append(
                    TransformRule(
                        name=str(entry.get("name") or "自定义"),
                        pattern=pattern,
                        replacement=str(entry.get("replacement") or ""),
                        enabled=bool(entry.get("enabled", True)),
                    )
                )
            elif isinstance(entry, str) and "|" in entry:
                thumb, _, full = entry.partition("|")
                derived = derive_rule(thumb, full, name=f"自动{len(rules) + 1}")
                if derived:
                    rules.append(derived)

    for index, (thumb, full) in enumerate(auto_pairs or [], start=1):
        derived = derive_rule(thumb, full, name=f"自动{len(rules) + index}")
        if derived:
            rules.append(derived)

    return rules


def to_original(url: str, rules: Iterable[TransformRule]) -> tuple[str, Optional[str]]:
    """把缩略图地址换成原图地址。

    返回 ``(最终地址, 命中的规则名)``。**只有在真的改动了地址时**才算命中 ——
    规则可能对某些 URL 不适用, 那种情况必须原样返回, 不能让用户拿到一个坏链接。
    """
    if not url:
        return url, None
    for rule in rules:
        try:
            candidate = rule.apply(url)
        except Exception:  # noqa: BLE001
            continue
        if candidate and candidate != url:
            return candidate, rule.name
    return url, None


# ---------------------------------------------------------------------------
# "本次最多下载几个"的统一约束
# ---------------------------------------------------------------------------
#: 插件上下文 data 里的键。由爬虫按任务参数写入, 各下载插件读取。
MEDIA_LIMIT_KEY = "media_limit"
#: 各插件自己配置的键(兼容老配置)
PLUGIN_LIMIT_KEY = "max_items"


def resolve_limit(ctx: Any, plugin_default: int) -> int:
    """决定本次任务该类媒体的下载上限。

    优先级: **任务参数 > 插件配置 > 内置默认**。

    任务的优先级最高是有原因的: 用户在抓取页填的"下载数量"或直接写在抓取目标里的
    "爬取前三张", 都是**本次任务**的意图, 不该被插件的长期配置覆盖 —— 实测就是这么
    出现"说好前三张, 结果下了 24 张"的。
    """
    data = getattr(ctx, "data", None) or {}
    task_limit = data.get(MEDIA_LIMIT_KEY)
    if task_limit is not None:
        try:
            value = int(task_limit)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    config = getattr(ctx, "config", None) or {}
    for key in (PLUGIN_LIMIT_KEY, "max_images", "max_files", "max_songs"):
        if key in config:
            try:
                value = int(config[key])
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
    return plugin_default


#: 从自然语言目标里认出"数量"的表达(中文/英文)。
#: 数字位允许中文数字, 因为"爬取前三张"和"爬取前3张"一样常见 —— 实测只写 \d 时
#: "爬取前三张"(用户的原始说法)会解析不出来。
_NUM = r"([0-9]{1,4}|[一二两三四五六七八九十百]{1,3})"
_UNIT = r"(?:张|个|条|首|幅|张图|图片|照片|作品|歌曲|音频|音乐|pics?|images?|items?|photos?)"
_COUNT_PATTERNS = (
    rf"(?:前|头|first\s*)\s*{_NUM}\s*{_UNIT}?",
    rf"(?:最多|至多|不超过|只要|仅要|只需要|取)\s*{_NUM}\s*{_UNIT}?",
    rf"{_NUM}\s*(?:张|幅|首|个)\s*(?:图|图片|照片|作品|歌曲|音频|音乐)?",
    rf"(?:top|limit|max|count)\s*[:=]?\s*{_NUM}",
)

#: 中文数字 -> 阿拉伯数字("前三张"这种说法必须支持)
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _to_int(token: str) -> Optional[int]:
    """把匹配到的数字(阿拉伯或中文)转成 int。"""
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    # 中文数字: 支持 一~十 与 十一~九十九 这类常见写法
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for ch in token:
        value = _CN_DIGITS.get(ch)
        if value is None:
            return None
        total = total * 10 + value if total else value
    return total or None


def parse_count_from_goal(goal: str) -> Optional[int]:
    """从自然语言抓取目标里解析"要几个"。

    用户的原始报障就是这条路径: 输入"爬取前三张", 结果下载了全部 24 张 ——
    AI 生成的提取规则并不知道"三张"这个**数量约束**, 数量必须在下载环节被强制。
    """
    text = (goal or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for pattern in _COUNT_PATTERNS:
        match = re.search(pattern, lowered)
        if not match:
            continue
        value = _to_int(match.group(1))
        if value and value > 0:
            logger.debug(f"从抓取目标里解析出数量约束: {value} (原文 {text[:40]!r})")
            return value
    return None


__all__ = [
    "TransformRule",
    "derive_rule",
    "build_rules",
    "to_original",
    "resolve_limit",
    "parse_count_from_goal",
    "MEDIA_LIMIT_KEY",
]
