"""补全步骤结果列的未校验、取消与空值语义。

Revision ID: 0007_run_step_outcome_comment
Revises: 0006_workspace_members_read
Create Date: 2026-09-29

本迁移只更新数据字典 COMMENT，不改列类型、约束、默认值或任何业务行。
"""
from __future__ import annotations

from alembic import op

revision = "0007_run_step_outcome_comment"
down_revision = "0006_workspace_members_read"
branch_labels = None
depends_on = None

_NEW_COMMENT = (
    "步骤结果：passed通过、failed断言失败、error执行或配置错误、"
    "interrupted结果不明、canceled已取消、completed_unchecked响应未校验；"
    "空表示没有已提交的最终结论"
)
_OLD_COMMENT = "步骤结果：passed/failed/error/interrupted"


def _comment_sql(comment: str) -> str:
    escaped = comment.replace("'", "''")
    return f"COMMENT ON COLUMN app.run_step_attempts.outcome IS '{escaped}'"


def upgrade() -> None:
    op.execute(_comment_sql(_NEW_COMMENT))


def downgrade() -> None:
    op.execute(_comment_sql(_OLD_COMMENT))
