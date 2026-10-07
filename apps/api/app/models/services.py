"""项目服务目录、环境映射及不可变映射版本。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from .projects import project_fk, project_object_fk


class ProjectService(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_services"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_project_services_scope_id"),
        UniqueConstraint("workspace_id", "project_id", "service_key", name="uq_project_services_key"),
        UniqueConstraint("workspace_id", "project_id", "normalized_name", name="uq_project_services_name"),
        Index(
            "uq_project_services_default",
            "workspace_id", "project_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        CheckConstraint("status IN ('active','archived')", name="ck_project_services_status"),
        CheckConstraint("rev > 0", name="ck_project_services_rev_positive"),
        project_fk("fk_project_services_project"),
        {"comment": "项目内稳定服务目录；默认服务受生命周期保护，命名服务可软停用"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    service_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="服务端生成的稳定服务标识；默认服务为default")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="用户可读服务显示名")
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, comment="去首尾普通空格并casefold后的完整去重名，不截断")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", comment="是否系统默认服务；同项目最多一个")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="服务状态：active或archived；默认服务恒为active")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="服务元数据乐观锁修订号")


class EnvironmentServiceMapping(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environment_service_mappings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_environment_service_mappings_scope_id"),
        UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "id", name="uq_environment_service_mappings_target_id"),
        UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", name="uq_environment_service_mappings_environment_service"),
        CheckConstraint("status IN ('active','disabled','archived')", name="ck_environment_service_mappings_status"),
        CheckConstraint("rev > 0", name="ck_environment_service_mappings_rev_positive"),
        project_fk("fk_environment_service_mappings_project"),
        project_object_fk(["environment_id"], "environments", "fk_environment_service_mappings_environment"),
        project_object_fk(["service_id"], "project_services", "fk_environment_service_mappings_service", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "environment_id", "service_id", "id", "current_version_id"],
            [
                "app.environment_service_versions.workspace_id",
                "app.environment_service_versions.project_id",
                "app.environment_service_versions.environment_id",
                "app.environment_service_versions.service_id",
                "app.environment_service_versions.mapping_id",
                "app.environment_service_versions.id",
            ],
            name="fk_environment_service_mappings_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        {"comment": "环境到项目服务的当前地址映射；默认映射状态跟随环境"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="映射所属环境")
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="同项目服务目录引用")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="映射乐观锁修订号")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="映射状态：命名服务active/disabled，默认服务active/archived")
    current_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="当前不可变映射版本")


class EnvironmentServiceVersion(Base):
    __tablename__ = "environment_service_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "mapping_id", "id", name="uq_environment_service_versions_scope_id"),
        UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "id", name="uq_environment_service_versions_run_ref"),
        UniqueConstraint("workspace_id", "project_id", "mapping_id", "version", name="uq_environment_service_versions_version"),
        CheckConstraint("version > 0", name="ck_environment_service_versions_version_positive"),
        CheckConstraint("status IN ('active','disabled','archived')", name="ck_environment_service_versions_status"),
        project_fk("fk_environment_service_versions_project"),
        project_object_fk(["environment_id"], "environments", "fk_environment_service_versions_environment"),
        project_object_fk(["service_id"], "project_services", "fk_environment_service_versions_service", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "environment_id", "service_id", "mapping_id"],
            [
                "app.environment_service_mappings.workspace_id",
                "app.environment_service_mappings.project_id",
                "app.environment_service_mappings.environment_id",
                "app.environment_service_mappings.service_id",
                "app.environment_service_mappings.id",
            ],
            name="fk_environment_service_versions_mapping",
            ondelete="CASCADE",
        ),
        {"comment": "环境服务映射的不可变地址与状态版本"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="映射版本主键")
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属环境")
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目服务")
    mapping_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属环境服务映射")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="映射内递增版本号")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, comment="该版本基础地址原文；新保存最多500字符")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="保存时映射状态；不替代当前安全检查")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="RESTRICT"), nullable=True, comment="保存主体；迁移初始化为空")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")
