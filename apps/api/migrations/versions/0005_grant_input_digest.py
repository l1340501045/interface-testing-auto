"""为凭证用途授权补上输入摘要，把请求变量纳入授权绑定。

Revision ID: 0005_grant_input_digest
Revises: 0004_credential_set_fk_fix
Create Date: 2026-09-14

背景：授权按用例版本签发，而版本模板里的 `{{...}}` 由项目／环境普通变量在运行时
解析。同一份版本模板可以因为变量不同指向完全不同的请求，只按版本 id 匹配授权
等于让一份为 `/{{operation}}` 签发的凭证在变量被改掉后继续注入到另一条路径上。

新增 `input_digest`：签发授权时冻结当时生效的普通变量摘要，执行前重新计算并比对，
不一致即拒绝使用该授权。列可为空，**不回填历史行**——按当前变量回填等于替从未存在
过的绑定伪造证据；空值表示“未绑定输入的旧授权”，执行时按未授权处理，需要重新签发。

只加列，不改数据、不改约束，可安全回退到 0004。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_grant_input_digest"
down_revision = "0004_credential_set_fk_fix"
branch_labels = None
depends_on = None

_COLUMN = "input_digest"
_TABLE = "credential_use_grants"
_COMMENT = "签发授权时冻结的普通变量输入摘要；为空表示未绑定输入的旧授权，不参与执行"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=64), nullable=True, comment=_COMMENT),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN, schema="app")
