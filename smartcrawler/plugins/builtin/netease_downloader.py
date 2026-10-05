"""网易云音乐下载器: 用登录会话换取真实音频直链后下载。

## 为什么需要单独的插件

网易云**不把音频地址放在页面里**: 歌曲页没有 ``<audio>`` 元素, 页面脚本里也没有可用的
音频 URL, 播放地址要经 ``weapi`` 加密请求 ``/song/url/v1`` 换取。所以"抓页面 + 下载"
这条路在网易云上走不通 —— 实测抓到的 ``link`` 是**歌曲页地址**, 直接下它只会拿到 HTML。

本插件用 `pymusiclibrary <https://github.com/2061360308/MusicLibrary>`_(MIT)完成加密与
取址: 它内部用 QuickJS 执行 NeteaseCloudMusicApi 的 JS 逻辑, 因此**不需要我们重新实现
weapi 加密**。取到的是**直链 mp3/m4a**, 通常不是 m3u8 流。

## 关于 ffmpeg

网易云给的是**直链**, 所以本插件正常情况下**不需要 ffmpeg** —— 音频直接由框架流式下载。
ffmpeg 只在拿到**流式地址**(m3u8/mpd)时才派上用场, 那条路径由
:mod:`._media_stream` 统一处理(音频/视频下载器共用), 作用是:

1. **拉流 + 合并**: m3u8 只是播放列表(几 KB 文本), 真正的音频在一堆 ``.ts`` 分片里。
   ffmpeg 按列表把分片拉下来拼成一个完整容器。不做这一步就会把播放列表当"音频"存下来。
2. **转封装而非重编码**(``-c copy``): 只换容器、不改变音频数据本身, 所以**快且无损**。
3. **落盘后把关的配套**: 合并产物要能被 ffprobe 解析出时长才算成功(见 ``verify_media_file``)。

也就是说: **ffmpeg 是"流式音频"这条支路的依赖, 不是网易云插件的依赖**。

## 合规

音频受版权保护。本插件按用户要求仅用于个人收藏鉴赏, 请勿传播。使用时请遵守网易云
服务条款; 插件本身只做"用你自己的会话换取播放地址"这一件事。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..base import BasePlugin, PluginContext
from ._media import absolute_url, download_many, urls_from_items
from ._media_stream import (
    download_streams,
    is_stream_url,
    resolve_renamed,
    verify_media_file,
)

#: 音频场景的容器 -> 扩展名(mp3 容器落 .mp3, mp4 容器落 .m4a)
_AUDIO_EXT: dict[str, str] = {
    "mp3": ".mp3",
    "mp4": ".m4a",
    "matroska": ".mka",
    "ogg": ".ogg",
    "flac": ".flac",
    "mpegts": ".ts",
}

#: 音质档位网易云接受的值(由高到低)。账号权限不足时会自动降级到能取到的最高档。
_LEVELS = ("lossless", "hires", "exhigh", "higher", "standard")

#: 从 URL 里认歌曲 id 的形态: /song?id=123、/song/123、#/song?id=123、song?id=123
_SONG_ID_RE = re.compile(r"(?:^|[/?#&])song(?:\.php)?(?:/|\?|#|$)[^0-9]{0,12}(\d{2,})", re.I)
#: 兜底: 任何位置出现的 id=数字(用于用户直接把链接塞进字段的情形)
_ID_QUERY_RE = re.compile(r"[?&#]id=(\d{2,})")


def _song_id_from_url(url: str) -> str:
    """从歌曲页 URL 里取出歌曲 id。取不到返回空串。

    **必须同时支持两种写法**: 网易云的网页地址是 ``/song?id=123``, 而它的移动端/API
    形态是 ``/song/123`` —— 实测用户手上两种都有, 只认一种会漏掉一半。
    """
    if not url:
        return ""
    m = _SONG_ID_RE.search(url)
    if m:
        return m.group(1)
    # 兜底: /song?id=123 里的 id 参数
    if "/song" in url.lower():
        m2 = _ID_QUERY_RE.search(url)
        if m2:
            return m2.group(1)
    return ""


def _cookies_from_storage_state(path: Path) -> dict[str, str]:
    """把 Playwright 的 ``storage_state`` 转成网易云的 ``{name: value}`` cookie。

    只取 ``163.com`` 域下的条目 —— 会话文件里混着各站点的 cookie(实测 74 个里只有约 40 个
    属于网易云), 全塞进去会让请求头臃肿, 还可能把别的站点的会话泄露给网易云。
    """
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取会话文件失败({path}): {exc}")
        return {}
    jar: dict[str, str] = {}
    for c in state.get("cookies") or []:
        if "163.com" not in (c.get("domain") or ""):
            continue
        name, value = c.get("name"), c.get("value")
        if name and value is not None:
            jar[name] = value
    return jar


def _load_pymusiclibrary() -> Any:
    """延迟导入 pymusiclibrary。未安装时返回 None(插件跳过并给出安装提示)。"""
    try:
        from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi  # noqa: PLC0415

        return NeteaseCloudMusicApi
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"pymusiclibrary 不可用: {type(exc).__name__}: {exc}")
        return None


@contextlib.contextmanager
def _quiet_library():
    """屏蔽 pymusiclibrary 的 stdout 输出。

    **这关系到会话凭据安全, 不只是"看着乱"。** 该库每次调用都会把**完整 cookie**
    (含 ``MUSIC_U`` 登录令牌)打印成 ``[ROUTE] route: /song/url/v1, cookie: {...}``。
    任务日志会被界面收集并落盘到 ``logs/``, 等于把登录令牌写进了日志文件。

    **必须做文件描述符级重定向**: 那段输出来自 native 库(``ncm_music_api.dll``)对
    进程 stdout 的直接写入, Python 的 ``contextlib.redirect_stdout`` 只替换
    ``sys.stdout``, 拦不住它 —— 实测加 ``redirect_stdout`` 后 ``[ROUTE]`` 照旧出现在
    控制台上。所以这里用 ``os.dup2`` 把 fd 1 临时接到一个临时文件。

    同时屏蔽的还有 ``[INIT] Anonymous token`` 与 ``Registering environment variables``
    几行 —— 它们同样由 native 库打印。
    """
    import tempfile

    try:
        saved = os.dup(1)
    except OSError:
        yield  # 没有可用的 fd(极少数环境), 退回不做重定向
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        yield
    finally:
        try:
            os.dup2(saved, 1)
        finally:
            os.close(saved)
            os.close(devnull)


#: 判定"瞬时失败"的错误特征 —— 这类失败重试一次往往就好了。
#: 对应 issue #5: 网易云 CDN 偶发 ReadTimeout, 没有重试会让整个任务看起来像坏了。
_TRANSIENT_RE = re.compile(
    r"timeout|timed out|超时|connect|connection|reset|refused|unreachable|"
    r"Temporary failure|502|503|504|429|too many requests",
    re.I,
)


def _is_transient_error(error: Any) -> bool:
    """错误是否属于"值得重试"的瞬时问题。

    **不重试**的情况: HTTP 403/404(地址失效或防盗链)、类型不符、完整性校验未通过 ——
    这些再试多少次结果都一样, 重试只会浪费时间并加重对方限流。
    """
    text = str(error or "")
    if not text:
        return False
    # 永久性错误优先排除
    if re.search(r"\b(?:40[0-9])\b", text) and not re.search(r"\b429\b", text):
        return False
    if "类型不符" in text or "完整性校验" in text or "超过大小上限" in text:
        return False
    return bool(_TRANSIENT_RE.search(text))


def _safe_name(text: str, limit: int = 80) -> str:
    """把歌名/歌手变成安全的文件名片段。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit]


