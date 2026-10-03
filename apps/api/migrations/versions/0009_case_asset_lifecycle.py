"""资产生命周期动作约束与运行来源。

Revision ID: 0009_case_asset_lifecycle
Revises: 0008_case_asset_discovery
Create Date: 2026-10-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_case_asset_lifecycle"
down_revision = "0008_case_asset_discovery"
branch_labels = None
depends_on = None

_ACTIONS = "'case_copy','move','archive','restore','folder_archive','folder_restore'"


def upgrade() -> None:
    op.create_check_constraint(
        "ck_asset_operations_action", "asset_operations", f"action IN ({_ACTIONS})", schema="app"
    )
    op.create_check_constraint(
        "ck_asset_selections_action", "asset_selections", f"action IN ({_ACTIONS})", schema="app"
    )
    op.add_column(
        "runs",
        sa.Column(
            "debug_source_case_id", sa.UUID(), nullable=True,
            comment="已保存用例临时调试的来源；独立调试、固定版本及历史运行为空",
        ),
        schema="app",
    )
    op.create_foreign_key(
        "fk_runs_debug_source_case", "runs", "cases",
        ["workspace_id", "project_id", "debug_source_case_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app", referent_schema="app", ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_runs_debug_source_target", "runs",
        "debug_source_case_id IS NULL OR target_type = 'debug_snapshot'", schema="app",
    )
    op.create_index(
        "ix_runs_debug_source_created", "runs",
        ["workspace_id", "project_id", "debug_source_case_id", "created_at"], schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_runs_debug_source_created", table_name="runs", schema="app")
    op.drop_constraint("ck_runs_debug_source_target", "runs", schema="app", type_="check")
    op.drop_constraint("fk_runs_debug_source_case", "runs", schema="app", type_="foreignkey")
    op.drop_column("runs", "debug_source_case_id", schema="app")
    op.drop_constraint("ck_asset_selections_action", "asset_selections", schema="app", type_="check")
    op.drop_constraint("ck_asset_operations_action", "asset_operations", schema="app", type_="check")
