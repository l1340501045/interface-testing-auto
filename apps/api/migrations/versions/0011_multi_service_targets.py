"""项目服务目录、环境映射版本与运行目标旁路。

Revision ID: 0011_multi_service_targets
Revises: 0010_environment_config_versions
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_multi_service_targets"
down_revision = "0010_environment_config_versions"
branch_labels = None
depends_on = None

_SCOPE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
_TABLES = ("project_services", "environment_service_mappings", "environment_service_versions")


def upgrade() -> None:
    op.execute("COMMENT ON COLUMN app.environment_config_versions.schema_version IS '配置快照结构版本；S1为1，多服务目标为2'")
    op.create_table(
        "project_services",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="项目服务主键"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("service_key", sa.String(64), nullable=False, comment="服务端生成的稳定服务标识；默认服务为default"),
        sa.Column("name", sa.String(200), nullable=False, comment="用户可读服务显示名"),
        sa.Column("normalized_name", sa.Text(), nullable=False, comment="去首尾普通空格并casefold后的完整去重名，不截断"),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False, comment="是否系统默认服务；同项目最多一个"),
        sa.Column("status", sa.String(16), server_default="active", nullable=False, comment="服务状态：active或archived；默认服务恒为active"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="服务元数据乐观锁修订号"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_project_services_status"),
        sa.CheckConstraint("rev > 0", name="ck_project_services_rev_positive"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_project_services_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_services")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_project_services_scope_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "service_key", name="uq_project_services_key"),
        sa.UniqueConstraint("workspace_id", "project_id", "normalized_name", name="uq_project_services_name"),
        schema="app",
        comment="项目内稳定服务目录；默认服务受生命周期保护，命名服务可软停用",
    )
    op.create_index("uq_project_services_default", "project_services", ["workspace_id", "project_id"], unique=True, schema="app", postgresql_where=sa.text("is_default"))

    op.create_table(
        "environment_service_mappings",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="环境服务映射主键"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="映射所属环境"),
        sa.Column("service_id", sa.UUID(), nullable=False, comment="同项目服务目录引用"),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False, comment="映射乐观锁修订号"),
        sa.Column("status", sa.String(16), server_default="active", nullable=False, comment="映射状态：命名服务active/disabled，默认服务active/archived"),
        sa.Column("current_version_id", sa.UUID(), nullable=True, comment="当前不可变映射版本；初始化完成后非空"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.CheckConstraint("status IN ('active','disabled','archived')", name="ck_environment_service_mappings_status"),
        sa.CheckConstraint("rev > 0", name="ck_environment_service_mappings_rev_positive"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_environment_service_mappings_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id"], ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"], name="fk_environment_service_mappings_environment", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["app.project_services.workspace_id", "app.project_services.project_id", "app.project_services.id"], name="fk_environment_service_mappings_service", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_service_mappings")),
        sa.UniqueConstraint("workspace_id", "project_id", "id", name="uq_environment_service_mappings_scope_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "id", name="uq_environment_service_mappings_target_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", name="uq_environment_service_mappings_environment_service"),
        schema="app",
        comment="环境到项目服务的当前地址映射；默认映射状态跟随环境",
    )

    op.create_table(
        "environment_service_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False, comment="映射版本主键"),
        sa.Column("workspace_id", sa.UUID(), nullable=False, comment="工作空间范围"),
        sa.Column("project_id", sa.UUID(), nullable=False, comment="所属项目"),
        sa.Column("environment_id", sa.UUID(), nullable=False, comment="所属环境"),
        sa.Column("service_id", sa.UUID(), nullable=False, comment="所属项目服务"),
        sa.Column("mapping_id", sa.UUID(), nullable=False, comment="所属环境服务映射"),
        sa.Column("version", sa.Integer(), nullable=False, comment="映射内递增版本号"),
        sa.Column("base_url", sa.Text(), nullable=False, comment="该版本基础地址原文；新保存最多500字符"),
        sa.Column("status", sa.String(16), nullable=False, comment="保存时映射状态；不替代当前安全检查"),
        sa.Column("created_by", sa.UUID(), nullable=True, comment="保存主体；迁移初始化为空"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.CheckConstraint("version > 0", name="ck_environment_service_versions_version_positive"),
        sa.CheckConstraint("status IN ('active','disabled','archived')", name="ck_environment_service_versions_status"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["app.projects.workspace_id", "app.projects.id"], name="fk_environment_service_versions_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id"], ["app.environments.workspace_id", "app.environments.project_id", "app.environments.id"], name="fk_environment_service_versions_environment", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["app.project_services.workspace_id", "app.project_services.project_id", "app.project_services.id"], name="fk_environment_service_versions_service", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id", "environment_id", "service_id", "mapping_id"], ["app.environment_service_mappings.workspace_id", "app.environment_service_mappings.project_id", "app.environment_service_mappings.environment_id", "app.environment_service_mappings.service_id", "app.environment_service_mappings.id"], name="fk_environment_service_versions_mapping", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["app.users.id"], name="fk_environment_service_versions_created_by", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_service_versions")),
        sa.UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "mapping_id", "id", name="uq_environment_service_versions_scope_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "environment_id", "service_id", "id", name="uq_environment_service_versions_run_ref"),
        sa.UniqueConstraint("workspace_id", "project_id", "mapping_id", "version", name="uq_environment_service_versions_version"),
        schema="app",
        comment="环境服务映射的不可变地址与状态版本",
    )
    op.create_foreign_key(
        "fk_environment_service_mappings_current_version",
        "environment_service_mappings", "environment_service_versions",
        ["workspace_id", "project_id", "environment_id", "service_id", "id", "current_version_id"],
        ["workspace_id", "project_id", "environment_id", "service_id", "mapping_id", "id"],
        source_schema="app", referent_schema="app", deferrable=True, initially="DEFERRED",
    )

    op.add_column("cases", sa.Column("service_id", sa.UUID(), nullable=True, comment="命名服务引用；默认及历史用例为空"), schema="app")
    op.add_column("case_versions", sa.Column("service_id", sa.UUID(), nullable=True, comment="该版本命名服务引用；默认及历史版本为空"), schema="app")
    op.add_column("runs", sa.Column("service_id", sa.UUID(), nullable=True, comment="命名服务引用；默认及历史运行为空"), schema="app")
    op.add_column("runs", sa.Column("environment_service_version_id", sa.UUID(), nullable=True, comment="命名服务受理时固定的映射版本；默认及历史运行为空"), schema="app")
    op.create_check_constraint(
        "ck_runs_service_version_pair", "runs",
        "(service_id IS NULL) = (environment_service_version_id IS NULL)", schema="app",
    )
    for table, constraint in (("cases", "fk_cases_service"), ("case_versions", "fk_case_versions_service"), ("runs", "fk_runs_service")):
        op.create_foreign_key(constraint, table, "project_services", ["workspace_id", "project_id", "service_id"], ["workspace_id", "project_id", "id"], source_schema="app", referent_schema="app", ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_runs_environment_service_version", "runs", "environment_service_versions",
        ["workspace_id", "project_id", "environment_id", "service_id", "environment_service_version_id"],
        ["workspace_id", "project_id", "environment_id", "service_id", "id"],
        source_schema="app", referent_schema="app", ondelete="RESTRICT",
    )

    op.execute("""
        INSERT INTO app.project_services
          (workspace_id, project_id, service_key, name, normalized_name, is_default, status, rev)
        SELECT workspace_id, id, 'default', '默认服务', '默认服务', true, 'active', 1
        FROM app.projects
    """)
    op.execute("""
        INSERT INTO app.environment_service_mappings
          (workspace_id, project_id, environment_id, service_id, rev, status)
        SELECT e.workspace_id, e.project_id, e.id, s.id, 1,
               CASE WHEN e.status = 'archived' THEN 'archived' ELSE 'active' END
        FROM app.environments e
        JOIN app.project_services s
          ON s.workspace_id=e.workspace_id AND s.project_id=e.project_id AND s.is_default
    """)
    op.execute("""
        INSERT INTO app.environment_service_versions
          (workspace_id, project_id, environment_id, service_id, mapping_id, version, base_url, status, created_by)
        SELECT m.workspace_id, m.project_id, m.environment_id, m.service_id, m.id, 1,
               e.base_url, m.status, NULL
        FROM app.environment_service_mappings m
        JOIN app.environments e ON e.id=m.environment_id
    """)
    op.execute("""
        UPDATE app.environment_service_mappings m
        SET current_version_id=v.id
        FROM app.environment_service_versions v
        WHERE v.mapping_id=m.id AND v.version=1
    """)
    # current复合FK为INITIALLY DEFERRED：有旧环境时，上面的mapping/version seed会留下
    # pending constraint trigger。PostgreSQL禁止在pending trigger未结算时ALTER同一表；
    # 精确切为IMMEDIATE会先验证完整六维引用，失败则整笔迁移原子回滚，再安全收紧NOT NULL。
    op.execute("SET CONSTRAINTS app.fk_environment_service_mappings_current_version IMMEDIATE")
    op.alter_column("environment_service_mappings", "current_version_id", nullable=False, schema="app")

    op.execute("""
        WITH next_versions AS (
          SELECT e.id AS environment_id, e.workspace_id, e.project_id, e.variables,
                 COALESCE(max(cv.version), 0) + 1 AS version,
                 m.id AS mapping_id, m.current_version_id
          FROM app.environments e
          LEFT JOIN app.environment_config_versions cv ON cv.environment_id=e.id
          JOIN app.environment_service_mappings m ON m.environment_id=e.id
          JOIN app.project_services s ON s.id=m.service_id AND s.is_default
          GROUP BY e.id, e.workspace_id, e.project_id, e.variables, m.id, m.current_version_id
        ), inserted AS (
          INSERT INTO app.environment_config_versions
            (workspace_id, project_id, environment_id, version, schema_version, snapshot, created_by)
          SELECT workspace_id, project_id, environment_id, version, 2,
                 jsonb_build_object(
                   'schema_version', 2,
                   'variables', variables,
                   'service_versions', jsonb_build_object(
                     'default', jsonb_build_object(
                       'mapping_id', mapping_id,
                       'mapping_version_id', current_version_id
                     )
                   )
                 ), NULL
          FROM next_versions
          RETURNING environment_id, id
        )
        UPDATE app.environments e SET current_config_version_id=i.id
        FROM inserted i WHERE e.id=i.environment_id
    """)

    for table in _TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant ON app.{table} USING ({_SCOPE}) WITH CHECK ({_SCOPE})")
    op.execute("""
      DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_runtime') THEN
        REVOKE UPDATE, DELETE ON app.environment_service_versions, app.environment_config_versions FROM app_runtime;
        REVOKE DELETE ON app.project_services, app.environment_service_mappings FROM app_runtime;
        GRANT SELECT, INSERT, UPDATE ON app.project_services, app.environment_service_mappings TO app_runtime;
        GRANT SELECT, INSERT ON app.environment_service_versions TO app_runtime;
      END IF; END $$;
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM app.project_services WHERE NOT is_default)
           OR EXISTS (
             SELECT 1 FROM app.project_services
             WHERE is_default AND (
               service_key <> 'default' OR name <> '默认服务'
               OR normalized_name <> '默认服务' OR status <> 'active' OR rev <> 1
             )
           )
           OR EXISTS (SELECT 1 FROM app.environment_service_versions WHERE created_by IS NOT NULL)
           OR EXISTS (
             SELECT 1 FROM app.environment_config_versions
             WHERE schema_version=2 AND created_by IS NOT NULL
           )
           OR EXISTS (SELECT 1 FROM app.cases WHERE service_id IS NOT NULL)
           OR EXISTS (SELECT 1 FROM app.case_versions WHERE service_id IS NOT NULL)
           OR EXISTS (SELECT 1 FROM app.runs WHERE service_id IS NOT NULL OR environment_service_version_id IS NOT NULL)
           OR EXISTS (
             SELECT 1 FROM app.environments e
             WHERE NOT EXISTS (
               SELECT 1 FROM app.environment_config_versions cv
               WHERE cv.environment_id=e.id AND cv.schema_version=1
             )
           )
           OR EXISTS (
             SELECT 1
             FROM app.runs r
             WHERE (r.snapshot #>> '{resolution,config_basis,environment_config_version_id}') IN (
               SELECT id::text FROM app.environment_config_versions WHERE schema_version=2
             )
                OR (r.snapshot #>> '{resolution,target_ref,service_id}') IN (
               SELECT id::text FROM app.project_services
             )
                OR (r.snapshot #>> '{resolution,target_ref,mapping_id}') IN (
               SELECT id::text FROM app.environment_service_mappings
             )
                OR (r.snapshot #>> '{resolution,target_ref,mapping_version_id}') IN (
               SELECT id::text FROM app.environment_service_versions
             )
           )
        THEN RAISE EXCEPTION '检测到S2业务写入，拒绝盲降级；请前滚修复或先保存数据'; END IF;
      END $$;
    """)
    op.execute("""
      WITH previous AS (
        SELECT DISTINCT ON (environment_id) environment_id, id
        FROM app.environment_config_versions
        WHERE schema_version=1
        ORDER BY environment_id, version DESC
      )
      UPDATE app.environments e SET current_config_version_id=p.id
      FROM previous p WHERE e.id=p.environment_id
    """)
    op.execute("DELETE FROM app.environment_config_versions WHERE schema_version=2 AND created_by IS NULL")
    op.drop_constraint("fk_runs_environment_service_version", "runs", schema="app", type_="foreignkey")
    op.drop_constraint("ck_runs_service_version_pair", "runs", schema="app", type_="check")
    for table, constraint in (("runs", "fk_runs_service"), ("case_versions", "fk_case_versions_service"), ("cases", "fk_cases_service")):
        op.drop_constraint(constraint, table, schema="app", type_="foreignkey")
    op.drop_column("runs", "environment_service_version_id", schema="app")
    op.drop_column("runs", "service_id", schema="app")
    op.drop_column("case_versions", "service_id", schema="app")
    op.drop_column("cases", "service_id", schema="app")
    op.drop_constraint("fk_environment_service_mappings_current_version", "environment_service_mappings", schema="app", type_="foreignkey")
    op.drop_table("environment_service_versions", schema="app")
    op.drop_table("environment_service_mappings", schema="app")
    op.drop_table("project_services", schema="app")
    op.execute("COMMENT ON COLUMN app.environment_config_versions.schema_version IS '配置快照结构版本；S1固定为1'")
