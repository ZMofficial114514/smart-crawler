"""
登录会话管理 —— 保存/加载 Playwright 的 ``storage_state``(Cookie + localStorage)。

**为什么需要它**: 有些内容必须登录才能看到(还可能是"登录后页面结构完全不同", 例如
pixiv 的首页 —— 未登录是注册/登录引导页, 登录后才是作品瀑布流)。而自动填账号密码涉及
凭据管理, 风险与责任都远超框架边界。所以采用**手动登录一次 + 复用会话**的方案:

1. 用**有头**(可见)浏览器打开目标站点;
2. 用户自己完成登录(含验证码/短信/扫码, 这些只有人能过);
3. 框架把 Cookie 与 localStorage 存成 JSON;
4. 之后每次抓取自动带上这份会话。

这样框架**从不接触用户密码**, 只保存服务端签发的会话凭据。

安全说明: 这个文件等同于登录凭据, 泄露即等于账号被冒用。因此
``data/sessions/`` 与 ``data/session.json`` 都在 ``.gitignore`` 里, 且接口只返回掩码摘要。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

from .config import PROJECT_ROOT

#: 会话文件默认位置(与 BrowserConfig.storage_state 的默认约定一致)
DEFAULT_SESSION_PATH = PROJECT_ROOT / "data" / "session.json"
#: 按站点分存的会话目录(自建, 用于多站点场景)
SESSION_DIR = PROJECT_ROOT / "data" / "sessions"


def site_slug(url: str) -> str:
    """把 URL 变成一个安全的文件名片段(如 ``www.pixiv.net`` -> ``www.pixiv.net``)。"""
    host = urlparse(url).hostname or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


@dataclass
class SessionSummary:
    """会话摘要 —— 刻意**不含**凭据内容, 可安全地回传给前端。"""

    exists: bool = False
    path: str = ""
    cookies: int = 0
    origins: int = 0
    domains: list[str] = field(default_factory=list)
    #: 是否包含看起来与登录相关的 Cookie(仅凭名字判断, 不看值)
    has_auth_cookie: bool = False
    auth_cookies: list[str] = field(default_factory=list)
    saved_at: Optional[str] = None
    size: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "path": self.path,
            "cookies": self.cookies,
            "origins": self.origins,
            "domains": self.domains,
            "has_auth_cookie": self.has_auth_cookie,
            "auth_cookies": self.auth_cookies,
            "saved_at": self.saved_at,
            "size": self.size,
            "error": self.error,
        }


#: Cookie 名里出现这些词, 通常意味着"这是登录态"
_AUTH_COOKIE_HINTS = (
    "session", "sess", "sid", "token", "auth", "login", "logged", "jwt",
    "phpsessid", "jsessionid", "asp.net_sessionid", "remember", "uid", "userid",
)


def summarize_session(path: Path | str = DEFAULT_SESSION_PATH) -> SessionSummary:
    """读取会话文件并生成摘要(不泄露凭据值)。"""
    file_path = Path(path)
    summary = SessionSummary(path=str(file_path))
    if not file_path.exists():
        return summary

    try:
        stat = file_path.stat()
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        summary.error = f"{type(exc).__name__}: {exc}"
        return summary

    summary.exists = True
    summary.size = stat.st_size
    summary.saved_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")

    cookies = data.get("cookies") or []
    origins = data.get("origins") or []
    summary.cookies = len(cookies)
    summary.origins = len(origins)

    domains: list[str] = []
    auth_names: list[str] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".")
        if domain and domain not in domains:
            domains.append(domain)
        name = str(cookie.get("name") or "")
        lowered = name.lower()
        # 会话类 Cookie 通常不是 30 天以上的长期项, 但名字特征更可靠
        if any(hint in lowered for hint in _AUTH_COOKIE_HINTS):
            if name not in auth_names:
                auth_names.append(name)
    for origin in origins:
        host = urlparse(str(origin.get("origin") or "")).hostname
        if host and host not in domains:
            domains.append(host)

    summary.domains = domains[:20]
    summary.auth_cookies = auth_names[:10]
    summary.has_auth_cookie = bool(auth_names)
    return summary


def save_storage_state(state: dict[str, Any], path: Path | str = DEFAULT_SESSION_PATH) -> Path:
    """把 Playwright 的 storage_state 落盘(自动建目录)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    # 会话文件等同凭据, 尽量收紧权限(Windows 上 chmod 作用有限, 但仍表达意图)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    logger.info(
        f"登录会话已保存: {target} "
        f"({len(state.get('cookies') or [])} 个 Cookie, {len(state.get('origins') or [])} 个站点 localStorage)"
    )
    return target


def merge_storage_state(
    base: Optional[dict[str, Any]], incoming: dict[str, Any]
) -> dict[str, Any]:
    """把新会话合并进已有会话(按 cookie 的 name+domain+path 去重, 新值覆盖旧值)。

    这样可以在保留旧站点登录态的同时, 追加/更新某个站点的会话。
    """
    merged: dict[str, list[dict[str, Any]]] = {
        "cookies": list((base or {}).get("cookies") or []),
        "origins": list((base or {}).get("origins") or []),
    }

    def cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )

    index = {cookie_key(c): i for i, c in enumerate(merged["cookies"])}
    for cookie in incoming.get("cookies") or []:
        key = cookie_key(cookie)
        if key in index:
            merged["cookies"][index[key]] = cookie
        else:
            index[key] = len(merged["cookies"])
            merged["cookies"].append(cookie)

    origin_index = {
        str(o.get("origin") or ""): i for i, o in enumerate(merged["origins"])
    }
    for origin in incoming.get("origins") or []:
        key = str(origin.get("origin") or "")
        if key in origin_index:
            merged["origins"][origin_index[key]] = origin
        else:
            origin_index[key] = len(merged["origins"])
            merged["origins"].append(origin)

    return merged


def delete_session(path: Path | str = DEFAULT_SESSION_PATH) -> bool:
    """删除会话文件(退出登录)。"""
    target = Path(path)
    if not target.exists():
        return False
    try:
        target.unlink()
        logger.info(f"登录会话已删除: {target}")
        return True
    except OSError as exc:
        logger.warning(f"删除会话文件失败: {exc}")
        return False


def resolve_session_path(configured: str = "") -> Path:
    """解析会话文件路径: 配置值优先, 否则用默认位置。"""
    if not configured:
        return DEFAULT_SESSION_PATH
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path
