"""环境配置不可变版本与当前指针。

Revision ID: 0010_environment_config_versions
Revises: 0009_case_asset_lifecycle
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_environment_config_versions"
down_revision = "0009_case_asset_lifecycle"
branch_labels = None
depends_on = None

_SCOPE_PREDICATE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "environment_config_versions",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False,
            comment="配置快照主键",
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="本配置所属环境"),
        sa.Column(
            "version", sa.Integer(), nullable=False,
            comment="该环境自版本化启用后的递增配置版本",
        ),
        sa.Column(
            "schema_version", sa.SmallInteger(), server_default="1", nullable=False,
            comment="配置快照结构版本；S1固定为1",
        ),
        sa.Column(
            "snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            comment="普通环境配置快照；不含解密身份值",
        ),
        sa.Column(
            "created_by", sa.UUID(), nullable=True,
            comment="保存主体；迁移初始化为空",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False, comment="创建时间",
        ),
        sa.CheckConstraint("version > 0", name="ck_environment_config_versions_version_positive"),
        sa.CheckConstraint(
            "schema_version > 0", name="ck_environment_config_versions_schema_positive"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"],
            name="fk_environment_config_versions_project", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id", "environment_id"],
            ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"],
            name="fk_environment_config_versions_environment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app.users.id"],
            name="fk_environment_config_versions_created_by", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_config_versions")),
        sa.UniqueConstraint(
            "workspace_id", "project_id", "environment_id", "version",
            name="uq_environment_config_versions_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "project_id", "environment_id", "id",
            name="uq_environment_config_versions_scope_id",
        ),
        schema="app",
        comment="环境普通配置的不可变版本快照；不保存身份秘密",
    )
    op.add_column(
        "environments",
        sa.Column(
            "current_config_version_id", sa.UUID(), nullable=True,
            comment="当前环境普通配置快照；迁移完成后非空",
        ),
        schema="app",
    )
    op.execute(
        """
        WITH initialized AS (
          INSERT INTO app.environment_config_versions (
            workspace_id, project_id, environment_id, version, schema_version, snapshot, created_by
          )
          SELECT workspace_id, project_id, id, 1, 1,
                 jsonb_build_object('schema_version', 1, 'variables', variables), NULL
          FROM app.environments
          RETURNING environment_id, id
        )
        UPDATE app.environments AS environment
        SET current_config_version_id = initialized.id
        FROM initialized
        WHERE environment.id = initialized.environment_id
        """
    )
    op.alter_column(
        "environments", "current_config_version_id", nullable=False, schema="app"
    )
    op.create_foreign_key(
        "fk_environments_current_config_version",
        "environments", "environment_config_versions",
        ["workspace_id", "project_id", "id", "current_config_version_id"],
        ["workspace_id", "project_id", "environment_id", "id"],
        source_schema="app", referent_schema="app",
        deferrable=True, initially="DEFERRED",
    )
    op.execute("ALTER TABLE app.environment_config_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.environment_config_versions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY environment_config_versions_tenant "
        "ON app.environment_config_versions "
        f"USING ({_SCOPE_PREDICATE}) WITH CHECK ({_SCOPE_PREDICATE})"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN "
        "GRANT SELECT, INSERT ON app.environment_config_versions TO app_runtime; "
        "END IF; END $$;"
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_environments_current_config_version", "environments",
        schema="app", type_="foreignkey",
    )
    op.drop_column("environments", "current_config_version_id", schema="app")
    op.drop_table("environment_config_versions", schema="app")
