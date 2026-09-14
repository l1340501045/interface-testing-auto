"""认证路由：本地账号登录、退出撤销、当前身份。

会话令牌只入 httpOnly Cookie，数据库只保存 SHA-256 摘要；CSRF 令牌另发一个
可读 Cookie 供前端回填请求头，形成双提交校验。
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ...db import get_db
from ...models import Session as LoginSession
from ...models import User, WorkspaceMembership
from ...security import verify_password
from ...services.login_guard import get_limiter
from ..deps import (
    Principal,
    apply_tenant,
    get_principal,
    hash_token,
    new_token,
    require_csrf,
    session_ttl,
    visible_workspaces,
)
from ..errors import unauthorized
from ..schemas import LoginRequest, PrincipalOut, SessionOut, WorkspaceOut

router = APIRouter(tags=["认证"])


def _client_key(request: Request, username: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{username.lower()}|{client}"


def _set_cookies(response: Response, settings: Settings, token: str, csrf: str, expires: datetime) -> None:
    max_age = int((expires - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    # CSRF 令牌需要被前端读取后放入请求头，因此不是 http-only。
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_cookies(response: Response, settings: Settings) -> None:
    # 删除时的属性要与写入时一致：Secure Cookie 只能被同样带 Secure 的
    # Set-Cookie 覆盖，否则浏览器会保留旧值，退出后本地仍带着失效令牌。
    response.delete_cookie(
        settings.cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def _session_payload(session: Session, principal: Principal) -> SessionOut:
    # 成员表与工作空间表都按 app.user_id 过滤，查询前必须先建立主体上下文。
    apply_tenant(session, None, principal.user_id)
    memberships = {
        membership.workspace_id: membership.role
        for membership in session.scalars(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == principal.user_id)
        )
    }
    workspaces = [
        WorkspaceOut(id=workspace.id, name=workspace.name, role=memberships.get(workspace.id, "viewer"))
        for workspace in visible_workspaces(session, principal)
    ]
    return SessionOut(
        user=PrincipalOut(
            user_id=principal.user_id,
            username=principal.username,
            display_name=principal.display_name,
            is_admin=principal.is_admin,
        ),
        workspaces=workspaces,
    )


@router.post("/auth/login", response_model=SessionOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SessionOut:
    limiter = get_limiter(settings.login_max_attempts, settings.login_window_seconds)
    key = _client_key(request, payload.username)
    wait = limiter.retry_after(key)
    if wait:
        from ..errors import too_many_requests

        raise too_many_requests(f"登录尝试过于频繁，请 {wait} 秒后重试")

    user = session.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.password_hash):
        limiter.record_failure(key)
        # 账号不存在与密码错误返回同一提示，不泄露账号是否存在。
        raise unauthorized("账号或密码不正确")
    if user.status != "active":
        raise unauthorized("账号不可用")

    limiter.reset(key)

    token = new_token()
    csrf = new_token()
    expires = datetime.now(UTC) + session_ttl(settings)
    login_session = LoginSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=csrf,
        expires_at=expires,
    )
    session.add(login_session)
    session.commit()

    _set_cookies(response, settings, token, csrf, expires)
    principal = Principal(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        session_id=login_session.id,
    )
    return _session_payload(session, principal)


@router.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    raw = request.cookies.get(settings.cookie_name)
    require_csrf(request, session, raw)
    if raw:
        login_session = session.scalar(
            select(LoginSession).where(LoginSession.token_hash == hash_token(raw))
        )
        if login_session is not None and login_session.revoked_at is None:
            login_session.revoked_at = datetime.now(UTC)
            session.commit()
    _clear_cookies(response, settings)
    # 必须返回被注入的那个 Response：FastAPI 只在处理函数不返回 Response 时才把
    # 注入响应的头合并进最终响应，返回新建的 Response 会把上面的删除 Cookie 头整体丢掉。
    response.status_code = 204
    return response


@router.get("/me", response_model=SessionOut)
def me(
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> SessionOut:
    return _session_payload(session, principal)