def _rename_with_metadata(record: Any, meta: dict[str, str]) -> None:
    """把下载产物按「歌手 - 歌名」重命名。

    默认文件名是 ``<url 哈希>_<哈希>.mp3``, 对用户等于没有信息 —— 下 20 首之后完全无法
    辨认哪首是哪首。抓取结果里本来就有歌名与歌手, 顺手用上即可。
    """
    path = Path(record.path or "")
    if not path.is_file():
        return
    artist = _safe_name(meta.get("artist") or "", 60)
    title = _safe_name(meta.get("title") or "", 80)
    if not title and not artist:
        return
    stem = f"{artist} - {title}" if (artist and title) else (title or artist)
    if not stem:
        return
    target = path.with_name(f"{stem}{path.suffix}")
    if target == path:
        return
    # 同名(同名歌曲/同歌手多版本)时加序号, 不覆盖已有文件
    n = 2
    while target.exists():
        target = path.with_name(f"{stem} ({n}){path.suffix}")
        n += 1
        if n > 50:
            return
    try:
        path.rename(target)
    except OSError as exc:
        logger.debug(f"重命名失败({path.name}): {exc}")
        return
    record.path = str(target)
    record.filename = target.name


class NeteaseMusicPlugin(BasePlugin):
    """用登录会话从网易云换取音频直链并下载(默认关闭)。"""

    id = "netease-music"
    name = "网易云音乐下载器"
    description = "用登录会话换取网易云音频直链并下载到本地(仅供个人收藏鉴赏)"
    version = "1.0.0"
    author = ""
    category = "download"
    tags = ["音频", "网易云", "需登录", "需授权"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {
            "key": "session_file",
            "label": "登录会话文件",
            "type": "str",
            "default": "data/session.json",
            "description": (
                "Playwright 保存的会话文件(界面「登录一次」写入的那份)。"
                "插件只读取其中 163.com 域的 Cookie, 不回传、不落日志。"
            ),
        },
        {
            "key": "level",
            "label": "音质档位",
            "type": "str",
            "default": "exhigh",
            "description": (
                "lossless(无损) / hires / exhigh(极高) / higher(较高) / standard(标准)。"
                "权限不足或该曲受限时会自动降到能取到的档位。"
            ),
        },
        {
            "key": "max_audio",
            "label": "最多下载音频数",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 500,
            "description": "本次任务最多下载几首。优先级: 抓取页「下载数量」> 本项。",
        },
        {
            "key": "subdir",
            "label": "保存子目录",
            "type": "str",
            "default": "music",
        },
        {
            "key": "concurrency",
            "label": "并发下载数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 8,
            "description": "音频文件较大, 建议不超过 3 —— 网易云对高频请求会限流。",
        },
        {
            "key": "max_file_size_mb",
            "label": "单文件大小上限(MB)",
            "type": "int",
            "default": 80,
            "min": 1,
            "max": 2048,
        },
        {
            "key": "download_cover",
            "label": "同时下载封面",
            "type": "bool",
            "default": False,
            "description": "把专辑封面一并存到同一目录(文件名与音频同名加 _cover)。",
        },
        {
            "key": "item_field",
            "label": "记录中的歌曲字段",
            "type": "str",
            "default": "link",
            "description": (
                "默认 link —— 抓取网易云列表时, 记录里的歌曲页地址就在这个字段。"
                "留空则自动尝试 link/url/song_url/song_id 等常见名字。"
            ),
        },
    ]

    # ------------------------------------------------------------------
    # 取址
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_song_ids(
        ctx: PluginContext, items: list[dict[str, Any]]
    ) -> list[tuple[str, int, dict[str, str]]]:
        """从记录里收集 (歌曲 id, 记录序号, 元数据), 保持顺序并去重。

        **不往 PluginContext 上挂临时属性**: 那是插件之间的公共对象, 随手加字段会和别的
        插件(或未来版本的基类)撞名, 也让"插件能改什么"变得不可控。需要的数据直接传参。

        顺带把歌名/歌手一起带出来 —— 抓取结果里本来就有, 用来给下载文件命名,
        否则产物只能叫一串哈希, 下多了根本认不出哪首是哪首。
        """
        configured = str(ctx.config.get("item_field") or "").strip()
        fields = [configured] if configured else ["link", "url", "song_url", "song_link",
                                                 "song_id", "id", "href"]
        out: list[tuple[str, int, dict[str, str]]] = []
        seen: set[str] = set()
        for idx, item in enumerate(items):
            sid = ""
            for field in fields:
                raw = item.get(field)
                if raw is None:
                    continue
                if isinstance(raw, (int, float)):
                    raw = str(int(raw))
                if not isinstance(raw, str) or not raw.strip():
                    continue
                sid = _song_id_from_url(absolute_url(ctx.url, raw))
                if not sid and raw.strip().isdigit():
                    sid = raw.strip()
                if sid:
                    break
            if not sid or sid in seen:
                continue
            seen.add(sid)
            meta = {
                k: str(item.get(k) or "").strip()
                for k in ("title", "song_name", "name", "artist", "artists", "album")
                if item.get(k)
            }
            out.append((sid, idx, meta))
        return out

    def _fetch_urls(
        self, ctx: PluginContext, song_ids: list[tuple[str, int]], jar: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        """向网易云换取音频地址, 返回 ``{song_id: {...}}``(只包含取到地址的)。"""
        api_cls = _load_pymusiclibrary()
        if api_cls is None:
            ctx.notify(
                "WARNING",
                "网易云下载器: 未安装 pymusiclibrary, 无法换取音频地址。"
                "安装命令: pip install pymusiclibrary",
            )
            return {}

        wanted = str(ctx.config.get("level") or "exhigh").strip().lower()
        # 从配置的档位开始, 依次尝试到 standard —— 权限不足或单曲受限时自动降级
        start = _LEVELS.index(wanted) if wanted in _LEVELS else 2
        levels = list(_LEVELS[start:]) or ["standard"]

        found: dict[str, dict[str, Any]] = {}
        with _quiet_library():
            api = api_cls()
            for sid, _index, _meta in song_ids:
                if sid in found:
                    continue
                for level in levels:
                    try:
                        resp = api.song_url_v1(id=sid, level=level, cookie=jar)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"网易云取址失败(id={sid}, level={level}): "
                                       f"{type(exc).__name__}: {exc}")
                        continue
                    data = (resp.body or {}).get("data") or []
                    item = data[0] if data else {}
                    url = item.get("url") or ""
                    if not url:
                        continue
                    found[sid] = {
                        "url": url,
                        "size": item.get("size") or 0,
                        "br": item.get("br") or 0,
                        "type": (item.get("type") or "").upper(),
                        "level": item.get("level") or level,
                        "fee": item.get("fee"),
                    }
                    logger.info(
                        f"网易云取址成功: id={sid} level={item.get('level') or level} "
                        f"type={item.get('type')} br={item.get('br')} size={item.get('size')}"
                    )
                    break
                else:
                    logger.info(f"网易云未提供可下载地址(可能受版权限制): id={sid}")
        return found

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def _download_direct(
        self, ctx: PluginContext, urls: list[tuple[str, Optional[int]]],
        subdir: str, max_bytes: int, meta_by_index: dict[int, dict[str, str]] | None = None,
    ) -> list:
        """直链下载(网易云正常情况下走这条) + 落盘后的容器校验 + 按歌名重命名。

        **Referer 必须带上**: 网易云的 CDN 会校验来源, 缺 Referer 时返回 403 ——
        这不是"防盗链误伤", 而是它明确要求的请求头。
        """
        meta_by_index = meta_by_index or {}
        results = await download_many(
            ctx,
            urls,
            plugin_id=self.id,
            subdir=subdir,
            referer="https://music.163.com/",
            max_file_size=max_bytes,
            concurrency=int(ctx.config.get("concurrency") or 2),
            allowed_types=("audio/", "video/mp4", "application/ogg",
                           "application/octet-stream", "binary/octet-stream"),
            # 网易云 CDN 对部分地址返回 application/octet-stream 甚至不带类型,
            # 类型判定交给落盘后的魔数校验(它认 mp3/m4a/flac 等)。
            verify_type_by_content=True,
        )

        # 失败的单曲重试一轮。
        #
        # **为什么需要**(对应 issue #5): 网易云 CDN 偶发读超时, 实测 10 首里会有 1 首
        # `ReadTimeout`。原先没有重试, 一首失败就让整个任务带上错误、状态变 failed, 用户看到
        # "插件坏了" —— 而实际上再试一次就成功。音频文件大、并发又低, 一次重试的成本远低于
        # 让用户以为功能失效。
        retryable = [r for r in results if not r.ok and _is_transient_error(r.error)]
        if retryable:
            logger.info(f"网易云下载器: {len(retryable)} 首疑似瞬时失败, 重试一轮")
            pairs = [(r.url, getattr(r, "source_item_index", None)) for r in retryable]
            retried = await download_many(
                ctx, pairs, plugin_id=self.id, subdir=subdir,
                referer="https://music.163.com/", max_file_size=max_bytes,
                concurrency=1,  # 重试时降并发, 避免再被限流/超时
                allowed_types=("audio/", "video/mp4", "application/ogg",
                               "application/octet-stream", "binary/octet-stream"),
                verify_type_by_content=True,
            )
            # 用重试结果替换原记录(同 URL 的成败状态以最后一次为准)
            by_url = {r.url: r for r in retried}
            for i, r in enumerate(results):
                if r.url in by_url:
                    results[i] = by_url[r.url]

        for record in results:
            if not record.ok or not record.path:
                continue
            problem = verify_media_file(Path(record.path), ext_for_kind=_AUDIO_EXT)
            if problem is None:
                actual = resolve_renamed(Path(record.path))
                record.path = str(actual)
                record.filename = actual.name
                record.size = actual.stat().st_size
                # 用抓取结果里的歌名/歌手命名, 否则产物只有一串哈希, 下多了认不出
                idx = getattr(record, "source_item_index", None)
                if idx is not None and idx in meta_by_index:
                    _rename_with_metadata(record, meta_by_index[idx])
                continue
            record.ok = False
            record.error = f"完整性校验未通过: {problem}"
            for candidate in {Path(record.path), resolve_renamed(Path(record.path))}:
                with contextlib.suppress(OSError):
                    candidate.unlink(missing_ok=True)
            record.path = ""
            record.size = 0
            logger.warning(f"网易云音频校验未通过已删除: {problem}")
        return results

    async def after_extract(self, ctx: PluginContext, items: list[dict[str, Any]]):
        song_ids = self._collect_song_ids(ctx, items)
        if not song_ids:
            ctx.notify("DEBUG", "网易云下载器: 记录里没有歌曲链接(需要 /song?id=... 形态的字段)")
            return items

        limit = min(len(song_ids), int(ctx.config.get("max_audio") or 20))
        task_limit = (getattr(ctx, "data", None) or {}).get("media_limit")
        if task_limit:
            try:
                limit = min(limit, int(task_limit))
            except (TypeError, ValueError):
                pass
        song_ids = song_ids[:limit]

        session_file = Path(str(ctx.config.get("session_file") or "data/session.json"))
        jar = _cookies_from_storage_state(session_file)
        if not jar:
            ctx.notify(
                "WARNING",
                f"网易云下载器: 会话文件里没有网易云 Cookie({session_file}) —— "
                "请先在界面点「登录一次」登录网易云",
            )
            return items
        if "MUSIC_U" not in jar:
            ctx.notify(
                "WARNING",
                "网易云下载器: 会话里缺少 MUSIC_U(登录凭据) —— 当前多半是匿名会话, "
                "请重新登录网易云; 匿名状态下大部分歌曲取不到地址",
            )
        ctx.notify("INFO", f"网易云下载器: 准备换取 {len(song_ids)} 首歌的音频地址")

        found = self._fetch_urls(ctx, song_ids, jar)
        if not found:
            ctx.notify(
                "WARNING",
                f"网易云下载器: {len(song_ids)} 首歌都没能取到可下载地址"
                "(常见原因: 该曲受版权限制、账号无该音质权限、或会话已过期)",
            )
            return items

        subdir = str(ctx.config.get("subdir") or "music")
        max_bytes = int(ctx.config.get("max_file_size_mb") or 80) * 1024 * 1024

        direct: list[tuple[str, Optional[int]]] = []
        streams: list[str] = []
        index_of = {sid: idx for sid, idx, _meta in song_ids}
        meta_by_index = {idx: meta for _sid, idx, meta in song_ids}
        for sid, info in found.items():
            url = info["url"]
            if is_stream_url(url):
                streams.append(url)
            else:
                direct.append((url, index_of.get(sid)))

        if direct:
            await self._download_direct(ctx, direct, subdir, max_bytes,
                                        meta_by_index=meta_by_index)
        if streams:
            ctx.notify("INFO", f"网易云下载器: {len(streams)} 个地址是流式(m3u8/mpd), 交给 ffmpeg")
            await download_streams(
                ctx,
                streams,
                plugin_id=self.id,
                subdir=subdir,
                ext_for_kind=_AUDIO_EXT,
                default_ext=".m4a",
                hls_enabled=bool(ctx.config.get("hls_enabled", True)),
                ffmpeg_path=str(ctx.config.get("ffmpeg_path") or ""),
                timeout=int(ctx.config.get("ffmpeg_timeout_s") or 600),
                max_bytes=max_bytes,
            )

        missing = [sid for sid, _idx, _meta in song_ids if sid not in found]
        if missing:
            ctx.notify(
                "WARNING",
                f"网易云下载器: {len(missing)} 首未取到地址(多为版权/权限限制): {missing[:5]}",
            )
        return items


__all__ = ["NeteaseMusicPlugin"]
