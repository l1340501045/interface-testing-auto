"""项目与环境模型：项目、变量版本、环境、执行池及项目授权。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def project_fk(name: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """项目级复合外键：校验 workspace_id/project_id 归属一致。"""
    return ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["app.projects.workspace_id", "app.projects.id"],
        name=name,
        ondelete=ondelete,
    )


def project_object_fk(columns: list[str], referred_table: str, name: str, ondelete: str = "CASCADE", use_alter: bool = False) -> ForeignKeyConstraint:
    """项目内对象复合外键：workspace_id/project_id 随对象 id 绑定，防跨项目引用。

    columns 为引用对象 id 的列名（可含可空列）；被引用表必须有 (workspace_id, project_id, id) 唯一键。
    """
    return ForeignKeyConstraint(
        ["workspace_id", "project_id", *columns],
        [f"app.{referred_table}.workspace_id", f"app.{referred_table}.project_id", f"app.{referred_table}.id"],
        name=name,
        ondelete=ondelete,
        use_alter=use_alter,
    )


def workspace_object_fk(columns: list[str], referred_table: str, name: str, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """工作空间内对象复合外键：workspace_id 随对象 id 绑定，防跨工作空间引用。

    columns 为引用对象 id 的列名；被引用表必须有 (workspace_id, id) 唯一键。
    """
    return ForeignKeyConstraint(
        ["workspace_id", *columns],
        [f"app.{referred_table}.workspace_id", f"app.{referred_table}.id"],
        name=name,
        ondelete=ondelete,
    )


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_projects_ws_id"),
        UniqueConstraint("workspace_id", "key", name="uq_projects_ws_key"),
        {"comment": "项目，业务资源的直接归属层"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.workspaces.id", ondelete="CASCADE"), nullable=False, comment="所属工作空间"
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False, comment="项目键，工作空间内唯一")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="项目显示名称")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/archived"
    )


class ProjectConfigVersion(Base):
    __tablename__ = "project_config_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "version", name="uq_project_config_versions_version"),
        project_fk("fk_project_config_versions_project"),
        {"comment": "项目普通变量不可变配置修订"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号，项目内递增")
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}", comment="普通变量映射，ValueLiteral 契约，不接收秘密")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_environments_ws_project_id"),
        project_fk("fk_environments_project"),
        workspace_object_fk(["pool_id"], "runner_pools", "fk_environments_pool", ondelete="SET NULL"),
        {"comment": "项目可配置执行环境，绑定服务地址与执行池"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="环境显示名称")
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="test", server_default="test", comment="环境类型：test/production"
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="被测服务基础地址，仅允许受控目标")
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="绑定执行池，空由服务端默认解析"
    )
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}", comment="环境级普通变量，不接收秘密")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="乐观锁修订号")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/archived"
    )


class RunnerPool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runner_pools"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_runner_pools_ws_id"),
        {"comment": "执行池，决定网络与并发预算"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.workspaces.id", ondelete="CASCADE"), nullable=False, comment="所属工作空间"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="池显示名称")
    network_zone: Mapped[str] = mapped_column(
        String(50), nullable=False, default="internal", server_default="internal", comment="网络区域标识"
    )
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10", comment="并发预算，单位：个活动槽位")
    allowed_targets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="允许的目标 origin 白名单（scheme/host/port）")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/disabled"
    )


class RunnerPoolProjectGrant(Base):
    __tablename__ = "runner_pool_project_grants"
    __table_args__ = (
        UniqueConstraint("workspace_id", "pool_id", "project_id", name="uq_runner_pool_project_grants"),
        workspace_object_fk(["pool_id"], "runner_pools", "fk_runner_pool_project_grants_pool"),
        project_fk("fk_runner_pool_project_grants_project"),
        {"comment": "执行池项目显式授权"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属工作空间")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="被授予的项目")
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="被授予的池"
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="授权者用户 id")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/revoked"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="授予时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), comment="更新时间")
