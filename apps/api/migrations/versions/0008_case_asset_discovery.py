"""用例资产查询、归档批次基础与个人偏好结构。

Revision ID: 0008_case_asset_discovery
Revises: 0007_run_step_outcome_comment
Create Date: 2026-10-04

迁移只建立 S1 读取和后续生命周期共用的兼容结构，不回填旧归档批次，
也不开放复制、归档或恢复写动作。所有 DDL 前先检查不能安全约束的旧目录关系。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_case_asset_discovery"
down_revision = "0007_run_step_outcome_comment"
branch_labels = None
depends_on = None

RUNTIME_ROLE = "app_runtime"
_TENANT_TABLES = (
    "asset_operations",
    "asset_archive_members",
    "asset_selections",
    "case_preferences",
    "case_saved_views",
)
_SCOPE_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)


def upgrade() -> None:
    # 新的根级 NULLS NOT DISTINCT 唯一索引会覆盖根和非根目录。旧数据若有冲突、
    # 坏父链或循环，必须在任何结构变化前中止，不能自动改名、移动或删除。
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM app.folders
            GROUP BY workspace_id, project_id, parent_id, normalized_name
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION '目录迁移预检查失败：存在同项目同级重名目录';
          END IF;
          IF EXISTS (
            SELECT 1 FROM app.folders child
            LEFT JOIN app.folders parent
              ON parent.id = child.parent_id
             AND parent.workspace_id = child.workspace_id
             AND parent.project_id = child.project_id
            WHERE child.parent_id IS NOT NULL AND parent.id IS NULL
          ) THEN
            RAISE EXCEPTION '目录迁移预检查失败：存在失效或跨范围父目录关系';
          END IF;
          IF EXISTS (
            WITH RECURSIVE walk AS (
              SELECT id AS start_id, parent_id, ARRAY[id] AS visited, false AS cycle
              FROM app.folders
              UNION ALL
              SELECT walk.start_id, parent.parent_id,
                     walk.visited || parent.id,
                     parent.id = ANY(walk.visited)
              FROM walk
              JOIN app.folders parent ON parent.id = walk.parent_id
              WHERE NOT walk.cycle
            )
            SELECT 1 FROM walk WHERE cycle
          ) THEN
            RAISE EXCEPTION '目录迁移预检查失败：存在目录循环';
          END IF;
        END $$;
        """
    )

    op.create_table(
        "asset_operations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="发起操作的当前平台账号"),
        sa.Column("operation_key", sa.String(length=128), nullable=False, comment="客户端生成的项目内个人幂等操作键"),
        sa.Column("action", sa.String(length=32), nullable=False, comment="受限资产动作类型"),
        sa.Column("request_hash", sa.String(length=64), nullable=False, comment="规范化操作输入摘要，不含原请求"),
        sa.Column("result_schema_version", sa.Integer(), server_default="1", nullable=False, comment="持久回执结构版本"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="脱敏的操作结果与逐项状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="操作结果创建时间"),
        sa.ForeignKeyConstraint(["principal_id"], ["app.users.id"], name="fk_asset_operations_principal", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_asset_operations_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_operations")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_asset_operations_ws_project_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "principal_id", "operation_key", name="uq_asset_operations_principal_key"),
        schema="app",
        comment="资产写操作的持久幂等回执与最小结果；不保存请求正文或凭证",
    )

    op.add_column("folders", sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="目录乐观锁修订号"), schema="app")
    op.add_column(
        "folders",
        sa.Column("archive_operation_id", sa.UUID(), nullable=True, comment="使目录进入当前归档态的资产操作；空表示活动态或旧归档记录"),
        schema="app",
    )
    op.add_column(
        "cases",
        sa.Column("archive_operation_id", sa.UUID(), nullable=True, comment="使本用例进入当前归档态的资产操作；空表示活动态或旧归档记录"),
        schema="app",
    )
    op.create_foreign_key(
        "fk_folders_archive_operation", "folders", "asset_operations",
        ["workspace_id", "project_id", "archive_operation_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app", referent_schema="app", ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_cases_archive_operation", "cases", "asset_operations",
        ["workspace_id", "project_id", "archive_operation_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app", referent_schema="app", ondelete="RESTRICT",
    )
    op.create_index(
        "uq_folders_scope_parent_normalized_name",
        "folders",
        ["workspace_id", "project_id", "parent_id", "normalized_name"],
        unique=True,
        schema="app",
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "asset_archive_members",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("operation_id", sa.UUID(), nullable=False, comment="产生本成员事实的归档资产操作"),
        sa.Column("folder_id", sa.UUID(), nullable=True, comment="本次实际归档的目录；与用例二选一"),
        sa.Column("case_id", sa.UUID(), nullable=True, comment="本次实际归档的用例；与目录二选一"),
        sa.Column("before_rev", sa.Integer(), nullable=False, comment="归档前真实修订号"),
        sa.Column("archived_rev", sa.Integer(), nullable=False, comment="归档提交后的真实修订号"),
        sa.Column("original_parent_id", sa.UUID(), nullable=True, comment="归档时目录父级或用例所属目录；空表示根级或未分组"),
        sa.Column("before_state", sa.String(length=16), nullable=False, comment="本次归档前的资源状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="归档成员事实创建时间"),
        sa.CheckConstraint("(folder_id IS NOT NULL) <> (case_id IS NOT NULL)", name=op.f("ck_asset_archive_members_exactly_one_resource")),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_asset_archive_members_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "operation_id"], ["app.asset_operations.workspace_id", "app.asset_operations.project_id", "app.asset_operations.id"], name="fk_asset_archive_members_operation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "folder_id"], ["app.folders.workspace_id", "app.folders.project_id", "app.folders.id"], name="fk_asset_archive_members_folder", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_id"], ["app.cases.workspace_id", "app.cases.project_id", "app.cases.id"], name="fk_asset_archive_members_case", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "original_parent_id"], ["app.folders.workspace_id", "app.folders.project_id", "app.folders.id"], name="fk_asset_archive_members_original_parent", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_archive_members")),
        sa.UniqueConstraint("operation_id", "folder_id", name="uq_asset_archive_members_operation_folder"),
        sa.UniqueConstraint("operation_id", "case_id", name="uq_asset_archive_members_operation_case"),
        schema="app",
        comment="一次目录或用例归档实际改变的不可变成员集合",
    )

    op.create_table(
        "asset_selections",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="创建冻结选择的当前平台账号"),
        sa.Column("action", sa.String(length=32), nullable=False, comment="冻结选择允许执行的资产动作"),
        sa.Column("selector", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="白名单条件、明确资源及规范化摘要"),
        sa.Column("members", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="一致读取时冻结的资源标识、修订与结构摘要，最多五百项"),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="操作目标参数；空仅表示动作允许的未分组或根级"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="允许开始资产操作的截止时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="冻结选择创建时间"),
        sa.ForeignKeyConstraint(["principal_id"], ["app.users.id"], name="fk_asset_selections_principal", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_asset_selections_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_selections")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_asset_selections_ws_project_id"),
        schema="app",
        comment="资产批量操作前的短期冻结选择与预览范围",
    )
    op.create_index("ix_asset_selections_scope_owner_expiry", "asset_selections", ["workspace_id", "project_id", "principal_id", "expires_at"], schema="app")

    op.create_table(
        "case_preferences",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="偏好所属的平台账号"),
        sa.Column("case_id", sa.UUID(), nullable=False, comment="偏好关联的同项目用例"),
        sa.Column("favorite", sa.Boolean(), server_default="false", nullable=False, comment="当前用户是否收藏该用例"),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True, comment="当前用户最近一次明确成功打开用例的时间；空表示不在最近列表"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["principal_id"], ["app.users.id"], name="fk_case_preferences_principal", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_case_preferences_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "case_id"], ["app.cases.workspace_id", "app.cases.project_id", "app.cases.id"], name="fk_case_preferences_case", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_preferences")),
        sa.UniqueConstraint("workspace_id", "project_id", "principal_id", "case_id", name="uq_case_preferences_principal_case"),
        schema="app",
        comment="当前用户在项目内对用例的收藏与最近打开偏好",
    )
    op.create_index("ix_case_preferences_owner_recent", "case_preferences", ["workspace_id", "project_id", "principal_id", "last_opened_at", "case_id"], schema="app")
    op.create_index("ix_case_preferences_owner_favorite", "case_preferences", ["workspace_id", "project_id", "principal_id", "favorite", "case_id"], schema="app")

    op.create_table(
        "case_saved_views",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="主键，UUID"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("principal_id", sa.UUID(), nullable=False, comment="保存视图所属的平台账号"),
        sa.Column("name", sa.String(length=100), nullable=False, comment="当前用户命名的筛选视图名称"),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="版本化白名单筛选与排序，不含页位置或结果集"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="保存视图乐观锁修订号"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.ForeignKeyConstraint(["principal_id"], ["app.users.id"], name="fk_case_saved_views_principal", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_case_saved_views_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_saved_views")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_case_saved_views_ws_project_id"),
        schema="app",
        comment="当前用户保存的用例库筛选视图，不保存结果集合或请求正文",
    )
    op.create_index("ix_case_saved_views_owner_updated", "case_saved_views", ["workspace_id", "project_id", "principal_id", "updated_at", "id"], schema="app")

    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON app.{table} "
            f"USING ({_SCOPE_PREDICATE}) WITH CHECK ({_SCOPE_PREDICATE})"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "app.asset_operations, app.asset_archive_members, app.asset_selections, "
        "app.case_preferences, app.case_saved_views TO app_runtime; END IF; END $$;"
    )


def downgrade() -> None:
    op.drop_constraint("fk_cases_archive_operation", "cases", schema="app", type_="foreignkey")
    op.drop_constraint("fk_folders_archive_operation", "folders", schema="app", type_="foreignkey")
    for table in reversed(_TENANT_TABLES):
        op.drop_table(table, schema="app")
    op.drop_column("cases", "archive_operation_id", schema="app")
    op.drop_index("uq_folders_scope_parent_normalized_name", table_name="folders", schema="app")
    op.drop_column("folders", "archive_operation_id", schema="app")
    op.drop_column("folders", "rev", schema="app")
