"""初始迁移：建 app schema、业务表（中文 COMMENT）、FORCE RLS 与运行时授权。

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-10

结构与中文 COMMENT 在此固定固化，不在历史迁移执行时读取可变业务模型，
避免未来模型新增字段污染旧迁移。角色与密码初始化不属于本固定迁移，
由 app.roles.init_runtime_role 以驱动安全字面量单独完成。

RLS 仅按工作空间隔离，不保留 is_admin 跨租户放行：平台引导操作由独立的
迁移/引导身份（超级用户）承担，普通工作空间管理员不能借此绕过其他租户。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "app_runtime"

# 带 workspace_id 列的租户隔离表；按工作空间行级过滤，不保留管理员例外。
_WORKSPACE_SCOPED_TABLES = [
    "projects",
    "project_config_versions",
    "environments",
    "runner_pools",
    "runner_pool_project_grants",
    "folders",
    "cases",
    "case_versions",
    "case_assertions",
    "secrets",
    "secret_versions",
    "credential_profiles",
    "credential_profile_versions",
    "credential_sets",
    "credential_set_secret_versions",
    "credential_use_grants",
    "runs",
    "run_step_attempts",
    "assertion_results",
    "jobs",
    "idempotency_records",
    "audit_events",
]

# 当前工作空间由每次事务的 SET LOCAL app.workspace_id 提供；未设置时为 NULL，
# 比较结果为 NULL，任何行都不可见（默认拒绝）。
_SCOPE_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)


def _runtime_role_exists() -> bool:
    return (
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": RUNTIME_ROLE})
        .scalar()
        is not None
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")

    # —— 业务表（固定结构，中文 COMMENT 随表定义固定） ——
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=100), nullable=False, comment="登录名，全局唯一"),
        sa.Column("password_hash", sa.Text(), nullable=False, comment="密码哈希（scrypt），敏感值"),
        sa.Column("display_name", sa.String(length=200), nullable=False, comment="显示名称"),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False, comment="是否平台级管理员"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/disabled"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        schema="app",
        comment="平台账号，与被测系统业务账号分离",
    )
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=200), nullable=False, comment="工作空间显示名称"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/disabled"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        schema="app",
        comment="工作空间（租户），数据隔离边界",
    )
    op.create_table(
        "projects",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="所属工作空间"),
        sa.Column("key", sa.String(length=100), nullable=False, comment="项目键，工作空间内唯一"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="项目显示名称"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/archived"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id"], ["app.workspaces.id"], name=op.f("fk_projects_workspace_id_workspaces"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_projects_ws_id"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_projects_ws_key"),
        schema="app",
        comment="项目，业务资源的直接归属层",
    )
    op.create_table(
        "runner_pools",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="所属工作空间"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="池显示名称"),
        sa.Column("network_zone", sa.String(length=50), server_default="internal", nullable=False, comment="网络区域标识"),
        sa.Column("concurrency", sa.Integer(), server_default="10", nullable=False, comment="并发预算，单位：个活动槽位"),
        sa.Column("allowed_targets", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="允许的目标 origin 白名单（scheme/host/port）"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/disabled"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id"], ["app.workspaces.id"], name=op.f("fk_runner_pools_workspace_id_workspaces"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runner_pools")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_runner_pools_ws_id"),
        schema="app",
        comment="执行池，决定网络与并发预算",
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("user_id", sa.UUID(), nullable=False, comment="会话所属用户"),
        sa.Column("token_hash", sa.String(length=64), nullable=False, comment="会话令牌 SHA-256 摘要；令牌只入 http-only Cookie"),
        sa.Column("csrf_token", sa.String(length=64), nullable=False, comment="CSRF 令牌"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="过期时间"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True, comment="撤销时间，空表示有效"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["user_id"], ["app.users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
        schema="app",
        comment="平台登录会话，可撤销",
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="所属工作空间"),
        sa.Column("user_id", sa.UUID(), nullable=False, comment="成员用户"),
        sa.Column("role", sa.String(length=20), server_default="viewer", nullable=False, comment="角色：admin/editor/viewer"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="加入时间"),
        sa.ForeignKeyConstraint(["user_id"], ["app.users.id"], name=op.f("fk_workspace_memberships_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["app.workspaces.id"], name=op.f("fk_workspace_memberships_workspace_id_workspaces"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_memberships")),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_memberships_ws_user"),
        schema="app",
        comment="工作空间成员，承载最低项目角色",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="操作者用户 id"),
        sa.Column("action", sa.String(length=100), nullable=False, comment="动作标识"),
        sa.Column("object_type", sa.String(length=100), nullable=False, comment="对象类型"),
        sa.Column("object_id", sa.String(length=200), nullable=False, comment="对象标识"),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="脱敏差异"),
        sa.Column("trace_id", sa.String(length=100), nullable=True, comment="请求 trace_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_audit_events_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        schema="app",
        comment="关键操作的脱敏审计记录",
    )
    op.create_index("ix_audit_events_project_created", "audit_events", ["project_id", "created_at"], unique=False, schema="app")
    op.create_table(
        "environments",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="环境显示名称"),
        sa.Column("kind", sa.String(length=20), server_default="test", nullable=False, comment="环境类型：test/production"),
        sa.Column("base_url", sa.String(length=500), nullable=False, comment="被测服务基础地址，仅允许受控目标"),
        sa.Column("pool_id", sa.UUID(), nullable=True, comment="绑定执行池，空由服务端默认解析"),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False, comment="环境级普通变量，不接收秘密"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="乐观锁修订号"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/archived"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "pool_id"], ["app.runner_pools.workspace_id", "app.runner_pools.id"], name="fk_environments_pool", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_environments_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environments")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_environments_ws_project_id"),
        schema="app",
        comment="项目可配置执行环境，绑定服务地址与执行池",
    )
    op.create_table(
        "folders",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("parent_id", sa.UUID(), nullable=True, comment="父目录，空表示根级"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="目录名称"),
        sa.Column("normalized_name", sa.String(length=200), nullable=False, comment="名称标准化，用于同级唯一"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True, comment="归档时间，空表示在用"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "parent_id"], ["app.folders.workspace_id", "app.folders.project_id", "app.folders.id"], name="fk_folders_parent", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_folders_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_folders")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_folders_ws_project_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "parent_id", "normalized_name", name="uq_folders_sibling_name"),
        schema="app",
        comment="用例目录，只负责组织与查找",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="请求主体用户 id"),
        sa.Column("action", sa.String(length=50), nullable=False, comment="动作类型，如 run:create"),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False, comment="幂等键"),
        sa.Column("request_hash", sa.String(length=64), nullable=False, comment="请求内容摘要"),
        sa.Column("result_ref", sa.String(length=200), nullable=True, comment="结果引用"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="过期时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_idempotency_records_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint("workspace_id", "principal_id", "action", "idempotency_key", name="uq_idempotency_records_key"),
        schema="app",
        comment="创建运行等动作的幂等保护记录",
    )
    op.create_table(
        "project_config_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号，项目内递增"),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False, comment="普通变量映射，ValueLiteral 契约，不接收秘密"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_project_config_versions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_config_versions")),
        sa.UniqueConstraint("workspace_id", "project_id", "version", name="uq_project_config_versions_version"),
        schema="app",
        comment="项目普通变量不可变配置修订",
    )
    op.create_table(
        "runner_pool_project_grants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="所属工作空间"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="被授予的项目"),
        sa.Column("pool_id", sa.UUID(), nullable=False, comment="被授予的池"),
        sa.Column("granted_by", sa.UUID(), nullable=False, comment="授权者用户 id"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/revoked"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="授予时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "pool_id"], ["app.runner_pools.workspace_id", "app.runner_pools.id"], name="fk_runner_pool_project_grants_pool", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_runner_pool_project_grants_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runner_pool_project_grants")),
        sa.UniqueConstraint("workspace_id", "pool_id", "project_id", name="uq_runner_pool_project_grants"),
        schema="app",
        comment="执行池项目显式授权",
    )
    op.create_table(
        "secrets",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="秘密名称"),
        sa.Column("kind", sa.String(length=20), server_default="static", nullable=False, comment="秘密类型：static"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_secrets_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secrets")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_secrets_ws_project_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "name", name="uq_secrets_ws_project_name"),
        schema="app",
        comment="项目级加密秘密的引用，值只存 secret_versions.encrypted_value",
    )
    op.create_table(
        "cases",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("folder_id", sa.UUID(), nullable=True, comment="所属目录"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="用例名称"),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="可视化请求定义（方法、服务路径、重复查询参数、请求头、正文）"),
        sa.Column("assertions", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="草稿断言配置数组"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="乐观锁修订号"),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False, comment="状态：draft/archived"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "folder_id"], ["app.folders.workspace_id", "app.folders.project_id", "app.folders.id"], name="fk_cases_folder", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_cases_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_cases_ws_project_id"),
        schema="app",
        comment="接口用例可编辑草稿",
    )
    op.create_table(
        "credential_profiles",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="所属环境"),
        sa.Column("name", sa.String(length=200), nullable=False, comment="身份配置名称"),
        sa.Column("provider", sa.String(length=50), server_default="manual_credential", nullable=False, comment="提供器：manual_credential"),
        sa.Column("allowed_targets", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="允许的目标 origin 白名单（scheme/host/port）"),
        sa.Column("allowed_auth_slots", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="允许注入的认证槽位（header/query）"),
        sa.Column("current_epoch", sa.Integer(), server_default="0", nullable=False, comment="当前凭证 epoch，凭证换新时 +1"),
        sa.Column("current_set_id", sa.UUID(), nullable=True, comment="当前凭证集合"),
        sa.Column("status", sa.String(length=20), server_default="unconfigured", nullable=False, comment="状态：unconfigured/available/unavailable"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="乐观锁修订号"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id"], ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"], name="fk_credential_profiles_environment", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_credential_profiles_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_profiles")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_credential_profiles_ws_project_id"),
        schema="app",
        comment="环境级被测系统身份的配置入口，保存认证位置与失效判据",
    )
    op.create_table(
        "secret_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("secret_id", sa.UUID(), nullable=False, comment="所属秘密"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False, comment="加密值（Fernet），敏感值"),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False, comment="主密钥版本"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "secret_id"], ["app.secrets.workspace_id", "app.secrets.project_id", "app.secrets.id"], name="fk_secret_versions_secret", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_secret_versions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_versions")),
        sa.UniqueConstraint("secret_id", "version", name="uq_secret_versions_secret_version"),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_secret_versions_ws_project_id"),
        schema="app",
        comment="秘密的加密值版本，密钥轮换产生新版本",
    )
    op.create_table(
        "case_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("case_id", sa.UUID(), nullable=False, comment="源用例"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号，用例内递增"),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="请求定义不可变快照"),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False, comment="协议版本"),
        sa.Column("side_effect", sa.String(length=20), server_default="unknown", nullable=False, comment="副作用分类：read/write/unknown；默认 unknown，未知副作用禁止自动重放"),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False, comment="快照内容摘要"),
        sa.Column("created_by", sa.UUID(), nullable=False, comment="发布者用户 id"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="发布时间"),
        sa.CheckConstraint("side_effect IN ('read','write','unknown')", name=op.f("ck_case_versions_side_effect")),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_id"], ["app.cases.workspace_id", "app.cases.project_id", "app.cases.id"], name="fk_case_versions_case", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_case_versions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_versions")),
        sa.UniqueConstraint("workspace_id", "project_id", "case_id", "version", name="uq_case_versions_version"),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_case_versions_ws_project_id"),
        schema="app",
        comment="用例发布不可变快照",
    )
    op.create_table(
        "credential_profile_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("profile_id", sa.UUID(), nullable=False, comment="所属配置"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="认证位置/失效判据等不可变配置快照"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "profile_id"], ["app.credential_profiles.workspace_id", "app.credential_profiles.project_id", "app.credential_profiles.id"], name="fk_credential_profile_versions_profile", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_credential_profile_versions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_profile_versions")),
        sa.UniqueConstraint("profile_id", "version", name="uq_credential_profile_versions_version"),
        schema="app",
        comment="身份配置的不可变版本（认证位置/失效判据）",
    )
    op.create_table(
        "credential_sets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("profile_id", sa.UUID(), nullable=False, comment="所属配置"),
        sa.Column("epoch", sa.Integer(), nullable=False, comment="对应 epoch"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/unavailable"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, comment="已知到期时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "profile_id"], ["app.credential_profiles.workspace_id", "app.credential_profiles.project_id", "app.credential_profiles.id"], name="fk_credential_sets_profile", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_credential_sets_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_sets")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_credential_sets_ws_project_id"),
        schema="app",
        comment="一次完整凭证集合，引用秘密版本",
    )
    # credential_profiles 与 credential_sets 互为引用，形成环；在两张表都建好后
    # 再以 ALTER 添加当前集合外键，避免 CREATE TABLE 期间引用尚不存在的表。
    op.create_foreign_key(
        "fk_credential_profiles_current_set",
        "credential_profiles",
        "credential_sets",
        ["workspace_id", "project_id", "current_set_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_table(
        "case_assertions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("case_version_id", sa.UUID(), nullable=False, comment="所属用例版本"),
        sa.Column("assertion_id", sa.String(length=64), nullable=False, comment="稳定断言标识"),
        sa.Column("target_source", sa.String(length=50), nullable=False, comment="检查来源，如 request.query/response.json"),
        sa.Column("selector", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="字段定位，不得 eval"),
        sa.Column("type", sa.String(length=50), nullable=False, comment="断言类型"),
        sa.Column("operator_version", sa.Integer(), server_default="1", nullable=False, comment="公共方法语义版本"),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="断言参数，数值按十进制文本"),
        sa.Column("compare_as", sa.String(length=20), nullable=True, comment="数值比较声明：number/integer"),
        sa.Column("severity", sa.String(length=20), server_default="error", nullable=False, comment="严重级别：error/warning"),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False, comment="是否启用"),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False, comment="展示与求值顺序"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_version_id"], ["app.case_versions.workspace_id", "app.case_versions.project_id", "app.case_versions.id"], name="fk_case_assertions_case_version", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_case_assertions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_assertions")),
        schema="app",
        comment="已发布用例版本的字段断言条件",
    )
    op.create_table(
        "credential_set_secret_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("credential_set_id", sa.UUID(), nullable=False, comment="所属凭证集合"),
        sa.Column("secret_version_id", sa.UUID(), nullable=False, comment="引用的秘密版本"),
        sa.Column("auth_slot", sa.String(length=100), nullable=False, comment="绑定认证槽位，如 header.Authorization/query.token"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "credential_set_id"], ["app.credential_sets.workspace_id", "app.credential_sets.project_id", "app.credential_sets.id"], name="fk_credential_set_secret_versions_set", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "secret_version_id"], ["app.secret_versions.workspace_id", "app.secret_versions.project_id", "app.secret_versions.id"], name="fk_credential_set_secret_versions_secret_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_credential_set_secret_versions_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_set_secret_versions")),
        sa.UniqueConstraint("credential_set_id", "auth_slot", name="uq_credential_set_secret_versions_slot"),
        sa.UniqueConstraint("credential_set_id", "secret_version_id", name="uq_credential_set_secret_versions_ref"),
        schema="app",
        comment="凭证集合到秘密版本的规范关联，绑定认证槽位",
    )
    op.create_table(
        "credential_use_grants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("profile_id", sa.UUID(), nullable=False, comment="所属配置"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="允许的环境"),
        sa.Column("case_version_id", sa.UUID(), nullable=True, comment="允许的已发布用例版本；仅 grant_type=case_version 时非空"),
        sa.Column("debug_snapshot_hash", sa.String(length=64), nullable=True, comment="临时调试快照摘要；仅 grant_type=debug_snapshot 时非空"),
        sa.Column("grant_type", sa.String(length=20), nullable=False, comment="授权类型：case_version/debug_snapshot"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="被允许使用的主体用户 id"),
        sa.Column("allowed_targets", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="允许的目标 origin 白名单"),
        sa.Column("allowed_auth_slots", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False, comment="允许注入的认证槽位"),
        sa.Column("allowed_inputs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False, comment="允许的变量输入范围"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, comment="授权有效期；为空表示长期"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True, comment="一次性使用绑定，使用后失效"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False, comment="状态：active/revoked"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.CheckConstraint(
            "(grant_type = 'case_version' AND case_version_id IS NOT NULL AND debug_snapshot_hash IS NULL)"
            " OR (grant_type = 'debug_snapshot' AND debug_snapshot_hash IS NOT NULL AND case_version_id IS NULL)",
            name=op.f("ck_credential_use_grants_target"),
        ),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_version_id"], ["app.case_versions.workspace_id", "app.case_versions.project_id", "app.case_versions.id"], name="fk_credential_use_grants_case_version", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id"], ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"], name="fk_credential_use_grants_environment", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "profile_id"], ["app.credential_profiles.workspace_id", "app.credential_profiles.project_id", "app.credential_profiles.id"], name="fk_credential_use_grants_profile", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_credential_use_grants_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credential_use_grants")),
        schema="app",
        comment="凭证用途授权，约束可用的用例版本、来源、主体与输入范围",
    )
    op.create_table(
        "runs",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("target_type", sa.String(length=20), nullable=False, comment="目标类型：case_version/debug_snapshot"),
        sa.Column("case_version_id", sa.UUID(), nullable=True, comment="执行的已发布用例版本；仅 target_type=case_version 时非空"),
        sa.Column("debug_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="临时不可变调试快照；仅 target_type=debug_snapshot 时非空"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="目标环境"),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False, comment="触发方式：manual"),
        sa.Column("state", sa.String(length=20), server_default="created", nullable=False, comment="生命周期：created/queued/running/finished"),
        sa.Column("outcome", sa.String(length=30), nullable=True, comment="终态结果：passed/failed/error/timed_out/canceled/interrupted/completed_unchecked"),
        sa.Column("reason_category", sa.String(length=20), nullable=True, comment="失败分类：assertion/network/authentication/configuration/policy/cleanup/platform/unknown"),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False, comment="脱敏执行快照，不含密钥明文"),
        sa.Column("pool_id", sa.UUID(), nullable=True, comment="解析后的执行池"),
        sa.Column("queue_deadline_at", sa.DateTime(timezone=True), nullable=False, comment="最晚允许开始执行时间，排队超时不发请求"),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True, comment="首次领取并开始执行的数据库时间，只写一次"),
        sa.Column("business_deadline_at", sa.DateTime(timezone=True), nullable=False, comment="业务步骤截止时间"),
        sa.Column("hard_deadline_at", sa.DateTime(timezone=True), nullable=False, comment="整轮硬截止时间"),
        sa.Column("cleanup_started_at", sa.DateTime(timezone=True), nullable=True, comment="首次进入清理的时间，只写一次"),
        sa.Column("cleanup_deadline_at", sa.DateTime(timezone=True), nullable=True, comment="清理截止，为清理开始加预算与硬截止的较早值"),
        sa.Column("cleanup_budget_ms", sa.Integer(), server_default="60000", nullable=False, comment="清理最多耗时，单位毫秒"),
        sa.Column("created_by", sa.UUID(), nullable=False, comment="创建者用户 id"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.CheckConstraint("target_type = 'case_version' OR target_type = 'debug_snapshot'", name=op.f("ck_runs_target_type")),
        sa.CheckConstraint(
            "(target_type = 'case_version' AND case_version_id IS NOT NULL AND debug_snapshot IS NULL)"
            " OR (target_type = 'debug_snapshot' AND debug_snapshot IS NOT NULL AND case_version_id IS NULL)",
            name=op.f("ck_runs_target_consistency"),
        ),
        sa.ForeignKeyConstraint(["workspace_id", "pool_id"], ["app.runner_pools.workspace_id", "app.runner_pools.id"], name="fk_runs_pool", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_version_id"], ["app.case_versions.workspace_id", "app.case_versions.project_id", "app.case_versions.id"], name="fk_runs_case_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id"], ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"], name="fk_runs_environment", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_runs_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_runs_ws_project_id"),
        schema="app",
        comment="接口用例单次执行记录，按工作空间与项目隔离",
    )
    op.create_table(
        "jobs",
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="项目范围"),
        sa.Column("run_id", sa.UUID(), nullable=False, comment="所属运行"),
        sa.Column("pool_id", sa.UUID(), nullable=False, comment="领取所需的执行池"),
        sa.Column("state", sa.String(length=20), server_default="queued", nullable=False, comment="任务状态：queued/leased/done"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="可领取时间"),
        sa.Column("leased_by", sa.String(length=100), nullable=True, comment="领取者 worker 标识"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True, comment="租约到期时间"),
        sa.Column("fencing_token", sa.Integer(), server_default="0", nullable=False, comment="递增 fencing token，每次领取 +1"),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False, comment="领取次数"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.ForeignKeyConstraint(["workspace_id", "pool_id"], ["app.runner_pools.workspace_id", "app.runner_pools.id"], name="fk_jobs_pool", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "run_id"], ["app.runs.workspace_id", "app.runs.project_id", "app.runs.id"], name="fk_jobs_run_scope", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("workspace_id", "project_id", "run_id", name="uq_jobs_ws_project_run"),
        schema="app",
        comment="持久任务队列与租约，按工作空间与项目范围绑定运行",
    )
    op.create_index("ix_jobs_pool_queued", "jobs", ["pool_id", "state", "available_at"], unique=False, schema="app")
    op.create_index("ix_jobs_queued_available", "jobs", ["state", "available_at"], unique=False, schema="app")
    op.create_table(
        "run_step_attempts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("run_id", sa.UUID(), nullable=False, comment="所属运行"),
        sa.Column("step_key", sa.String(length=50), server_default="main", nullable=False, comment="步骤键，单接口固定为 main"),
        sa.Column("attempt_no", sa.Integer(), server_default="1", nullable=False, comment="尝试序号，从 1 递增"),
        sa.Column("state", sa.String(length=20), nullable=False, comment="步骤状态：queued/sending/finished/skipped"),
        sa.Column("outcome", sa.String(length=20), nullable=True, comment="步骤结果：passed/failed/error/interrupted"),
        sa.Column("send_intent_at", sa.DateTime(timezone=True), nullable=True, comment="持久化发送意图的时间"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="开始执行时间"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True, comment="耗时，单位毫秒"),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="脱敏后的实际发送请求"),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="脱敏后的响应摘要"),
        sa.Column("error_code", sa.String(length=50), nullable=True, comment="稳定错误码"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "run_id"], ["app.runs.workspace_id", "app.runs.project_id", "app.runs.id"], name="fk_run_step_attempts_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_run_step_attempts_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_step_attempts")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_run_step_attempts_ws_project_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "run_id", "step_key", "attempt_no", name="uq_run_step_attempts_key_attempt"),
        schema="app",
        comment="运行内步骤执行尝试，记录发送意图与脱敏结果",
    )
    op.create_table(
        "assertion_results",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("run_id", sa.UUID(), nullable=False, comment="所属运行"),
        sa.Column("step_attempt_id", sa.UUID(), nullable=False, comment="所属步骤尝试"),
        sa.Column("assertion_id", sa.String(length=64), nullable=False, comment="断言标识"),
        sa.Column("type", sa.String(length=50), nullable=False, comment="断言类型"),
        sa.Column("operator_version", sa.Integer(), nullable=False, comment="公共方法语义版本"),
        sa.Column("phase", sa.String(length=20), nullable=False, comment="阶段：pre_request/post_response"),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="检查目标摘要"),
        sa.Column("status", sa.String(length=20), nullable=False, comment="结果：passed/failed/error/skipped"),
        sa.Column("expected", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="脱敏期望值"),
        sa.Column("actual", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="脱敏实际值"),
        sa.Column("reason_code", sa.String(length=50), nullable=True, comment="失败或跳过原因码"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True, comment="求值耗时，单位毫秒"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "run_id"], ["app.runs.workspace_id", "app.runs.project_id", "app.runs.id"], name="fk_assertion_results_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "step_attempt_id"], ["app.run_step_attempts.workspace_id", "app.run_step_attempts.project_id", "app.run_step_attempts.id"], name="fk_assertion_results_step_attempt", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_assertion_results_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assertion_results")),
        schema="app",
        comment="单次尝试中每条断言的求值结果",
    )
    op.create_index("ix_assertion_results_step_attempt", "assertion_results", ["step_attempt_id"], unique=False, schema="app")

    # —— RLS：强制行级安全，只按工作空间过滤 ——
    for table in _WORKSPACE_SCOPED_TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON app.{table} "
            f"USING ({_SCOPE_PREDICATE}) WITH CHECK ({_SCOPE_PREDICATE})"
        )

    # 工作空间：仅本人为成员的工作空间可见；不提供平台管理员跨租户放行。
    op.execute("ALTER TABLE app.workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspaces_member ON app.workspaces "
        "USING (EXISTS (SELECT 1 FROM app.workspace_memberships m "
        "WHERE m.workspace_id = workspaces.id "
        "AND m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid))"
    )

    # 成员表：只可见自己的成员记录。
    op.execute("ALTER TABLE app.workspace_memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.workspace_memberships FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_memberships_self ON app.workspace_memberships "
        "USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid) "
        "WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)"
    )

    # 迁移版本表由 Alembic 维护，仍补中文 COMMENT，保证 app schema 无空注释。
    op.execute("COMMENT ON TABLE app.alembic_version IS '迁移版本记录，仅迁移身份可写'")
    op.execute("COMMENT ON COLUMN app.alembic_version.version_num IS '已应用的迁移修订号'")

    # —— 运行时授权（角色由独立引导步骤创建） ——
    if not _runtime_role_exists():
        return

    op.execute(f"GRANT USAGE ON SCHEMA app TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO {RUNTIME_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE}"
    )
    op.execute(f"REVOKE ALL ON app.alembic_version FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
