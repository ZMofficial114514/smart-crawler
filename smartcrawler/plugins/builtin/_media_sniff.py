"""内嵌网络抓包: 从已捕获的接口响应里翻出"疑似下载地址", 并**实测验证**后才下载。

## 为什么需要它

越来越多的音乐/视频站点**不把媒体地址放在页面里** —— 页面里没有 ``<audio>``、脚本里也没有
可用地址, 地址只在某个接口的 JSON 里出现, 而且字段名各家不同。

实测酷我音乐的完整链路(用户给的目标 ``kuwo.cn/search/list?key=琵琶曲``):

- 搜索接口 ``/openapi/v1/www/search/searchMusicBykeyWord`` 返回歌单, 但**只有 ``rid``**,
  **没有任何音频地址**;
- 播放页 ``/play_detail/526058813`` 只请求了 ``/www/lyric/getlyric``(歌词),
  **点播放也不产生新的地址请求**;
- 整页唯一带 ``url`` 字段的响应是 ``/api/sl/web/generate_url?file=...kwplayerautolite_C_APK...``
  —— 那是**酷我 PC 客户端安装包**, 与歌曲无关。

也就是说: 页面上那些"看起来重复的 URL"(``down_pc?pack=web_2`` / ``web_6`` /
``down_ar?pack=gw`` / ``car``)是**同一个客户端下载的不同参数**, 而"被截断的 URL"是
``generate_url`` 把目标地址塞在查询参数里显示不全。**都不是歌曲。**

结论: 光靠"从 JSON 里正则捞 URL"会捞到一堆安装包/广告/统计地址。所以本模块做两件事:

1. **找**: 按字段名与取值形态给候选打分, 挑出最像媒体的;
2. **验**: 对候选发一次 ``Range: bytes=0-…`` 的试探请求, **按实际响应的魔数**判断它到底
   是不是音频。只有验证通过的才会交给下载。

## 关于"截断的 URL"

截断的地址在验证阶段会自然暴露 —— 请求它要么 404, 要么返回 HTML。因此不需要额外写
"这个 URL 看起来被截断了吗"的启发式: **试探请求就是最好的判据**。

## 关于 ``api_base64_decode`` 这类包装

酷我的 ``generate_url`` 返回的 ``data.url`` 是**签名后的跳转地址**(里面再嵌真实地址)。
本模块会识别"URL 里嵌了 URL"的形态(``?file=`` / ``?url=`` / ``?u=`` 等参数), 把内层地址
也作为候选 —— 否则只会拿到一层包装, 下不到东西。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger

# ---------------------------------------------------------------------------
# 取值形态: 什么字符串"看起来像媒体地址"
# ---------------------------------------------------------------------------
#: 直接以媒体扩展名结尾(最常见)
_MEDIA_EXT_RE = re.compile(
    r"\.(?:mp3|m4a|flac|wav|aac|ogg|oga|ape|wma|opus|mp4|m4v|webm|mkv|mov|m3u8|mpd)"
    r"(?:$|[?#])",
    re.I,
)
#: 像"取地址"的接口路径(即使没有扩展名也值得一试)
_URLISH_PATH_RE = re.compile(
    r"/(?:url|play|song|music|audio|media|download|stream|source|convert|listen|anti)"
    r"(?:[/_.-]|$)",
    re.I,
)

#: **明显不是媒体**的特征 —— 命中即降权/排除。
#: 实测酷我整页唯一的 url 字段就是客户端安装包, 不排除掉会一直下 APK。
_NOT_MEDIA_RE = re.compile(
    r"\.(?:apk|exe|dmg|pkg|msi|deb|rpm|zip|rar|7z|tar|gz|"
    r"jpg|jpeg|png|gif|webp|bmp|svg|ico|css|js|json|xml|txt|html?|pdf|doc[xm]?|xls[xm]?|"
    r"torrent|crx|xpi)(?:$|[?#])",
    re.I,
)
#: 客户端/更新/统计/广告类关键词 —— 站点里这类地址很多, 且都长得像正常 URL
_NOISE_HINT_RE = re.compile(
    r"kwplayer|autolite|pkgdown|/mbox/|update|upgrade|client|setup|installer|"
    r"analytics|gtag|collect|beacon|log\.|/log/|track|report|ads?[/_.-]|advert|banner|"
    r"popConfig|feedback|weblog|statistic|monitor",
    re.I,
)

#: 字段名里的正向提示(用户在需求里点名的: author / song / album / name / url ...)
_FIELD_HINT_RE = re.compile(
    r"url|uri|link|src|source|play|listen|download|stream|audio|music|media|"
    r"song|album|author|artist|singer|name|rid|id",
    re.I,
)
#: 字段名里的负向提示
_FIELD_NOISE_RE = re.compile(
    r"pic|image|cover|avatar|icon|logo|poster|thumb|head|face|"
    r"lyric|lrc|text|desc|comment|reply|share|page|prev|next",
    re.I,
)

#: 能嵌内层地址的查询参数名(签名跳转 / 代理下载常见)
_INNER_URL_PARAMS = ("file", "url", "u", "src", "source", "target", "link", "href",
                     "download", "path", "proxy", "redirect")

#: 音频魔数 -> 名称。与 `_media_stream.sniff_kind` 保持一致的判定口径。
_AUDIO_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav/riff"),
)


@dataclass
class UrlCandidate:
    """一个候选地址及其来源信息。"""

    url: str
    field: str = ""          # JSON 里的字段路径(用于展示与调试)
    score: int = 0
    source_url: str = ""     # 从哪个接口响应里翻出来的
    reason: str = ""

    def key(self) -> str:
        return self.url.split("#", 1)[0]


@dataclass
class SniffResult:
    """嗅探结果。"""

    candidates: list[UrlCandidate] = field(default_factory=list)
    verified: list[UrlCandidate] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (url, 原因)

    def summary(self) -> str:
        return (f"候选 {len(self.candidates)} 个, 验证通过 {len(self.verified)} 个, "
                f"排除 {len(self.rejected)} 个")


# ---------------------------------------------------------------------------
# 一、从捕获的响应里找候选
# ---------------------------------------------------------------------------
def _walk(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """递归产出 ``(字段路径, 值)``。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _inner_urls(value: str) -> list[str]:
    """若 URL 的查询参数里嵌了另一个 URL, 把它取出来(可能多层)。"""
    out: list[str] = []
    try:
        qs = parse_qs(urlparse(value).query)
    except Exception:  # noqa: BLE001
        return out
    for name in _INNER_URL_PARAMS:
        for raw in qs.get(name) or []:
            inner = unquote(raw)
            if inner.startswith(("http://", "https://")) and inner != value:
                out.append(inner)
    return out


