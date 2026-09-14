"""身份与隔离模型：工作空间、用户、成员、会话。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = {"comment": "工作空间（租户），数据隔离边界"}

    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="工作空间显示名称")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/disabled"
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = {"comment": "平台账号，与被测系统业务账号分离"}

    username: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="登录名，全局唯一"
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False, comment="密码哈希（scrypt），敏感值")
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="显示名称")
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="是否平台级管理员"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/disabled"
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_memberships_ws_user"),
        {"comment": "工作空间成员，承载最低项目角色"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.workspaces.id", ondelete="CASCADE"), nullable=False, comment="所属工作空间"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False, comment="成员用户"
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="viewer", server_default="viewer", comment="角色：admin/editor/viewer"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="加入时间"
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"comment": "平台登录会话，可撤销"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False, comment="会话所属用户"
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="会话令牌 SHA-256 摘要；令牌只入 http-only Cookie"
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False, comment="CSRF 令牌")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="过期时间")
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="撤销时间，空表示有效"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间"
    )
