"""请求依赖：会话认证、租户上下文、项目角色校验。

每个请求在同一个数据库事务内先写入 `app.user_id` / `app.workspace_id`，
PostgreSQL 行安全策略据此过滤；应用层再按项目角色做动作授权。
两个防线并行，缺一不可。
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import bind_tenant, get_db
from ..models import Session as LoginSession
from ..models import User, Workspace, WorkspaceMembership
from ..services.permissions import require
from .errors import forbidden, not_found, unauthorized

_CSRF_EXEMPT_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Principal:
    """当前请求的认证主体。"""

    user_id: uuid.UUID
    username: str
    display_name: str
    is_admin: bool
    session_id: uuid.UUID


@dataclass(frozen=True)
class ProjectScope:
    """已校验的项目范围，携带租户上下文与角色。"""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    role: str
    principal: Principal


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def apply_tenant(session: Session, workspace_id: uuid.UUID | None, user_id: uuid.UUID | None) -> None:
    """建立租户上下文；提交后由 db.bind_tenant 登记的会话状态自动重放。"""
    bind_tenant(session, workspace_id, user_id)


def commit(session: Session) -> None:
    """提交即可：租户上下文会在下一个事务开始时自动重放。"""
    session.commit()


def _load_session(session: Session, token: str) -> LoginSession | None:
    now = datetime.now(UTC)
    return session.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == hash_token(token),
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > now,
        )
    )


def authenticate(session: Session, settings: Settings, raw_token: str | None) -> Principal:
    if not raw_token:
        raise unauthorized()
    login = _load_session(session, raw_token)
    if login is None:
        raise unauthorized("会话已失效，请重新登录")
    user = session.get(User, login.user_id)
    if user is None or user.status != "active":
        raise unauthorized("账号不可用")
    return Principal(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        session_id=login.id,
    )


def require_csrf(request: Request, session: Session, raw_token: str | None) -> None:
    """写请求必须携带与会话匹配的 CSRF 令牌（双提交）。"""
    if request.method in _CSRF_EXEMPT_METHODS:
        return
    if not raw_token:
        raise forbidden("csrf_missing", "缺少 CSRF 令牌，请重新登录后再试")
    login = _load_session(session, raw_token)
    if login is None:
        raise unauthorized("会话已失效，请重新登录")
    header = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(header, login.csrf_token):
        raise forbidden("csrf_invalid", "CSRF 令牌校验失败，请刷新页面后重试")


def get_settings_dep() -> Settings:
    return get_settings()


def get_principal(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Principal:
    token = request.cookies.get(settings.cookie_name)
    principal = authenticate(session, settings, token)
    require_csrf(request, session, token)
    apply_tenant(session, None, principal.user_id)
    return principal


def visible_workspaces(session: Session, principal: Principal) -> list[Workspace]:
    """当前主体可访问的工作空间；RLS 只放行本人为成员的工作空间。"""
    apply_tenant(session, None, principal.user_id)
    return list(session.scalars(select(Workspace).order_by(Workspace.created_at)))


def workspace_role(session: Session, principal: Principal, workspace_id: uuid.UUID) -> str:
    """取项目角色；非成员按 404 处理，不泄露其他租户是否存在该工作空间。"""
    apply_tenant(session, workspace_id, principal.user_id)
    membership = session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == principal.user_id,
        )
    )
    if membership is None:
        raise not_found("工作空间不存在或无权访问")
    return membership.role


def project_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    action: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    """项目范围依赖：工作空间成员校验 + 动作授权 + 租户上下文固化。"""
    role = workspace_role(session, principal, workspace_id)
    require(role, action)
    apply_tenant(session, workspace_id, principal.user_id)
    # projects 受 RLS 约束，跨工作空间的 project_id 在此不可见。
    exists = session.scalar(
        text("SELECT 1 FROM app.projects WHERE id = :pid AND workspace_id = :ws"),
        {"pid": str(project_id), "ws": str(workspace_id)},
    )
    if exists is None:
        raise not_found("项目不存在或无权访问")
    return ProjectScope(
        workspace_id=workspace_id, project_id=project_id, role=role, principal=principal
    )


def view_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    return project_scope(workspace_id, project_id, "view", session, principal)


def edit_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    return project_scope(workspace_id, project_id, "edit", session, principal)


def execute_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    return project_scope(workspace_id, project_id, "execute", session, principal)


def admin_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    return project_scope(workspace_id, project_id, "manage_secrets", session, principal)


def pool_admin_scope(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ProjectScope:
    """执行池授权与目标白名单的维护范围。

    与凭证管理同属管理员动作，但动作名分开：白名单决定“能访问哪些目标”，
    凭证决定“以谁的身份访问”，两者的审计与复核不应合成一个开关。
    """
    return project_scope(workspace_id, project_id, "manage_pool_grants", session, principal)


def session_ttl(settings: Settings) -> timedelta:
    return timedelta(seconds=settings.session_ttl_seconds)
