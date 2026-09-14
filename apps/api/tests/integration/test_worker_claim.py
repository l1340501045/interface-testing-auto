"""真实数据库集成：两 worker 争抢、旧 fencing token 拒收与 RLS 租户隔离。

对应验收 SC-08（工作项恢复、过期 fencing token、终态覆写）与 SC-10（RLS 由
无绕过权限的角色实际验证）。数据由本测试自建并清理，不使用开发库既有数据。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

pytestmark = pytest.mark.integration


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=False)


def _seed_tenant(connection: psycopg.Connection, name: str) -> dict:
    """写入一个工作空间、项目、执行池、环境与一个排队工作项。"""
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    project_id, pool_id, environment_id, run_id, job_id = (uuid.uuid4() for _ in range(5))
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspaces (id, name, status) VALUES (%s, %s, 'active')",
            (workspace_id, name),
        )
        cursor.execute(
            "INSERT INTO app.projects (id, workspace_id, key, name, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (project_id, workspace_id, name.lower(), name),
        )
        cursor.execute(
            "INSERT INTO app.runner_pools (id, workspace_id, name, allowed_targets, status)"
            " VALUES (%s, %s, %s, %s::jsonb, 'active')",
            (pool_id, workspace_id, "默认执行池", '["http://echo:8080"]'),
        )
        cursor.execute(
            "INSERT INTO app.runner_pool_project_grants"
            " (id, workspace_id, project_id, pool_id, granted_by, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (uuid.uuid4(), workspace_id, project_id, pool_id, user_id),
        )
        cursor.execute(
            "INSERT INTO app.environments"
            " (id, workspace_id, project_id, name, kind, base_url, pool_id, variables, status)"
            " VALUES (%s, %s, %s, %s, 'test', %s, %s, '{}'::jsonb, 'active')",
            (environment_id, workspace_id, project_id, "测试环境", "http://echo:8080", pool_id),
        )
        now = datetime.now(UTC)
        cursor.execute(
            "INSERT INTO app.runs"
            " (id, workspace_id, project_id, target_type, debug_snapshot, environment_id,"
            "  trigger, state, snapshot, pool_id, queue_deadline_at, business_deadline_at,"
            "  hard_deadline_at, cleanup_budget_ms, created_by)"
            " VALUES (%s, %s, %s, 'debug_snapshot', %s::jsonb, %s, 'manual', 'queued',"
            "  '{}'::jsonb, %s, %s, %s, %s, 60000, %s)",
            (
                run_id,
                workspace_id,
                project_id,
                '{"request": {"method": "GET", "path": "/echo"}}',
                environment_id,
                pool_id,
                now + timedelta(seconds=60),
                now + timedelta(seconds=300),
                now + timedelta(seconds=360),
                user_id,
            ),
        )
        cursor.execute(
            "INSERT INTO app.jobs (id, workspace_id, project_id, run_id, pool_id, state, available_at)"
            " VALUES (%s, %s, %s, %s, %s, 'queued', now())",
            (job_id, workspace_id, project_id, run_id, pool_id),
        )
    connection.commit()
    return {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "pool_id": pool_id,
        "run_id": run_id,
        "job_id": job_id,
    }


def _cleanup(connection: psycopg.Connection, tenant: dict) -> None:
    """按依赖顺序清理本测试自建的数据。

    工作项对执行池是 RESTRICT，而工作空间到池是 CASCADE；同时存在两条删除路径时
    PostgreSQL 不保证先后，因此先显式删掉工作项与运行，再删工作空间。
    """
    workspace = (tenant["workspace_id"],)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM app.jobs WHERE workspace_id = %s", workspace)
        cursor.execute("DELETE FROM app.runs WHERE workspace_id = %s", workspace)
        cursor.execute("DELETE FROM app.environments WHERE workspace_id = %s", workspace)
        cursor.execute(
            "DELETE FROM app.runner_pool_project_grants WHERE workspace_id = %s", workspace
        )
        cursor.execute("DELETE FROM app.runner_pools WHERE workspace_id = %s", workspace)
        cursor.execute("DELETE FROM app.workspaces WHERE id = %s", workspace)
    connection.commit()


def _claim(connection: psycopg.Connection, worker: str, pool_id: uuid.UUID, lease: int = 30):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT job_id, run_id, workspace_id, fencing_token, attempt"
            " FROM app.claim_job(%s, %s::uuid[], %s)",
            (worker, [pool_id], lease),
        )
        return cursor.fetchone()


def test_two_workers_only_one_claims(migrator_url: str, runtime_url: str) -> None:
    """两个 worker 抢同一个工作项：只有一个领到，另一个返回空。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "争抢测试")
    try:
        first = _connect(runtime_url)
        second = _connect(runtime_url)
        try:
            claim_a = _claim(first, "worker-a", tenant["pool_id"])
            first.commit()
            claim_b = _claim(second, "worker-b", tenant["pool_id"])
            second.commit()
            winners = [item for item in (claim_a, claim_b) if item is not None]
            assert len(winners) == 1, "同一个工作项只能被一个 worker 领到"
            assert winners[0][0] == tenant["job_id"]
            assert winners[0][3] == 1, "首次领取的 fencing token 应为 1"
            assert winners[0][4] == 1, "首次领取的尝试序号应为 1"
        finally:
            first.close()
            second.close()
    finally:
        _cleanup(admin, tenant)
        admin.close()


