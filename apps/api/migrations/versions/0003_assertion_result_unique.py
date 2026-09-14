"""断言结果在单次尝试内的唯一性。

一次尝试里，每条断言在每个阶段只会求值一次：入参阶段一次、响应阶段一次。
`app.assertion_results` 原先只建了普通索引，同一 (step_attempt_id, assertion_id,
phase) 可以被重复插入。报告按尝试号取结果行，重复行会让同一字段在页面上出现
两条一模一样的断言记录。

数据库层把这条业务不变量固定下来：唯一约束同时兼作 step_attempt_id 的查询索引，
因此原来那条单列索引成为冗余，一并移除。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_assertion_result_unique"
down_revision: str | None = "0002_job_broker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_assertion_results_attempt_assertion_phase"
_REDUNDANT_INDEX = "ix_assertion_results_step_attempt"


def upgrade() -> None:
    # 唯一约束会连带重建索引：先清理历史重复行，否则加约束时直接失败。
    # 重复行只可能来自同一个 attempt 下同一断言的重复写入，保留最早的一行。
    op.execute(
        """
        DELETE FROM app.assertion_results a
        USING app.assertion_results b
        WHERE a.step_attempt_id = b.step_attempt_id
          AND a.assertion_id = b.assertion_id
          AND a.phase = b.phase
          AND (a.created_at, a.id) > (b.created_at, b.id)
        """
    )
    op.create_unique_constraint(
        _CONSTRAINT,
        "assertion_results",
        ["step_attempt_id", "assertion_id", "phase"],
        schema="app",
    )
    op.drop_index(_REDUNDANT_INDEX, table_name="assertion_results", schema="app")
    op.execute(
        f"COMMENT ON CONSTRAINT {_CONSTRAINT} ON app.assertion_results IS "
        "'同一次尝试中每条断言在每个阶段只产生一条结果；由数据库保证，报告不因重复写入出现两条相同记录'"
    )


def downgrade() -> None:
    op.create_index(_REDUNDANT_INDEX, "assertion_results", ["step_attempt_id"], schema="app")
    op.drop_constraint(_CONSTRAINT, "assertion_results", schema="app", type_="unique")
