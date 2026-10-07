"""S2 0011真实约束与最终ACL专题。"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from harness import (
    CONTROLLED_TARGET_ALT_BASE_URL,
    create_environment,
    migrator_connection,
    project_base,
)

pytestmark = pytest.mark.integration


def test_run_service_version_pair_rejects_each_half_null(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="S2成对约束")
    service = client.post(f"{base}/services", json={"name": "成对服务"}).json()
    config = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{"service_key": service["service_key"], "base_url": CONTROLLED_TARGET_ALT_BASE_URL}]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert config.status_code == 200, config.text
    mapping = next(item["mapping"] for item in config.json()["items"] if item["service_key"] == service["service_key"])
    run = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": {"request": {"method": "GET", "path": "/echo", "body_type": "none", "body": ""}, "assertions": []}},
    )
    assert run.status_code == 202, run.text
    connection = migrator_connection()
    try:
        for statement, value in (
            ("UPDATE app.runs SET service_id=%s WHERE id=%s", service["id"]),
            ("UPDATE app.runs SET environment_service_version_id=%s WHERE id=%s", mapping["version_id"]),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(statement, (value, run.json()["id"]))
    finally:
        connection.close()


def test_runtime_acl_keeps_versions_immutable_in_visible_workspace(
    client: TestClient, account: dict, project: dict, runtime_url: str
) -> None:
    environment = create_environment(client, account, project, name="S2权限环境")
    connection = psycopg.connect(runtime_url, autocommit=False)
    try:
        connection.execute(
            "SELECT set_config('app.workspace_id', %s, true)",
            (str(account["workspace_id"]),),
        )
        version_id = connection.execute(
            "SELECT v.id FROM app.environment_service_versions v "
            "WHERE v.environment_id=%s",
            (environment["id"],),
        ).fetchone()[0]
        privileges = connection.execute(
            "SELECT "
            "has_table_privilege('app_runtime','app.environment_service_versions','INSERT'),"
            "has_table_privilege('app_runtime','app.environment_service_versions','UPDATE'),"
            "has_table_privilege('app_runtime','app.environment_service_versions','DELETE'),"
            "has_table_privilege('app_runtime','app.environment_config_versions','UPDATE'),"
            "has_table_privilege('app_runtime','app.project_services','DELETE')"
        ).fetchone()
        assert privileges == (True, False, False, False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "UPDATE app.environment_service_versions SET version=version+1 WHERE id=%s",
                (version_id,),
            )
    finally:
        connection.rollback()
        connection.close()


def test_version_mapping_five_dimension_fk_rejects_wrong_environment(
    client: TestClient, account: dict, project: dict
) -> None:
    first = create_environment(client, account, project, name="S2范围甲")
    second = create_environment(client, account, project, name="S2范围乙")
    connection = migrator_connection()
    try:
        row = connection.execute(
            "SELECT m.workspace_id,m.project_id,m.service_id,m.id,v.base_url,v.status "
            "FROM app.environment_service_mappings m JOIN app.environment_service_versions v "
            "ON v.id=m.current_version_id WHERE m.environment_id=%s",
            (first["id"],),
        ).fetchone()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO app.environment_service_versions "
                    "(id,workspace_id,project_id,environment_id,service_id,mapping_id,version,base_url,status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,99,%s,%s)",
                    (uuid.uuid4(), row[0], row[1], second["id"], row[2], row[3], row[4], row[5]),
                )
    finally:
        connection.close()
