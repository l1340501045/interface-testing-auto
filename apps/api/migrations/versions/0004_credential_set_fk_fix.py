"""修正当前凭证集合外键的删除动作，使凭证集合与身份都可正常删除。

Revision ID: 0004_credential_set_fk_fix
Revises: 0003_assertion_result_unique
Create Date: 2026-09-10

背景：`credential_profiles.current_set_id` 使用项目内复合外键，引用列为
`(workspace_id, project_id, current_set_id)`，而其中 `workspace_id`／`project_id`
是 NOT NULL。原 `ON DELETE SET NULL` 在删除 `credential_sets` 行时会把这三列一起
置空，直接触发非空约束失败——表现为“任何凭证集合都删不掉”，并把清理流程卡死。

改为 `NO ACTION`：删除仍被身份引用的当前集合会被明确拒绝（这正是需要的语义），
删除身份本身时由 `credential_sets.profile_id` 的 CASCADE 正常清理其集合。

迁移只调整约束动作，不改列、不改数据，可安全回退到 0003。
"""
from __future__ import annotations

from alembic import op

revision = "0004_credential_set_fk_fix"
down_revision = "0003_assertion_result_unique"
branch_labels = None
depends_on = None

_CONSTRAINT = "fk_credential_profiles_current_set"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "credential_profiles", schema="app", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "credential_profiles",
        "credential_sets",
        ["workspace_id", "project_id", "current_set_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app",
        referent_schema="app",
        ondelete="NO ACTION",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "credential_profiles", schema="app", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "credential_profiles",
        "credential_sets",
        ["workspace_id", "project_id", "current_set_id"],
        ["workspace_id", "project_id", "id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