def test_expired_lease_is_reclaimable_with_higher_token(
    migrator_url: str, runtime_url: str
) -> None:
    """租约到期后可被重新领取，fencing token 递增。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "租约恢复测试")
    try:
        worker_a = _connect(runtime_url)
        worker_b = _connect(runtime_url)
        try:
            first = _claim(worker_a, "worker-a", tenant["pool_id"], lease=1)
            worker_a.commit()
            assert first is not None
            # 模拟租约自然过期：不等待真实秒数，直接回拨到期时间。
            with admin.cursor() as cursor:
                cursor.execute(
                    "UPDATE app.jobs SET lease_until = now() - interval '1 minute'"
                    " WHERE id = %s",
                    (tenant["job_id"],),
                )
            admin.commit()

            second = _claim(worker_b, "worker-b", tenant["pool_id"])
            worker_b.commit()
            assert second is not None, "租约过期后应可被重新领取"
            assert second[3] == first[3] + 1, "重新领取必须递增 fencing token"
            assert second[4] == first[4] + 1, "重新领取必须递增尝试序号"
        finally:
            worker_a.close()
            worker_b.close()
    finally:
        _cleanup(admin, tenant)
        admin.close()


def test_stale_fencing_token_cannot_finish(migrator_url: str, runtime_url: str) -> None:
    """旧 token 的终态提交必须失败，不能覆写当前状态。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "旧令牌测试")
    try:
        worker_a = _connect(runtime_url)
        worker_b = _connect(runtime_url)
        try:
            first = _claim(worker_a, "worker-a", tenant["pool_id"], lease=1)
            worker_a.commit()
            stale_token = first[3]
            with admin.cursor() as cursor:
                cursor.execute(
                    "UPDATE app.jobs SET lease_until = now() - interval '1 minute'"
                    " WHERE id = %s",
                    (tenant["job_id"],),
                )
            admin.commit()
            second = _claim(worker_b, "worker-b", tenant["pool_id"])
            worker_b.commit()
            assert second[3] != stale_token

            # 条件更新同时校验 state、leased_by 与 fencing_token。
            with worker_a.cursor() as cursor:
                cursor.execute(
                    "UPDATE app.jobs SET state = 'done'"
                    " WHERE id = %s AND state = 'leased' AND leased_by = %s"
                    " AND fencing_token = %s RETURNING id",
                    (tenant["job_id"], "worker-a", stale_token),
                )
                assert cursor.fetchone() is None, "旧 token 的终态提交必须被拒绝"
            worker_a.commit()

            with admin.cursor() as cursor:
                cursor.execute("SELECT state, leased_by FROM app.jobs WHERE id = %s", (tenant["job_id"],))
                state, leased_by = cursor.fetchone()
            assert (state, leased_by) == ("leased", "worker-b"), "当前租约持有者不得被旧令牌覆盖"
        finally:
            worker_a.close()
            worker_b.close()
    finally:
        _cleanup(admin, tenant)
        admin.close()


