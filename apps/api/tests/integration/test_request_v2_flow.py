"""请求 v2 的持久化、能力门禁与固定版本运行幂等证据。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from harness import create_environment, migrator_connection, project_base

pytestmark = pytest.mark.integration


def _row(*, name: str = "q", enabled: bool = True) -> dict:
    return {
        "row_id": str(uuid.uuid4()),
        "name": name,
        "value": "1",
        "enabled": enabled,
        "description": "说明",
    }


def _request_v2(row: dict) -> dict:
    return {
        "schema_version": 2,
        "method": "GET",
        "path": "/echo",
        "query_params": [row],
        "headers": [],
        "body_type": "none",
        "body": "",
    }


def _assertion(row_id: str) -> dict:
    return {
        "id": "row-exists",
        "target_source": "request.query",
        "selector": [
            {"kind": "row", "row_id": row_id},
            {"kind": "key", "key": "value"},
        ],
        "type": "exists",
        "parameters": {},
        "severity": "error",
    }


def _plant_legacy_failed_report(run_id: str, selector: object) -> None:
    """模拟升级前已入队、由旧 worker 记为配置失败的调试运行。"""
    from psycopg.types.json import Jsonb

    assertion = {
        "id": f"legacy-{uuid.uuid4().hex}",
        "target_source": "request.query",
        "selector": selector,
        "type": "exists",
        "parameters": {},
        "severity": "error",
    }
    attempt_id = uuid.uuid4()
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.runs SET snapshot = jsonb_set(snapshot, '{assertions}', %s), "
                "state = 'finished', outcome = 'error', reason_category = 'configuration' "
                "WHERE id = %s",
                (Jsonb([assertion]), run_id),
            )
            cursor.execute("UPDATE app.jobs SET state = 'done' WHERE run_id = %s", (run_id,))
            cursor.execute(
                "INSERT INTO app.run_step_attempts "
                "(id, workspace_id, project_id, run_id, step_key, attempt_no, state, outcome, "
                "finished_at, request, error_code) "
                "SELECT %s, workspace_id, project_id, id, 'main', 1, 'skipped', 'error', "
                "now(), %s, 'case_invalid' FROM app.runs WHERE id = %s",
                (
                    attempt_id,
                    Jsonb({"rejected": "case_invalid", "message": "历史断言配置无效"}),
                    run_id,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_v2_case_requires_capability_and_cannot_be_downgraded(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    row = _row()
    payload = {
        "name": "v2 用例",
        "request": _request_v2(row),
        "assertions": [_assertion(row["row_id"])],
    }

    blocked = client.post(f"{base}/cases", json=payload)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "client_contract_required"

    created = client.post(
        f"{base}/cases", json=payload, headers={"X-Request-Contract": "2"}
    )
    assert created.status_code == 201, created.text
    case_id = created.json()["id"]
    assert created.json()["request"] == payload["request"]

    old_read = client.get(f"{base}/cases/{case_id}")
    assert old_read.status_code == 409
    current = client.get(
        f"{base}/cases/{case_id}", headers={"X-Request-Contract": "2"}
    )
    assert current.status_code == 200, current.text

    downgrade = client.patch(
        f"{base}/cases/{case_id}",
        json={"request": {"method": "GET", "path": "/echo"}},
        headers={"If-Match": current.headers["ETag"], "X-Request-Contract": "2"},
    )
    assert downgrade.status_code == 409
    assert downgrade.json()["code"] == "request_contract_downgrade"

    publish_blocked = client.post(
        f"{base}/cases/{case_id}/publish", json={"draft_rev": created.json()["rev"]}
    )
    assert publish_blocked.status_code == 409
    published = client.post(
        f"{base}/cases/{case_id}/publish",
        json={"draft_rev": created.json()["rev"]},
        headers={"X-Request-Contract": "2"},
    )
    assert published.status_code == 201, published.text
    assert published.json()["schema_version"] == 2

    environment = create_environment(client, account, project, name="v2 版本执行环境")
    run = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "case_version_id": published.json()["id"],
        },
    )
    assert run.status_code == 202, run.text
    from test_execution_flow import _run_once

    assert _run_once(project["pool_id"]) == "completed_unchecked"
    report = client.get(
        f"{base}/runs/{run.json()['id']}/report",
        headers={"X-Request-Contract": "2"},
    )
    assert report.status_code == 200, report.text
    assert report.json()["request"]["url"].endswith("/echo?q=1")


def test_debug_v2_and_row_report_are_closed_to_old_clients(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="v2 调试环境")
    row = _row()
    disabled = _row(name="disabled", enabled=False)
    disabled["value"] = "{{never_defined}}"
    request = _request_v2(row)
    request["query_params"].insert(0, disabled)
    snapshot = {
        "request": request,
        "assertions": [_assertion(row["row_id"])],
    }

    digest = client.post(f"{base}/debug-snapshot-digest", json=snapshot)
    assert digest.status_code == 409
    accepted = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers={"X-Request-Contract": "2"},
    )
    assert accepted.status_code == 202, accepted.text
    from test_execution_flow import _run_once

    assert _run_once(project["pool_id"]) == "completed_unchecked"

    old_report = client.get(f"{base}/runs/{accepted.json()['id']}/report")
    assert old_report.status_code == 409
    new_report = client.get(
        f"{base}/runs/{accepted.json()['id']}/report",
        headers={"X-Request-Contract": "2"},
    )
    assert new_report.status_code == 200, new_report.text
    assert new_report.json()["request"]["url"].endswith("/echo?q=1")
    assert "disabled" not in new_report.text
    assert "说明" not in new_report.text


def test_case_version_run_idempotency_replays_and_rejects_different_content(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="版本幂等环境")
    second_environment = create_environment(client, account, project, name="版本幂等环境二")
    created = client.post(
        f"{base}/cases",
        json={"name": "固定版本", "request": {"method": "GET", "path": "/echo"}},
    )
    assert created.status_code == 201, created.text
    published = client.post(
        f"{base}/cases/{created.json()['id']}/publish",
        json={"draft_rev": created.json()["rev"]},
    )
    assert published.status_code == 201, published.text

    key = uuid.uuid4().hex
    payload = {
        "environment_id": environment["id"],
        "case_version_id": published.json()["id"],
    }
    first = client.post(f"{base}/runs", json=payload, headers={"Idempotency-Key": key})
    second = client.post(f"{base}/runs", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]

    conflict = client.post(
        f"{base}/runs",
        json={**payload, "environment_id": second_environment["id"]},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 400
    assert conflict.json()["code"] == "idempotency_conflict"


@pytest.mark.parametrize("legacy_selector", [7, True])
def test_old_and_new_clients_can_read_legacy_failed_reports_with_non_list_selectors(
    client: TestClient,
    account: dict,
    project: dict,
    legacy_selector: object,
) -> None:
    base = project_base(account, project)
    environment = create_environment(
        client, account, project, name=f"历史失败-{type(legacy_selector).__name__}"
    )
    accepted = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}},
        },
    )
    assert accepted.status_code == 202, accepted.text
    _plant_legacy_failed_report(accepted.json()["id"], legacy_selector)

    for headers in ({}, {"X-Request-Contract": "2"}):
        report = client.get(
            f"{base}/runs/{accepted.json()['id']}/report",
            headers=headers,
        )
        assert report.status_code == 200, report.text
        assert report.json()["run"]["reason_category"] == "configuration"
        assert report.json()["steps"][0]["error_code"] == "case_invalid"
