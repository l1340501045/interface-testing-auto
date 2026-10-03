"""用例资产操作、归档成员、冻结选择与个人偏好模型。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import NULLABLE_JSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin
from .projects import project_fk, project_object_fk


class AssetOperation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "asset_operations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_asset_operations_ws_project_id"),
        UniqueConstraint(
            "workspace_id", "project_id", "principal_id", "operation_key",
            name="uq_asset_operations_principal_key",
        ),
        project_fk("fk_asset_operations_project"),
        {"comment": "资产写操作的持久幂等回执与最小结果；不保存请求正文或凭证"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="RESTRICT"), nullable=False,
        comment="发起操作的当前平台账号",
    )
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="客户端生成的项目内个人幂等操作键")
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="受限资产动作类型")
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="规范化操作输入摘要，不含原请求")
    result_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="持久回执结构版本"
    )
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="脱敏的操作结果与逐项状态")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="操作结果创建时间"
    )


class AssetArchiveMember(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "asset_archive_members"
    __table_args__ = (
        CheckConstraint(
            "(folder_id IS NOT NULL) <> (case_id IS NOT NULL)",
            name="exactly_one_resource",
        ),
        UniqueConstraint("operation_id", "folder_id", name="uq_asset_archive_members_operation_folder"),
        UniqueConstraint("operation_id", "case_id", name="uq_asset_archive_members_operation_case"),
        project_fk("fk_asset_archive_members_project"),
        project_object_fk(["operation_id"], "asset_operations", "fk_asset_archive_members_operation", ondelete="RESTRICT"),
        project_object_fk(["folder_id"], "folders", "fk_asset_archive_members_folder", ondelete="RESTRICT"),
        project_object_fk(["case_id"], "cases", "fk_asset_archive_members_case", ondelete="RESTRICT"),
        project_object_fk(["original_parent_id"], "folders", "fk_asset_archive_members_original_parent", ondelete="RESTRICT"),
        {"comment": "一次目录或用例归档实际改变的不可变成员集合"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="产生本成员事实的归档资产操作")
    folder_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="本次实际归档的目录；与用例二选一")
    case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="本次实际归档的用例；与目录二选一")
    before_rev: Mapped[int] = mapped_column(Integer, nullable=False, comment="归档前真实修订号")
    archived_rev: Mapped[int] = mapped_column(Integer, nullable=False, comment="归档提交后的真实修订号")
    original_parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="归档时目录父级或用例所属目录；空表示根级或未分组"
    )
    before_state: Mapped[str] = mapped_column(String(16), nullable=False, comment="本次归档前的资源状态")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="归档成员事实创建时间"
    )


class AssetSelection(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "asset_selections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_asset_selections_ws_project_id"),
        project_fk("fk_asset_selections_project"),
        {"comment": "资产批量操作前的短期冻结选择与预览范围"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False,
        comment="创建冻结选择的当前平台账号",
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="冻结选择允许执行的资产动作")
    selector: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="白名单条件、明确资源及规范化摘要")
    members: Mapped[list] = mapped_column(JSONB, nullable=False, comment="一致读取时冻结的资源标识、修订与结构摘要，最多五百项")
    target: Mapped[dict | None] = mapped_column(
        NULLABLE_JSONB,
        nullable=True,
        comment="操作目标参数；SQL空值仅表示动作允许的未分组或根级",
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="允许开始资产操作的截止时间")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="冻结选择创建时间"
    )


class CasePreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_preferences"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "project_id", "principal_id", "case_id",
            name="uq_case_preferences_principal_case",
        ),
        project_fk("fk_case_preferences_project"),
        project_object_fk(["case_id"], "cases", "fk_case_preferences_case", ondelete="CASCADE"),
        {"comment": "当前用户在项目内对用例的收藏与最近打开偏好"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False,
        comment="偏好所属的平台账号",
    )
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="偏好关联的同项目用例")
    favorite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="当前用户是否收藏该用例"
    )
    last_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="当前用户最近一次明确成功打开用例的时间；空表示不在最近列表"
    )


class CaseSavedView(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_saved_views"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_case_saved_views_ws_project_id"),
        project_fk("fk_case_saved_views_project"),
        {"comment": "当前用户保存的用例库筛选视图，不保存结果集合或请求正文"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False,
        comment="保存视图所属的平台账号",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="当前用户命名的筛选视图名称")
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="版本化白名单筛选与排序，不含页位置或结果集")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="保存视图乐观锁修订号")