def _score(url: str, field_name: str) -> tuple[int, str]:
    """给候选打分; 返回 ``(分数, 理由)``。分数 <= 0 表示不建议尝试。"""
    if _NOT_MEDIA_RE.search(url):
        return 0, "扩展名明显不是媒体(客户端/图片/脚本等)"
    if _NOISE_HINT_RE.search(url):
        return -50, "命中客户端/统计/广告类关键词"
    score = 0
    reasons: list[str] = []
    if _MEDIA_EXT_RE.search(url):
        score += 60
        reasons.append("以媒体扩展名结尾")
    if _URLISH_PATH_RE.search(urlparse(url).path or ""):
        score += 25
        reasons.append("路径像取址接口")
    if _FIELD_HINT_RE.search(field_name or ""):
        score += 20
        reasons.append(f"字段名含正向提示({field_name})")
    if _FIELD_NOISE_RE.search(field_name or ""):
        score -= 15
        reasons.append(f"字段名像图片/文本({field_name})")
    if url.startswith("https://"):
        score += 2
    if len(url) < 16:
        return 0, "地址过短, 不像完整 URL"
    return score, "; ".join(reasons) or "无明显特征"


def find_candidates(
    records: Iterable[Any],
    *,
    min_score: int = 20,
    max_candidates: int = 80,
) -> list[UrlCandidate]:
    """从网络记录里翻出疑似媒体地址。

    :param records: ``NetworkRecord`` 列表(取 ``body_json`` / ``body_text`` / ``url``)
    :param min_score: 低于此分数的候选丢弃
    """
    found: dict[str, UrlCandidate] = {}

    def add(url: str, field: str, source: str) -> None:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return
        key = url.split("#", 1)[0]
        if key in found:
            return
        score, reason = _score(url, field)
        if score < min_score:
            if score < 0:
                found[key] = UrlCandidate(url=url, field=field, score=score,
                                          source_url=source, reason=reason)
            return
        found[key] = UrlCandidate(url=url, field=field, score=score,
                                  source_url=source, reason=reason)
        # 内层地址(签名跳转/代理)也作为候选 —— 否则只拿到一层包装
        for inner in _inner_urls(url):
            ikey = inner.split("#", 1)[0]
            if ikey in found:
                continue
            iscore, ireason = _score(inner, field)
            if iscore >= min_score:
                found[ikey] = UrlCandidate(url=inner, field=f"{field} -> 内层",
                                           score=iscore, source_url=source,
                                           reason=f"从查询参数解出; {ireason}")

    for rec in records:
        source = getattr(rec, "url", "") or ""
        body_json = getattr(rec, "body_json", None)
        if isinstance(body_json, (dict, list)):
            for path, val in _walk(body_json):
                if isinstance(val, str):
                    add(val, path, source)
            continue
        # 没有解析好的 JSON 就退回文本正则
        text = getattr(rec, "body_text", "") or ""
        if not text:
            continue
        for m in re.finditer(r"https?://[^\s\"'<>\\]{12,500}", text):
            add(m.group(0), "(文本正则)", source)

    out = [c for c in found.values() if c.score >= min_score]
    out.sort(key=lambda c: (-c.score, len(c.url)))
    return out[:max_candidates]


