"""0010旧业务数据升级0011的真实迁移回归；测试结束恢复0011。"""
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from helpers import migrator_connection

pytestmark = pytest.mark.integration


def _alembic() -> Config:
    api_root = Path(__file__).parents[2]
    return Config(str(api_root / "alembic.ini"))


def _cleanup_fixture(connection, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """只用0010/0011共同旧表按本例scope清理，之后才能独立恢复head。"""
    connection.rollback()
    statements = [
        "DELETE FROM app.jobs WHERE workspace_id=%s",
        "DELETE FROM app.run_step_attempts WHERE workspace_id=%s",
        "DELETE FROM app.runs WHERE workspace_id=%s",
        "DELETE FROM app.credential_use_grants WHERE workspace_id=%s",
        "DELETE FROM app.credential_profiles WHERE workspace_id=%s",
        "DELETE FROM app.cases WHERE workspace_id=%s",
        "DELETE FROM app.folders WHERE workspace_id=%s",
        "DELETE FROM app.environments WHERE workspace_id=%s",
        "DELETE FROM app.project_config_versions WHERE workspace_id=%s",
        "DELETE FROM app.runner_pool_project_grants WHERE workspace_id=%s",
        "DELETE FROM app.runner_pools WHERE workspace_id=%s",
        "DELETE FROM app.workspace_memberships WHERE workspace_id=%s",
        "DELETE FROM app.projects WHERE workspace_id=%s",
        "DELETE FROM app.workspaces WHERE id=%s",
        "DELETE FROM app.sessions WHERE user_id=%s",
        "DELETE FROM app.users WHERE id=%s",
    ]
    for statement in statements[:-2]:
        connection.execute(statement, (workspace_id,))
    for statement in statements[-2:]:
        connection.execute(statement, (user_id,))
    connection.commit()


def test_upgrade_with_archived_bad_url_environment_preserves_old_rows() -> None:
    config = _alembic()
    command.downgrade(config, "0010_environment_config_versions")

    connection = migrator_connection()
    user_id, workspace_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool_id, environment_id, config_id, run_id = (
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )
    bad_url = "legacy-target:8080/path"
    variables = {"旧变量": {"type": "string", "text": "保持"}}
    updated_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(91011001)")
            cursor.execute(
                "INSERT INTO app.users(id,username,password_hash,display_name,status) "
                "VALUES (%s,%s,'x','迁移回归','active')",
                (user_id, f"migration-{user_id.hex[:12]}"),
            )
            cursor.execute(
                "INSERT INTO app.workspaces(id,name,status) VALUES (%s,'迁移回归空间','active')",
                (workspace_id,),
            )
            cursor.execute(
                "INSERT INTO app.workspace_memberships(workspace_id,user_id,role) "
                "VALUES (%s,%s,'admin')",
                (workspace_id, user_id),
            )
            cursor.execute(
                "INSERT INTO app.projects(id,workspace_id,key,name,status) "
                "VALUES (%s,%s,%s,'迁移回归项目','active')",
                (project_id, workspace_id, f"migration-{project_id.hex[:8]}"),
            )
            cursor.execute(
                "INSERT INTO app.runner_pools(id,workspace_id,name,allowed_targets,status) "
                "VALUES (%s,%s,'迁移池','[]'::jsonb,'active')",
                (pool_id, workspace_id),
            )
            cursor.execute(
                "INSERT INTO app.runner_pool_project_grants"
                "(id,workspace_id,project_id,pool_id,granted_by,status) "
                "VALUES (%s,%s,%s,%s,%s,'active')",
                (uuid.uuid4(), workspace_id, project_id, pool_id, user_id),
            )
            cursor.execute("SET CONSTRAINTS app.fk_environments_current_config_version DEFERRED")
            cursor.execute(
                "INSERT INTO app.environments"
                "(id,workspace_id,project_id,name,kind,base_url,pool_id,variables,rev,status,"
                " current_config_version_id,updated_at) "
                "VALUES (%s,%s,%s,'旧归档环境','test',%s,%s,%s,17,'archived',%s,%s)",
                (
                    environment_id, workspace_id, project_id, bad_url, pool_id,
                    Jsonb(variables), config_id, updated_at,
                ),
            )
            cursor.execute(
                "INSERT INTO app.environment_config_versions"
                "(id,workspace_id,project_id,environment_id,version,schema_version,snapshot) "
                "VALUES (%s,%s,%s,%s,1,1,%s)",
                (
                    config_id, workspace_id, project_id, environment_id,
                    Jsonb({"schema_version": 1, "variables": variables}),
                ),
            )
            now = datetime.now(UTC)
            snapshot = {
                "legacy_marker": "keep",
                "environment": {
                    "id": str(environment_id), "name": "旧归档环境",
                    "kind": "test", "base_url": bad_url,
                },
                "resolution": {
                    "schema_version": 1,
                    "config_basis": {
                        "environment_config_version_id": str(config_id),
                        "environment_config_version": 1,
                    },
                },
            }
            cursor.execute(
                "INSERT INTO app.runs"
                "(id,workspace_id,project_id,target_type,debug_snapshot,environment_id,trigger,"
                " state,outcome,snapshot,pool_id,queue_deadline_at,business_deadline_at,"
                " hard_deadline_at,cleanup_budget_ms,created_by) "
                "VALUES (%s,%s,%s,'debug_snapshot',%s,%s,'manual','finished',"
                " 'completed_unchecked',%s,%s,%s,%s,%s,60000,%s)",
                (
                    run_id, workspace_id, project_id,
                    Jsonb({"request": {"method": "GET", "path": "/echo"}}),
                    environment_id, Jsonb(snapshot), pool_id,
                    now + timedelta(minutes=1), now + timedelta(minutes=5),
                    now + timedelta(minutes=6), user_id,
                ),
            )
        connection.commit()
        before = connection.execute(
            "SELECT name,base_url,variables,rev,status,updated_at FROM app.environments "
            "WHERE id=%s", (environment_id,),
        ).fetchone()
        old_snapshot = connection.execute(
            "SELECT snapshot FROM app.runs WHERE id=%s", (run_id,)
        ).fetchone()[0]
        connection.close()

        command.upgrade(config, "0011_multi_service_targets")
        connection = migrator_connection()
        after = connection.execute(
            "SELECT name,base_url,variables,rev,status,updated_at FROM app.environments "
            "WHERE id=%s", (environment_id,),
        ).fetchone()
        assert after == before
        assert connection.execute(
            "SELECT snapshot FROM app.runs WHERE id=%s", (run_id,)
        ).fetchone()[0] == old_snapshot
        row = connection.execute(
            "SELECT m.current_version_id IS NOT NULL,m.status,v.base_url,v.status "
            "FROM app.environment_service_mappings m "
            "JOIN app.environment_service_versions v ON v.id=m.current_version_id "
            "WHERE m.environment_id=%s", (environment_id,),
        ).fetchone()
        assert row == (True, "archived", bad_url, "archived")
        constraint = connection.execute(
            "SELECT condeferrable,condeferred FROM pg_constraint "
            "WHERE conname='fk_environment_service_mappings_current_version' "
            "AND conrelid='app.environment_service_mappings'::regclass"
        ).fetchone()
        assert constraint == (True, True)
        assert connection.execute(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid='app.environment_service_mappings'::regclass "
            "AND attname='current_version_id'"
        ).fetchone()[0] is True
    finally:
        original = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        if not connection.closed:
            try:
                connection.rollback()
            except Exception as error:
                cleanup_errors.append(error)
            finally:
                try:
                    connection.close()
                except Exception as error:
                    cleanup_errors.append(error)
        try:
            cleanup_connection = migrator_connection()
            try:
                _cleanup_fixture(cleanup_connection, workspace_id, user_id)
            finally:
                cleanup_connection.close()
        except Exception as error:  # 清理错误须与原升级错误分别保留
            cleanup_errors.append(error)
        try:
            command.upgrade(config, "0011_multi_service_targets")
        except Exception as error:  # 恢复错误不能覆盖原失败
            cleanup_errors.append(error)
        if cleanup_errors:
            detail = "; ".join(f"{type(item).__name__}: {item}" for item in cleanup_errors)
            if original is not None:
                original.add_note(f"迁移回归清理/恢复另有失败：{detail}")
            else:
                raise ExceptionGroup("迁移回归清理/恢复失败", cleanup_errors)
