"""目录与用例模型：目录、用例草稿、发布版本、断言配置。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from .projects import project_fk, project_object_fk


class Folder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_folders_ws_project_id"),
        UniqueConstraint(
            "workspace_id", "project_id", "parent_id", "normalized_name",
            name="uq_folders_sibling_name",
        ),
        project_fk("fk_folders_project"),
        project_object_fk(["parent_id"], "folders", "fk_folders_parent", ondelete="CASCADE"),
        project_object_fk(
            ["archive_operation_id"],
            "asset_operations",
            "fk_folders_archive_operation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        {"comment": "用例目录，只负责组织与查找"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="父目录，空表示根级")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="目录名称")
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="名称标准化，用于同级唯一")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="归档时间，空表示在用")
    rev: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="目录乐观锁修订号"
    )
    archive_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="使目录进入当前归档态的资产操作；空表示活动态或旧归档记录"
    )


class Case(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_cases_ws_project_id"),
        project_fk("fk_cases_project"),
        project_object_fk(["folder_id"], "folders", "fk_cases_folder", ondelete="SET NULL"),
        project_object_fk(
            ["archive_operation_id"],
            "asset_operations",
            "fk_cases_archive_operation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        {"comment": "接口用例可编辑草稿"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    folder_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="所属目录")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="用例名称")
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="可视化请求定义（方法、服务路径、重复查询参数、请求头、正文）")
    assertions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="草稿断言配置数组")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="乐观锁修订号")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft", comment="状态：draft/archived"
    )
    archive_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="使本用例进入当前归档态的资产操作；空表示活动态或旧归档记录"
    )


class CaseVersion(Base):
    __tablename__ = "case_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_case_versions_ws_project_id"),
        UniqueConstraint("workspace_id", "project_id", "case_id", "version", name="uq_case_versions_version"),
        project_fk("fk_case_versions_project"),
        project_object_fk(["case_id"], "cases", "fk_case_versions_case", ondelete="CASCADE"),
        CheckConstraint(
            "side_effect IN ('read','write','unknown')", name="side_effect"
        ),
        {"comment": "用例发布不可变快照"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="源用例")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号，用例内递增")
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="请求定义不可变快照")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="协议版本")
    side_effect: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown",
        comment="副作用分类：read/write/unknown；默认 unknown，未知副作用禁止自动重放",
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="快照内容摘要")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="发布者用户 id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="发布时间")


class CaseAssertion(Base):
    __tablename__ = "case_assertions"
    __table_args__ = (
        project_fk("fk_case_assertions_project"),
        project_object_fk(["case_version_id"], "case_versions", "fk_case_assertions_case_version", ondelete="CASCADE"),
        {"comment": "已发布用例版本的字段断言条件"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    case_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属用例版本")
    assertion_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="稳定断言标识")
    target_source: Mapped[str] = mapped_column(String(50), nullable=False, comment="检查来源，如 request.query/response.json")
    selector: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="字段定位，不得 eval")
    type: Mapped[str] = mapped_column(String(50), nullable=False, comment="断言类型")
    operator_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="公共方法语义版本")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="断言参数，数值按十进制文本")
    compare_as: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="数值比较声明：number/integer")
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default="error", server_default="error", comment="严重级别：error/warning"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="是否启用"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="展示与求值顺序")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")