# ---------------------------------------------------------------------------
# 二、试探验证: 只有真的返回音频才用
# ---------------------------------------------------------------------------
def looks_like_audio(head: bytes) -> Optional[str]:
    """按魔数判断是否音频; 是则返回类型名。"""
    if not head:
        return None
    for magic, name in _AUDIO_MAGIC:
        if head.startswith(magic):
            return name
    # ID3 之外: MPEG 帧同步 0xFF Ex
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4/m4a"
    return None


async def probe_url(
    client: Any,
    url: str,
    *,
    referer: str = "",
    timeout: float = 20.0,
    head_bytes: int = 2048,
) -> tuple[bool, str]:
    """对候选发一次小范围请求, 按实际内容判断是不是音频。

    返回 ``(是否可用, 说明)``。**这是"试"的核心**: 不猜、只看真实响应 ——
    截断的地址会 404 或返回 HTML, 客户端安装包会返回 APK 魔数, 都会被这一步筛掉。
    """
    headers = {"Range": f"bytes=0-{head_bytes - 1}", "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败: {type(exc).__name__}"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}"
    head = resp.content[:head_bytes]
    kind = looks_like_audio(head)
    if kind:
        return True, f"内容为音频({kind})"
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype.startswith(("audio/", "video/")):
        # 类型声称是媒体但魔数没认出来 —— 也接收(有些站点返回裸流)
        return True, f"Content-Type={ctype}"
    if head[:2] == b"PK":
        return False, "内容是 ZIP/APK(客户端安装包)"
    if head[:1] in (b"{", b"[") or head[:1] == b"<":
        return False, f"内容是 JSON/HTML, 不是媒体(Content-Type={ctype or '未知'})"
    return False, f"无法识别的内容(Content-Type={ctype or '未知'}, 头={head[:8].hex(' ')})"


async def sniff_and_verify(
    records: Iterable[Any],
    *,
    client: Any,
    referer: str = "",
    min_score: int = 20,
    max_candidates: int = 80,
    max_probe: int = 12,
    on_log: Any = None,
) -> SniffResult:
    """找候选 -> 逐个试探 -> 只留下验证通过的。

    :param max_probe: 最多试探几个候选。站点接口里噪声很多, 全试一遍既慢又像攻击。
    """
    result = SniffResult()
    result.candidates = find_candidates(records, min_score=min_score,
                                        max_candidates=max_candidates)

    def log(msg: str) -> None:
        logger.info(msg)
        if on_log:
            on_log(msg)

    if not result.candidates:
        log("网络嗅探: 捕获的响应里没有发现疑似媒体地址")
        return result

    log(f"网络嗅探: 找到 {len(result.candidates)} 个疑似地址, 开始试探前 "
        f"{min(len(result.candidates), max_probe)} 个")
    for cand in result.candidates[:max_probe]:
        ok, why = await probe_url(client, cand.url, referer=referer)
        if ok:
            cand.reason = f"{cand.reason} | 验证通过: {why}"
            result.verified.append(cand)
            log(f"网络嗅探: 验证通过 {cand.url[:90]} ({why}; 来源字段 {cand.field})")
        else:
            result.rejected.append((cand.url, why))
            logger.debug(f"网络嗅探: 排除 {cand.url[:90]} ({why})")
    log(f"网络嗅探完成: {result.summary()}")
    return result


__all__ = [
    "SniffResult",
    "UrlCandidate",
    "find_candidates",
    "looks_like_audio",
    "probe_url",
    "sniff_and_verify",
]
