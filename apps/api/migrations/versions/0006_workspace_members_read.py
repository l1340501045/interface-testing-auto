"""工作空间成员元数据读取函数：按中文姓名选主体的最小读取面。

Revision ID: 0006_workspace_members_read
Revises: 0005_grant_input_digest
Create Date: 2026-09-14

背景：签发凭证用途授权时要指定“被授权主体”。此前界面只能让管理员手抄一个用户
UUID——普通用户无从得知同事的 id，抄错的目标在签发时看不出问题，要等执行时才发现
授权落到了别人身上。

成员表 `workspace_memberships` 的 RLS 策略只放行本人的那一行（`workspace_memberships_self`），
这是有意的：运行角色不该在普通查询里看见别人的成员关系。因此这里沿用 0002 已确立的
做法——把跨成员的读取收敛成一个单用途 SECURITY DEFINER 函数，由不可登录的
app_job_broker（BYPASSRLS）拥有，只做“列出某个工作空间的成员姓名与角色”这一件事，
且**调用者必须是该工作空间的成员**，否则返回空集。运行角色本身仍无 BYPASSRLS。

函数只返回 user_id / username / display_name / role，不含密码哈希、会话或任何秘密；
不提供成员增删改，成员管理不在本版本范围内。

只新增函数与授权，不改表结构与数据，可安全回退到 0005。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0006_workspace_members_read"
down_revision = "0005_grant_input_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "app_runtime"
BROKER_ROLE = "app_job_broker"

_MEMBERS_FUNCTION = """
CREATE OR REPLACE FUNCTION app.list_workspace_members(
    p_workspace_id uuid,
    p_caller_id uuid
) RETURNS TABLE (
    user_id uuid,
    username text,
    display_name text,
    role text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = app, pg_catalog
AS $members$
    SELECT m.user_id, u.username, u.display_name, m.role
    FROM app.workspace_memberships m
    JOIN app.users u ON u.id = m.user_id
    WHERE m.workspace_id = p_workspace_id
      AND EXISTS (
          SELECT 1
          FROM app.workspace_memberships caller
          WHERE caller.workspace_id = p_workspace_id
            AND caller.user_id = p_caller_id
      )
    ORDER BY u.display_name, m.created_at
$members$;
"""

_SIGNATURE = "app.list_workspace_members(uuid, uuid)"


def _role_exists(role: str) -> bool:
    return (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role})
        .scalar()
        is not None
    )


def upgrade() -> None:
    # 函数属主必须是 broker（BYPASSRLS）：成员表的 RLS 只放行本人那一行，属主身份
    # 才能看到同一工作空间的其他成员。授予的读取权限仅限这两张表。
    if not _role_exists(BROKER_ROLE):
        op.execute(f"CREATE ROLE {BROKER_ROLE} WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
    op.execute(f"ALTER ROLE {BROKER_ROLE} BYPASSRLS")
    op.execute(f"GRANT USAGE ON SCHEMA app TO {BROKER_ROLE}")
    op.execute(f"GRANT SELECT ON app.workspace_memberships, app.users TO {BROKER_ROLE}")
    op.execute(_MEMBERS_FUNCTION)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {BROKER_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")

    if _role_exists(RUNTIME_ROLE):
        op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {RUNTIME_ROLE}")

    op.execute(
        f"COMMENT ON FUNCTION {_SIGNATURE} IS "
        "'列出工作空间成员的姓名与角色，供签发授权时按姓名选择主体；"
        "调用者必须是该工作空间成员，否则返回空集；不含秘密与任何凭据。'"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute(f"REVOKE SELECT ON app.workspace_memberships, app.users FROM {BROKER_ROLE}")
