"""工作项原子领取函数。

运行角色 app_runtime 无 BYPASSRLS，无法跨工作空间看到待领取的工作项。跨租户
领取被收敛为一个单用途 SECURITY DEFINER 函数：函数由不可登录的
app_job_broker（BYPASSRLS）拥有，只做“锁定并领取一个工作项”这一件事，
返回工作项坐标；其余全部读写仍回到调用方的租户上下文里按 RLS 执行。

这样既保证两个 worker 抢同一个工作项时只有一个成功，又没有给运行角色留下
宽泛的跨租户读取能力。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_job_broker"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "app_runtime"
BROKER_ROLE = "app_job_broker"

_CLAIM_FUNCTION = """
CREATE OR REPLACE FUNCTION app.claim_job(
    p_worker text,
    p_pools uuid[],
    p_lease_seconds integer
) RETURNS TABLE (
    job_id uuid,
    run_id uuid,
    workspace_id uuid,
    project_id uuid,
    pool_id uuid,
    fencing_token integer,
    attempt integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, pg_catalog
AS $claim$
DECLARE
    v_job uuid;
BEGIN
    SELECT j.id INTO v_job
    FROM app.jobs j
    WHERE j.pool_id = ANY(p_pools)
      AND (
            (j.state = 'queued' AND j.available_at <= now())
         OR (j.state = 'leased' AND j.lease_until IS NOT NULL AND j.lease_until < now())
      )
    ORDER BY j.available_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF v_job IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    UPDATE app.jobs j
    SET state = 'leased',
        leased_by = p_worker,
        lease_until = now() + make_interval(secs => p_lease_seconds),
        fencing_token = j.fencing_token + 1,
        attempt = j.attempt + 1,
        updated_at = now()
    WHERE j.id = v_job
    RETURNING j.id, j.run_id, j.workspace_id, j.project_id,
              j.pool_id, j.fencing_token, j.attempt;
END;
$claim$;
"""


def _role_exists(role: str) -> bool:
    return (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role})
        .scalar()
        is not None
    )


def upgrade() -> None:
    # 函数属主必须是 broker（BYPASSRLS）才能跨租户领取；app_runtime 仍无绕过权限。
    if not _role_exists(BROKER_ROLE):
        op.execute(
            f"CREATE ROLE {BROKER_ROLE} WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
        )
    op.execute(f"ALTER ROLE {BROKER_ROLE} BYPASSRLS")
    # SECURITY DEFINER 函数以属主身份执行，因此属主本身需要 schema 与表的访问权；
    # 只授予领取所需的最小权限，不授予 CREATE，也不授予其他业务表的访问权。
    op.execute(f"GRANT USAGE ON SCHEMA app TO {BROKER_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON app.jobs TO {BROKER_ROLE}")
    op.execute(_CLAIM_FUNCTION)
    op.execute(f"ALTER FUNCTION app.claim_job(text, uuid[], integer) OWNER TO {BROKER_ROLE}")
    op.execute("REVOKE ALL ON FUNCTION app.claim_job(text, uuid[], integer) FROM PUBLIC")

    if _role_exists(RUNTIME_ROLE):
        op.execute(
            f"GRANT EXECUTE ON FUNCTION app.claim_job(text, uuid[], integer) TO {RUNTIME_ROLE}"
        )

    op.execute(
        "COMMENT ON FUNCTION app.claim_job(text, uuid[], integer) IS "
        "'原子领取一个可执行工作项的跨租户单用途函数：按池过滤、跳过被他人锁定"
        "的行、递增 fencing token 并设置租约；返回工作项坐标供调用方建立租户上下文'"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.claim_job(text, uuid[], integer)")
    op.execute(f"REVOKE ALL ON app.jobs FROM {BROKER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA app FROM {BROKER_ROLE}")
