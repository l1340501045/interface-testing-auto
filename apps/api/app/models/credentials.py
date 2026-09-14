"""秘密与凭证模型：秘密、版本、凭证配置、集合及用途授权。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from .projects import project_fk, project_object_fk


class Secret(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "name", name="uq_secrets_ws_project_name"),
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_secrets_ws_project_id"),
        project_fk("fk_secrets_project"),
        {"comment": "项目级加密秘密的引用，值只存 secret_versions.encrypted_value"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="秘密名称")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="static", server_default="static", comment="秘密类型：static")


class SecretVersion(Base):
    __tablename__ = "secret_versions"
    __table_args__ = (
        UniqueConstraint("secret_id", "version", name="uq_secret_versions_secret_version"),
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_secret_versions_ws_project_id"),
        project_fk("fk_secret_versions_project"),
        project_object_fk(["secret_id"], "secrets", "fk_secret_versions_secret", ondelete="CASCADE"),
        {"comment": "秘密的加密值版本，密钥轮换产生新版本"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    secret_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属秘密")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, comment="加密值（Fernet），敏感值")
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="主密钥版本")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class CredentialProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "credential_profiles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_credential_profiles_ws_project_id"),
        project_fk("fk_credential_profiles_project"),
        project_object_fk(["environment_id"], "environments", "fk_credential_profiles_environment", ondelete="CASCADE"),
        # 复合外键把 workspace_id/project_id 一起纳入引用列，而这两列是 NOT NULL。
        # 因此这里不能用 ON DELETE SET NULL：删除凭证集合时会连带把租户列置空，
        # 触发非空约束失败，等于“集合永远删不掉”。改为 NO ACTION——删除仍被
        # 身份指向的当前集合会被明确拒绝，删除身份本身则由 CASCADE 正常清理。
        project_object_fk(["current_set_id"], "credential_sets", "fk_credential_profiles_current_set", ondelete="NO ACTION", use_alter=True),
        {"comment": "环境级被测系统身份的配置入口，保存认证位置与失效判据"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属环境")
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="身份配置名称")
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual_credential", server_default="manual_credential", comment="提供器：manual_credential"
    )
    allowed_targets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="允许的目标 origin 白名单（scheme/host/port）")
    allowed_auth_slots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="允许注入的认证槽位（header/query）")
    current_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="当前凭证 epoch，凭证换新时 +1")
    current_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="当前凭证集合")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unconfigured", server_default="unconfigured",
        comment="状态：unconfigured/available/unavailable",
    )
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="乐观锁修订号")


class CredentialProfileVersion(Base):
    __tablename__ = "credential_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_credential_profile_versions_version"),
        project_fk("fk_credential_profile_versions_project"),
        project_object_fk(["profile_id"], "credential_profiles", "fk_credential_profile_versions_profile", ondelete="CASCADE"),
        {"comment": "身份配置的不可变版本（认证位置/失效判据）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属配置")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="认证位置/失效判据等不可变配置快照")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class CredentialSet(Base):
    __tablename__ = "credential_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_credential_sets_ws_project_id"),
        project_fk("fk_credential_sets_project"),
        project_object_fk(["profile_id"], "credential_profiles", "fk_credential_sets_profile", ondelete="CASCADE"),
        {"comment": "一次完整凭证集合，引用秘密版本"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属配置")
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, comment="对应 epoch")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/unavailable"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="已知到期时间")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class CredentialSetSecretVersion(Base):
    __tablename__ = "credential_set_secret_versions"
    __table_args__ = (
        UniqueConstraint("credential_set_id", "secret_version_id", name="uq_credential_set_secret_versions_ref"),
        UniqueConstraint("credential_set_id", "auth_slot", name="uq_credential_set_secret_versions_slot"),
        project_fk("fk_credential_set_secret_versions_project"),
        project_object_fk(["credential_set_id"], "credential_sets", "fk_credential_set_secret_versions_set", ondelete="CASCADE"),
        project_object_fk(["secret_version_id"], "secret_versions", "fk_credential_set_secret_versions_secret_version", ondelete="RESTRICT"),
        {"comment": "凭证集合到秘密版本的规范关联，绑定认证槽位"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    credential_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属凭证集合")
    secret_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="引用的秘密版本")
    auth_slot: Mapped[str] = mapped_column(String(100), nullable=False, comment="绑定认证槽位，如 header.Authorization/query.token")


class CredentialUseGrant(Base):
    __tablename__ = "credential_use_grants"
    __table_args__ = (
        project_fk("fk_credential_use_grants_project"),
        project_object_fk(["profile_id"], "credential_profiles", "fk_credential_use_grants_profile", ondelete="CASCADE"),
        project_object_fk(["environment_id"], "environments", "fk_credential_use_grants_environment", ondelete="CASCADE"),
        project_object_fk(["case_version_id"], "case_versions", "fk_credential_use_grants_case_version", ondelete="CASCADE"),
        CheckConstraint(
            "(grant_type = 'case_version' AND case_version_id IS NOT NULL AND debug_snapshot_hash IS NULL)"
            " OR (grant_type = 'debug_snapshot' AND debug_snapshot_hash IS NOT NULL AND case_version_id IS NULL)",
            name="target",
        ),
        {"comment": "凭证用途授权，约束可用的用例版本、来源、主体与输入范围"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属配置")
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="允许的环境")
    case_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="允许的已发布用例版本；仅 grant_type=case_version 时非空"
    )
    debug_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="临时调试快照摘要；仅 grant_type=debug_snapshot 时非空")
    grant_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="授权类型：case_version/debug_snapshot")
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="被允许使用的主体用户 id")
    allowed_targets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="允许的目标 origin 白名单")
    allowed_auth_slots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]", comment="允许注入的认证槽位")
    allowed_inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}", comment="允许的变量输入范围；当前版本不支持，非空一律拒绝")
    input_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="签发授权时冻结的普通变量输入摘要；为空表示未绑定输入的旧授权，不参与执行",
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="授权有效期；为空表示长期")
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="一次性使用绑定，使用后失效")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", comment="状态：active/revoked"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), comment="更新时间")
