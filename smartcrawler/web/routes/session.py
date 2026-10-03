"""
登录会话接口 —— 手动登录、会话查看、退出登录。

**为什么要"手动登录"而不是自动填表**: 自动填充账号密码涉及凭据的存储/加密/泄露面,
风险与责任远超本框架边界; 而且验证码、短信、扫码这些本来只有人能过。这里的做法是:
打开一个**可见**浏览器让用户自己登录, 框架只保存服务端签发的 Cookie 与 localStorage。
框架自始至终不接触用户密码。

**安全约定**: 所有响应都只返回**掩码摘要**(Cookie 数量、域名、会话类 Cookie 的**名字**),
绝不回传 Cookie 的值 —— 那等同于把账号交出去。会话文件本身在 .gitignore 里。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..service import CrawlService
from . import get_service

router = APIRouter()


class LoginStartRequest(BaseModel):
    """POST /api/session/login —— 打开可见浏览器进行手动登录或手动过验证。"""

    url: str = Field(description="要登录/验证的站点地址, 例如 https://www.pixiv.net/")
    #: login = 等用户登录(默认); challenge = 等用户完成人机验证
    mode: str = Field(default="login", description="login | challenge")
    #: 仅供验收测试: 在打开目标站点前先访问这个地址以获得会话 Cookie,
    #: 用来代替"用户在窗口里手动登录"这一步。正常使用不要传。
    pre_auth_url: Optional[str] = Field(default=None, description="(测试用)预置登录入口")

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        return value

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        value = (value or "login").strip().lower()
        if value not in ("login", "challenge"):
            raise ValueError("mode 只能是 login 或 challenge")
        return value


@router.get("", summary="查看已保存的登录会话(掩码摘要)")
async def get_session(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.session_overview()


@router.post("/login", summary="打开可见浏览器手动登录 / 手动过人机验证")
async def start_login(
    req: LoginStartRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    try:
        return service.start_login(req.url, pre_auth_url=req.pre_auth_url, mode=req.mode)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法启动登录进程: {exc}") from exc


@router.get("/login/status", summary="轮询登录流程进度")
async def login_status(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.login_status()


@router.post("/login/confirm", summary="确认已登录并保存会话(等会话真正落盘后返回)")
async def confirm_login(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.confirm_login()


@router.post("/login/cancel", summary="取消登录流程")
async def cancel_login(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return service.cancel_login()


@router.post("/login/reset", summary="清理登录流程状态并重置浏览器")
async def reset_login(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.reset_login_flow()


class VerifyRequest(BaseModel):
    """POST /api/session/verify —— 用已保存的会话访问一次, 核实是否真的登录了。"""

    url: str = Field(description="要核实的地址")

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("URL 需以 http:// 或 https:// 开头")
        return value


@router.post("/verify", summary="用已保存的会话访问一次, 核实登录是否生效")
async def verify_session(
    req: VerifyRequest,
    service: CrawlService = Depends(get_service),
) -> dict[str, Any]:
    return await service.verify_session_effective(req.url)


@router.delete("", summary="删除已保存的会话(退出登录)")
async def delete_session_route(service: CrawlService = Depends(get_service)) -> dict[str, Any]:
    return await service.clear_session()