def test_runtime_role_cannot_cross_workspace(migrator_url: str, runtime_url: str) -> None:
    """运行角色在另一租户上下文下读不到本租户数据。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "隔离测试")
    other = _seed_tenant(admin, "另一租户")
    try:
        connection = _connect(runtime_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.workspace_id', %s, true)", (str(tenant["workspace_id"]),))
                cursor.execute("SELECT count(*) FROM app.projects")
                assert cursor.fetchone()[0] == 1, "应只看到本工作空间的项目"

                cursor.execute("SELECT set_config('app.workspace_id', %s, true)", (str(other["workspace_id"]),))
                cursor.execute(
                    "SELECT count(*) FROM app.projects WHERE id = %s", (tenant["project_id"],)
                )
                assert cursor.fetchone()[0] == 0, "跨工作空间的项目不得可见"
            connection.rollback()
        finally:
            connection.close()
    finally:
        _cleanup(admin, tenant)
        _cleanup(admin, other)
        admin.close()


def test_runtime_role_has_no_bypass_rls(runtime_url: str) -> None:
    """运行角色本身不具绕过 RLS 的能力。"""
    connection = _connect(runtime_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user"
            )
            bypass, superuser = cursor.fetchone()
        assert bypass is False and superuser is False
    finally:
        connection.close()


# —— 续租：只有仍然有效的那一份租约可以被延长 ——


def _claim_with_executor(pool_id: uuid.UUID, worker: str, lease: int = 30):
    """走执行器真实的领取入口，拿到与线上 worker 一致的领取上下文。"""
    from app.db import get_session_factory
    from app.services.executor import claim_job

    session = get_session_factory()()
    try:
        claim = claim_job(session, worker, [pool_id], lease)
        session.commit()
        return claim
    finally:
        session.close()


def _heartbeat(claim, worker: str, lease_seconds: int = 30):
    """真实的续租入口：worker 执行期间用的就是这一个对象与这一个方法。"""
    import threading

    from app.db import get_session_factory
    from app.worker import Heartbeat

    return Heartbeat(
        session_factory=get_session_factory(),
        claim=claim,
        worker_id=worker,
        lease_seconds=lease_seconds,
        interval=1.0,
        lost=threading.Event(),
    )


def _expire_job(admin: psycopg.Connection, job_id: uuid.UUID) -> None:
    """把租约回拨到过去：复现“已过期但尚未被重新领取”，不等待真实秒数。"""
    with admin.cursor() as cursor:
        cursor.execute(
            "UPDATE app.jobs SET lease_until = now() - interval '1 minute' WHERE id = %s",
            (job_id,),
        )
    admin.commit()


def _job_lease(admin: psycopg.Connection, job_id: uuid.UUID):
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT lease_until, leased_by, fencing_token FROM app.jobs WHERE id = %s",
            (job_id,),
        )
        row = cursor.fetchone()
    admin.commit()
    return row


def test_heartbeat_extends_a_live_lease(migrator_url: str) -> None:
    """正常续租：租约仍有效时，心跳按本次租期把到期时间重新算晚。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "心跳续租")
    try:
        claim = _claim_with_executor(tenant["pool_id"], "worker-hb", lease=1)
        assert claim is not None, "排队中的工作项应可被领取"
        before, holder, token = _job_lease(admin, tenant["job_id"])
        assert (holder, token) == ("worker-hb", claim.fencing_token)

        heartbeat = _heartbeat(claim, "worker-hb", lease_seconds=30)
        heartbeat._beat()

        after, holder_after, token_after = _job_lease(admin, tenant["job_id"])
        assert not heartbeat.lost.is_set(), "租约有效时心跳不得判定为丢失"
        assert (after - before).total_seconds() > 20, "续租应按本次租期重新计算到期时间"
        assert (holder_after, token_after) == (holder, token), "续租不改持有者与令牌"
    finally:
        _cleanup(admin, tenant)
        admin.close()


def test_heartbeat_cannot_revive_an_expired_lease(migrator_url: str) -> None:
    """已过期但尚无人接管：心跳不得把这份旧租约重新延长。

    进程暂停（长 GC、宿主机挂起、断点调试）到租约过期后，如果还没有别的 worker
    接管，一次心跳就能把旧租约续上，让一个本应重新领取的执行者继续通过发送前校验。
    到期只能靠重新领取恢复，不能靠心跳复活。
    """
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "过期租约心跳")
    try:
        claim = _claim_with_executor(tenant["pool_id"], "worker-hb")
        assert claim is not None
        _expire_job(admin, tenant["job_id"])
        before, holder, token = _job_lease(admin, tenant["job_id"])
        assert (holder, token) == ("worker-hb", claim.fencing_token), "本用例里并没有发生接管"

        heartbeat = _heartbeat(claim, "worker-hb")
        heartbeat._beat()

        after, holder_after, token_after = _job_lease(admin, tenant["job_id"])
        assert after == before, "过期租约不得被心跳延长"
        assert (holder_after, token_after) == (holder, token), "心跳不得改动持有者与令牌"
        assert heartbeat.lost.is_set(), "续租未生效时必须判定本次领取已丢失"
    finally:
        _cleanup(admin, tenant)
        admin.close()


def test_heartbeat_from_a_taken_over_lease_is_rejected(migrator_url: str) -> None:
    """已被接管的旧租约：心跳必须失败，且不得改动当前那一份租约。"""
    admin = _connect(migrator_url)
    tenant = _seed_tenant(admin, "接管后心跳")
    try:
        claim_a = _claim_with_executor(tenant["pool_id"], "worker-a")
        assert claim_a is not None
        _expire_job(admin, tenant["job_id"])
        claim_b = _claim_with_executor(tenant["pool_id"], "worker-b")
        assert claim_b is not None, "过期租约应可被重新领取"
        assert claim_b.fencing_token == claim_a.fencing_token + 1, "重新领取必须递增令牌"
        before, holder, token = _job_lease(admin, tenant["job_id"])
        assert (holder, token) == ("worker-b", claim_b.fencing_token)

        heartbeat = _heartbeat(claim_a, "worker-a")
        heartbeat._beat()

        after, holder_after, token_after = _job_lease(admin, tenant["job_id"])
        assert heartbeat.lost.is_set(), "旧令牌的心跳必须判定为丢失"
        assert (after, holder_after, token_after) == (before, holder, token), (
            "旧执行者的心跳不得改动当前租约"
        )
    finally:
        _cleanup(admin, tenant)
        admin.close()
