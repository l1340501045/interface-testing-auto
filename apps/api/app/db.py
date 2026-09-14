"""数据库引擎、会话与 RLS 租户上下文。

运行角色 `app_runtime` 无 BYPASSRLS、非表所有者。每个事务通过
`SET LOCAL app.workspace_id` / `app.user_id` 建立租户与主体上下文，
PostgreSQL 行安全策略据此过滤，作为应用授权的独立防线。

`SET LOCAL` 只活一个事务，提交即失效。执行内核会在一次运行中多次提交，
因此租户上下文不能只在入口设一次：`bind_tenant` 记录当前租户，`after_begin`
在每次新事务开始时自动重放。否则“提交后继续查询”会静默读到空集，表现为
环境或运行“不存在”，既难定位，也会让 RLS 看起来时灵时不灵。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


@event.listens_for(Session, "after_begin")
def _reapply_tenant_on_begin(session: Session, _transaction: object, connection: object) -> None:
    """每个新事务开始时重放已绑定的租户，避免提交后丢失上下文。

    用连接而不是 session 执行：在 after_begin 里走 session 会再次触发自动
    flush／begin，形成递归。
    """
    tenant = session.info.get("tenant")
    if tenant is None:
        return
    workspace_id, user_id = tenant
    connection.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": workspace_id}
    )
    connection.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_id}
    )


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """业务事务：建立租户/主体上下文后使用，结束自动回滚/提交。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def set_tenant_context(session: Session, workspace_id: uuid.UUID | None, user_id: uuid.UUID | None) -> None:
    """SET LOCAL 只在当前事务有效，事务结束自动清除，连接归还不残留。"""
    session.execute(
        text("SELECT set_config('app.workspace_id', :ws, true)"),
        {"ws": str(workspace_id) if workspace_id else ""},
    )
    session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": str(user_id) if user_id else ""},
    )


def bind_tenant(session: Session, workspace_id: uuid.UUID | None, user_id: uuid.UUID | None = None) -> None:
    """建立租户上下文并记住它，后续每个事务开始都会自动重放。"""
    session.info["tenant"] = (
        str(workspace_id) if workspace_id else "",
        str(user_id) if user_id else "",
    )
    set_tenant_context(session, workspace_id, user_id)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。租户上下文由认证依赖在进入业务前设置。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
