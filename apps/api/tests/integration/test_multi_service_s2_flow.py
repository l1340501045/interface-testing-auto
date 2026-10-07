"""S2 服务目录、映射、能力协商与目标冻结真实 PostgreSQL 回归。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import DEMO_SLOT, _create_profile, _create_secret
from test_execution_flow import _run_once

from harness import (
    CONTROLLED_TARGET_ALT_BASE_URL,
    CONTROLLED_TARGET_BASE_URL,
    create_environment,
    migrator_connection,
    project_base,
)

pytestmark = pytest.mark.integration
SERVICE_HEADERS = {"X-Service-Contract": "1"}


def _snapshot(*, service_key: str | None = None) -> dict:
    request = {
        "method": "GET",
        "path": "/echo",
        "query_params": [{"name": "a", "value": "1"}, {"name": "a", "value": ""}],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    if service_key is not None:
        request.update(service_contract=1, service_key=service_key)
    return {"request": request, "assertions": []}


def test_default_negotiation_and_named_mapping_flow(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="S2环境")

    services = client.get(f"{base}/services")
    assert services.status_code == 200, services.text
    default = services.json()["items"][0]
    assert default["service_key"] == "default"
    assert default["status"] == "active"

    old_preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
    )
    assert old_preview.status_code == 200, old_preview.text
    assert old_preview.json()["schema_version"] == 1
    assert "selected_target" not in old_preview.json()

    new_default = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
        headers=SERVICE_HEADERS,
    )
    assert new_default.status_code == 200, new_default.text
    assert new_default.json()["schema_version"] == 2
    assert new_default.json()["selected_target"]["kind"] == "default"
    assert new_default.json()["target_ref"]["service_key"] == "default"

    created = client.post(f"{base}/services", json={"name": "订单服务"})
    assert created.status_code == 201, created.text
    named = created.json()
    named_snapshot = _snapshot(service_key=named["service_key"])

    missing_header = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": named_snapshot},
    )
    assert missing_header.status_code == 409
    assert missing_header.json()["code"] == "service_contract_required"

    missing_mapping = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": named_snapshot},
        headers=SERVICE_HEADERS,
    )
    assert missing_mapping.status_code == 200, missing_mapping.text
    assert missing_mapping.json()["selected_target"]["availability"] == "mapping_missing"
    assert missing_mapping.json()["target_ref"] is None

    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={
            "items": [
                {
                    "service_key": named["service_key"],
                    "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
                }
            ]
        },
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    item = next(
        value for value in configured.json()["items"] if value["service_key"] == named["service_key"]
    )
    assert item["availability"] == "ready"
    assert item["mapping"]["base_url"] == CONTROLLED_TARGET_ALT_BASE_URL

    catalog = client.get(
        f"{base}/variable-context",
        params={"environment_id": environment["id"], "service_key": named["service_key"]},
        headers=SERVICE_HEADERS,
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["schema_version"] == 2
    assert catalog.json()["selected_target"]["service_key"] == named["service_key"]

    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": named_snapshot},
        headers=SERVICE_HEADERS,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["schema_version"] == 2
    assert preview.json()["target_ref"]["service_key"] == named["service_key"]
    assert preview.json()["masked_target"]["url"].startswith(CONTROLLED_TARGET_ALT_BASE_URL)

    run = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": named_snapshot,
            "resolution_context": preview.json()["resolution_context"],
        },
        headers={**SERVICE_HEADERS, "Idempotency-Key": "s2-named-run"},
    )
    assert run.status_code == 202, run.text
    named_report_old = client.get(f"{base}/runs/{run.json()['id']}/report")
    assert named_report_old.status_code == 409
    assert named_report_old.json()["code"] == "service_contract_required"
    named_report = client.get(
        f"{base}/runs/{run.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert named_report.status_code == 200, named_report.text
    assert named_report.json()["resolution"]["schema_version"] == 2


def test_default_mapping_update_keeps_legacy_address_atomic(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(
        client, account, project, name="S2默认镜像", base_url=CONTROLLED_TARGET_BASE_URL
    )
    config = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert config.status_code == 200, config.text
    default = next(item for item in config.json()["items"] if item["is_default"])
    updated = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={
            "items": [
                {
                    "service_key": "default",
                    "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
                    "expected_mapping_rev": default["mapping"]["rev"],
                }
            ]
        },
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text
    environments = client.get(f"{base}/environments")
    row = next(item for item in environments.json() if item["id"] == environment["id"])
    assert row["base_url"] == CONTROLLED_TARGET_ALT_BASE_URL
    mirrored = next(item for item in updated.json()["items"] if item["is_default"])
    assert mirrored["mapping"]["base_url"] == CONTROLLED_TARGET_ALT_BASE_URL


def test_default_schema_negotiation_and_idempotent_replay(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="S2默认协商")
    snapshot = _snapshot()
    schema1_preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    ).json()
    payload = {
        "environment_id": environment["id"],
        "debug_snapshot": snapshot,
        "resolution_context": schema1_preview["resolution_context"],
    }
    first = client.post(
        f"{base}/runs", json=payload, headers={"Idempotency-Key": "s2-default-replay"}
    )
    assert first.status_code == 202, first.text
    replay = client.post(
        f"{base}/runs",
        json=payload,
        headers={"Idempotency-Key": "s2-default-replay", **SERVICE_HEADERS},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]

    schema2_preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers=SERVICE_HEADERS,
    ).json()
    schema2_run = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": schema2_preview["resolution_context"],
        },
        headers={"Idempotency-Key": "s2-default-schema2", **SERVICE_HEADERS},
    )
    assert schema2_run.status_code == 202, schema2_run.text
    old_report = client.get(f"{base}/runs/{schema2_run.json()['id']}/report")
    assert old_report.status_code == 200, old_report.text
    assert old_report.json()["resolution"] is None
    new_report = client.get(
        f"{base}/runs/{schema2_run.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert new_report.status_code == 200, new_report.text
    assert new_report.json()["resolution"]["schema_version"] == 2


def test_named_case_content_requires_capability_and_version_keeps_service(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="S2版本环境")
    service = client.post(f"{base}/services", json={"name": "版本服务"}).json()
    config = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{"service_key": service["service_key"], "base_url": CONTROLLED_TARGET_ALT_BASE_URL}]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert config.status_code == 200, config.text
    request = _snapshot(service_key=service["service_key"])["request"]
    payload = {"name": "命名服务用例", "request": request, "assertions": []}
    denied = client.post(f"{base}/cases", json=payload)
    assert denied.status_code == 409
    assert denied.json()["code"] == "service_contract_required"
    created = client.post(f"{base}/cases", json=payload, headers=SERVICE_HEADERS)
    assert created.status_code == 201, created.text
    case_id = created.json()["id"]
    assert client.get(f"{base}/cases/{case_id}").status_code == 409
    detail = client.get(f"{base}/cases/{case_id}", headers=SERVICE_HEADERS)
    assert detail.status_code == 200
    published = client.post(
        f"{base}/cases/{case_id}/publish",
        json={"side_effect": "read", "draft_rev": detail.json()["rev"]},
        headers=SERVICE_HEADERS,
    )
    assert published.status_code == 201, published.text
    fixed = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "case_version_id": published.json()["id"]},
        headers=SERVICE_HEADERS,
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["target_ref"]["service_key"] == service["service_key"]
    fixed_payload = {
        "environment_id": environment["id"],
        "case_version_id": published.json()["id"],
        "resolution_context": fixed.json()["resolution_context"],
    }
    accepted = client.post(
        f"{base}/runs",
        json=fixed_payload,
        headers={**SERVICE_HEADERS, "Idempotency-Key": "fixed-named-replay"},
    )
    assert accepted.status_code == 202, accepted.text
    replay = client.post(
        f"{base}/runs",
        json=fixed_payload,
        headers={"Idempotency-Key": "fixed-named-replay"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == accepted.json()["id"]


def test_pool_block_does_not_falsify_ready_mapping(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="S2池阻塞")
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.runner_pool_project_grants SET status='revoked' "
            "WHERE project_id=%s AND pool_id=%s",
            (project["id"], project["pool_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    config = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert config.status_code == 200, config.text
    assert config.json()["inheritance"]["pool"]["state"] == "not_granted"
    default = next(item for item in config.json()["items"] if item["is_default"])
    assert default["availability"] == "ready"
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
        headers=SERVICE_HEADERS,
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["selected_target"]["availability"] == "ready"
    assert result["target_ref"] is not None
    assert result["resolution_context"] is not None
    assert result["ready"] is False
    assert any(item["code"] == "pool_not_granted" for item in result["issues"])

    old_preflight = client.post(
        f"{base}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
        headers=SERVICE_HEADERS,
    )
    assert old_preflight.status_code == 200, old_preflight.text
    old_result = old_preflight.json()
    assert old_result["ready"] is False
    assert old_result["resolution"]["ready"] is False
    assert old_result["resolution"]["resolution_context"] is not None
    assert any(item["code"] == "pool_not_granted" for item in old_result["issues"])


def test_named_target_ignores_coherent_bad_default_address(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="命名服务独立地址")
    service = client.post(f"{base}/services", json={"name": "可用命名服务"}).json()
    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{"service_key": service["service_key"], "base_url": CONTROLLED_TARGET_ALT_BASE_URL}]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    bad_default = "legacy-default-without-scheme:8080"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.environments SET base_url=%s WHERE id=%s",
            (bad_default, environment["id"]),
        )
        connection.execute(
            "UPDATE app.environment_service_versions v SET base_url=%s "
            "FROM app.environment_service_mappings m, app.project_services s "
            "WHERE v.id=m.current_version_id AND m.service_id=s.id AND s.is_default "
            "AND m.environment_id=%s",
            (bad_default, environment["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    snapshot = _snapshot(service_key=service["service_key"])
    for endpoint in ("debug-preflight", "resolution-preview"):
        response = client.post(
            f"{base}/{endpoint}",
            json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            headers=SERVICE_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["ready"] is True
    accepted = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers={**SERVICE_HEADERS, "Idempotency-Key": "named-bad-default"},
    )
    assert accepted.status_code == 202, accepted.text
    rejected_default = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
        headers=SERVICE_HEADERS,
    )
    assert rejected_default.status_code == 400
    assert rejected_default.json()["code"] == "environment_url_invalid"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.environment_service_versions v SET base_url='bad-named:8080' "
            "FROM app.environment_service_mappings m WHERE v.id=m.current_version_id "
            "AND m.environment_id=%s AND m.service_id=%s",
            (environment["id"], service["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    invalid_named = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers=SERVICE_HEADERS,
    )
    assert invalid_named.status_code == 400
    assert invalid_named.json()["code"] == "environment_url_invalid"


def test_same_mapping_save_does_not_move_current_or_block_accepted_run(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="同值映射")
    first = client.post(f"{base}/services", json={"name": "服务A"}).json()
    second = client.post(f"{base}/services", json={"name": "服务B"}).json()
    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [
            {"service_key": first["service_key"], "base_url": CONTROLLED_TARGET_ALT_BASE_URL},
            {"service_key": second["service_key"], "base_url": CONTROLLED_TARGET_BASE_URL},
        ]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    rows = {item["service_key"]: item for item in configured.json()["items"]}
    first_before = rows[first["service_key"]]["mapping"]
    second_before = rows[second["service_key"]]["mapping"]
    connection = migrator_connection()
    try:
        first_versions_before = connection.execute(
            "SELECT count(*) FROM app.environment_service_versions WHERE mapping_id=%s",
            (first_before["id"],),
        ).fetchone()[0]
        second_versions_before = connection.execute(
            "SELECT count(*) FROM app.environment_service_versions WHERE mapping_id=%s",
            (second_before["id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    snapshot = _snapshot(service_key=first["service_key"])
    accepted = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers={**SERVICE_HEADERS, "Idempotency-Key": "same-mapping-run"},
    )
    assert accepted.status_code == 202, accepted.text
    replay = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers={"Idempotency-Key": "same-mapping-run"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == accepted.json()["id"]
    updated = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [
            {
                "service_key": first["service_key"],
                "base_url": first_before["base_url"],
                "expected_mapping_rev": first_before["rev"],
            },
            {
                "service_key": second["service_key"],
                "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
                "expected_mapping_rev": second_before["rev"],
            },
        ]},
        headers={"If-Match": str(configured.json()["environment"]["rev"])},
    )
    assert updated.status_code == 200, updated.text
    after = {item["service_key"]: item for item in updated.json()["items"]}
    assert after[first["service_key"]]["mapping"] == first_before
    assert after[second["service_key"]]["mapping"]["version"] == second_before["version"] + 1
    connection = migrator_connection()
    try:
        assert connection.execute(
            "SELECT count(*) FROM app.environment_service_versions WHERE mapping_id=%s",
            (first_before["id"],),
        ).fetchone()[0] == first_versions_before
        assert connection.execute(
            "SELECT count(*) FROM app.environment_service_versions WHERE mapping_id=%s",
            (second_before["id"],),
        ).fetchone()[0] == second_versions_before + 1
    finally:
        connection.close()
    assert _run_once(project["pool_id"]) in {"passed", "completed_unchecked"}
    completed = client.get(
        f"{base}/runs/{accepted.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["response"]["status"] == 200
    assert len(completed.json()["steps"]) == 1
    assert completed.json()["context"]["resolution"] is not None
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.run_step_attempts SET request=jsonb_set(request, "
            "'{__platform_resolution_guard}', '[]'::jsonb) WHERE run_id=%s",
            (accepted.json()["id"],),
        )
        connection.commit()
        damaged_evidence = connection.execute(
            "SELECT request FROM app.run_step_attempts WHERE run_id=%s",
            (accepted.json()["id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    damaged_report = client.get(
        f"{base}/runs/{accepted.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert damaged_report.status_code == 200, damaged_report.text
    assert damaged_report.json()["response"]["status"] == 200
    assert damaged_report.json()["context"]["resolution"] is None
    connection = migrator_connection()
    try:
        assert connection.execute(
            "SELECT request FROM app.run_step_attempts WHERE run_id=%s",
            (accepted.json()["id"],),
        ).fetchone()[0] == damaged_evidence
    finally:
        connection.close()

    changed_run = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers={**SERVICE_HEADERS, "Idempotency-Key": "changed-mapping-run"},
    )
    assert changed_run.status_code == 202, changed_run.text
    first_current = after[first["service_key"]]["mapping"]
    changed = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{
            "service_key": first["service_key"],
            "base_url": CONTROLLED_TARGET_BASE_URL,
            "expected_mapping_rev": first_current["rev"],
        }]},
        headers={"If-Match": str(updated.json()["environment"]["rev"])},
    )
    assert changed.status_code == 200, changed.text
    assert _run_once(project["pool_id"]) == "error"
    report = client.get(
        f"{base}/runs/{changed_run.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert report.status_code == 200, report.text
    assert report.json()["steps"][-1]["error_code"] == "mapping_changed"
    assert report.json()["response"] is None


def test_worker_reads_default_basis_from_one_committed_snapshot(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker已读旧Environment后，default原子改版不能拼成不存在的损坏状态。"""
    from app.services import service_targets

    base = project_base(account, project)
    environment = create_environment(client, account, project, name="一致时点环境")
    service = client.post(f"{base}/services", json={"name": "稳定服务A"}).json()
    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{
            "service_key": service["service_key"],
            "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
        }]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    items = {item["service_key"]: item for item in configured.json()["items"]}
    selected_before = items[service["service_key"]]["mapping"]
    default_before = items["default"]["mapping"]
    run = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": _snapshot(service_key=service["service_key"]),
        },
        headers={**SERVICE_HEADERS, "Idempotency-Key": "default-interleaving"},
    )
    assert run.status_code == 202, run.text

    reached = threading.Event()
    release = threading.Event()
    real_inspect = service_targets.inspect_selected_target
    blocked = False

    def inspect_after_barrier(session, current_environment, request):
        nonlocal blocked
        if not blocked:
            blocked = True
            reached.set()
            assert release.wait(10), "配置PATCH后应释放worker目标读取"
        return real_inspect(session, current_environment, request)

    monkeypatch.setattr(service_targets, "inspect_selected_target", inspect_after_barrier)
    outcomes: list[str] = []
    failures: list[BaseException] = []

    def execute_worker() -> None:
        try:
            outcomes.append(_run_once(project["pool_id"]))
        except BaseException as error:  # 测试线程必须把原异常带回主线程
            failures.append(error)

    worker = threading.Thread(target=execute_worker, daemon=True)
    worker.start()
    try:
        assert reached.wait(10), "worker应在已读Environment后进入目标resolver"
        changed_default = client.patch(
            f"{base}/environments/{environment['id']}/service-config",
            json={"items": [{
                "service_key": "default",
                "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
                "expected_mapping_rev": default_before["rev"],
            }]},
            headers={"If-Match": str(configured.json()["environment"]["rev"])},
        )
        assert changed_default.status_code == 200, changed_default.text
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive(), "worker应在释放屏障后收敛"
    assert failures == []
    assert outcomes and outcomes[0] in {"passed", "completed_unchecked"}

    after = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert after.status_code == 200, after.text
    after_items = {item["service_key"]: item for item in after.json()["items"]}
    assert after_items[service["service_key"]]["mapping"] == selected_before
    assert after_items["default"]["mapping"]["version"] == default_before["version"] + 1
    assert after_items["default"]["mapping"]["base_url"] == CONTROLLED_TARGET_ALT_BASE_URL
    report = client.get(f"{base}/runs/{run.json()['id']}/report", headers=SERVICE_HEADERS)
    assert report.status_code == 200, report.text
    assert report.json()["response"]["status"] == 200
    assert len(report.json()["steps"]) == 1


def test_default_mirror_mismatch_has_consistent_read_failures(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="默认镜像损坏")
    mapped_service = client.post(f"{base}/services", json={"name": "已映射服务"}).json()
    missing_service = client.post(f"{base}/services", json={"name": "未映射服务"}).json()
    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{
            "service_key": mapped_service["service_key"],
            "base_url": CONTROLLED_TARGET_ALT_BASE_URL,
        }]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.environments SET base_url=%s WHERE id=%s",
            (CONTROLLED_TARGET_ALT_BASE_URL, environment["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    preflight = client.post(
        f"{base}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["resolution"] is None
    assert any(item["code"] == "config_inconsistent" for item in preflight.json()["issues"])
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
    )
    assert preview.status_code == 409
    assert preview.json()["code"] == "config_inconsistent"
    catalog = client.get(
        f"{base}/variable-context", params={"environment_id": environment["id"]}
    )
    assert catalog.status_code == 409
    assert catalog.json()["code"] == "config_inconsistent"
    for service in (mapped_service, missing_service):
        snapshot = _snapshot(service_key=service["service_key"])
        named_preflight = client.post(
            f"{base}/debug-preflight",
            json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            headers=SERVICE_HEADERS,
        )
        assert named_preflight.status_code == 200, named_preflight.text
        assert named_preflight.json()["resolution"] is None
        assert any(
            item["code"] == "config_inconsistent"
            for item in named_preflight.json()["issues"]
        )
        named_preview = client.post(
            f"{base}/resolution-preview",
            json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            headers=SERVICE_HEADERS,
        )
        assert named_preview.status_code == 409
        assert named_preview.json()["code"] == "config_inconsistent"
        named_catalog = client.get(
            f"{base}/variable-context",
            params={"environment_id": environment["id"], "service_key": service["service_key"]},
            headers=SERVICE_HEADERS,
        )
        assert named_catalog.status_code == 409
        assert named_catalog.json()["code"] == "config_inconsistent"
    connection = migrator_connection()
    try:
        before = connection.execute(
            "SELECT (SELECT count(*) FROM app.runs WHERE project_id=%s), "
            "(SELECT count(*) FROM app.jobs WHERE project_id=%s)",
            (project["id"], project["id"]),
        ).fetchone()
    finally:
        connection.close()
    for service in (mapped_service, missing_service):
        rejected = client.post(
            f"{base}/runs",
            json={
                "environment_id": environment["id"],
                "debug_snapshot": _snapshot(service_key=service["service_key"]),
            },
            headers=SERVICE_HEADERS,
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "config_inconsistent"
    connection = migrator_connection()
    try:
        after_counts = connection.execute(
            "SELECT (SELECT count(*) FROM app.runs WHERE project_id=%s), "
            "(SELECT count(*) FROM app.jobs WHERE project_id=%s)",
            (project["id"], project["id"]),
        ).fetchone()
        assert after_counts == before
    finally:
        connection.close()
    config = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert config.status_code == 200, config.text
    assert all(item["availability"] == "config_inconsistent" for item in config.json()["items"])
    assert all(item["mapping"] is None for item in config.json()["items"])


def test_missing_environment_basis_precedes_named_mapping_missing(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="全局依据损坏")
    service = client.post(f"{base}/services", json={"name": "尚未映射服务"}).json()
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.environment_config_versions c SET snapshot=jsonb_set("
            "snapshot, '{service_versions}', '[]'::jsonb) FROM app.environments e "
            "WHERE c.id=e.current_config_version_id AND e.id=%s",
            (environment["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    config = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert config.status_code == 200, config.text
    assert config.json()["environment"]["config_version"] is None
    assert all(item["availability"] == "config_inconsistent" for item in config.json()["items"])
    assert all(item["mapping"] is None for item in config.json()["items"])
    snapshot = _snapshot(service_key=service["service_key"])
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        headers=SERVICE_HEADERS,
    )
    assert preview.status_code == 409
    assert preview.json()["code"] == "config_inconsistent"

    second_environment = create_environment(
        client, account, project, name="默认映射缺失"
    )
    second_service = client.post(f"{base}/services", json={"name": "第二未映射服务"}).json()
    connection = migrator_connection()
    try:
        connection.execute(
            "DELETE FROM app.environment_service_mappings m USING app.project_services s "
            "WHERE m.service_id=s.id AND s.is_default AND m.environment_id=%s",
            (second_environment["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    for snapshot in (
        _snapshot(),
        _snapshot(service_key=second_service["service_key"]),
    ):
        headers = SERVICE_HEADERS if snapshot["request"].get("service_key") else {}
        missing_default = client.post(
            f"{base}/resolution-preview",
            json={
                "environment_id": second_environment["id"],
                "debug_snapshot": snapshot,
            },
            headers=headers,
        )
        assert missing_default.status_code == 409
        assert missing_default.json()["code"] == "config_inconsistent"


def test_corrupt_schema2_report_keeps_basic_result_and_named_capability(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="损坏报告")
    service = client.post(f"{base}/services", json={"name": "报告服务"}).json()
    configured = client.patch(
        f"{base}/environments/{environment['id']}/service-config",
        json={"items": [{"service_key": service["service_key"], "base_url": CONTROLLED_TARGET_ALT_BASE_URL}]},
        headers={"If-Match": str(environment["rev"])},
    )
    assert configured.status_code == 200, configured.text
    run = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot(service_key=service["service_key"])},
        headers={**SERVICE_HEADERS, "Idempotency-Key": "corrupt-report"},
    )
    assert run.status_code == 202, run.text
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.runs SET snapshot=jsonb_set(snapshot, '{resolution,target_ref}', "
            "(snapshot#>'{resolution,target_ref}') - 'mapping_version_id') WHERE id=%s",
            (run.json()["id"],),
        )
        connection.commit()
        damaged_snapshot = connection.execute(
            "SELECT snapshot FROM app.runs WHERE id=%s", (run.json()["id"],)
        ).fetchone()[0]
    finally:
        connection.close()
    denied = client.get(f"{base}/runs/{run.json()['id']}/report")
    assert denied.status_code == 409
    assert denied.json()["code"] == "service_contract_required"
    report = client.get(
        f"{base}/runs/{run.json()['id']}/report", headers=SERVICE_HEADERS
    )
    assert report.status_code == 200, report.text
    assert report.json()["run"]["id"] == run.json()["id"]
    assert report.json()["resolution"] is None
    connection = migrator_connection()
    try:
        assert connection.execute(
            "SELECT snapshot FROM app.runs WHERE id=%s", (run.json()["id"],)
        ).fetchone()[0] == damaged_snapshot
    finally:
        connection.close()

    default_run = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot()},
        headers={**SERVICE_HEADERS, "Idempotency-Key": "corrupt-default-report"},
    )
    assert default_run.status_code == 202, default_run.text
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.runs SET snapshot=jsonb_set(snapshot, '{resolution,schema_version}', "
            "'[2]'::jsonb) WHERE id=%s",
            (default_run.json()["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    default_report = client.get(f"{base}/runs/{default_run.json()['id']}/report")
    assert default_report.status_code == 200, default_report.text
    assert default_report.json()["run"]["id"] == default_run.json()["id"]
    assert default_report.json()["resolution"] is None


def test_service_config_identity_checks_current_set_metadata(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="身份摘要")
    profile = _create_profile(client, base, environment["id"], name="唯一身份")
    unconfigured = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert unconfigured.json()["inheritance"]["identity"]["state"] == "none"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.credential_profiles SET status='available' WHERE id=%s",
            (profile["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    missing_set = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert missing_set.json()["inheritance"]["identity"]["state"] == "unavailable"
    secret = _create_secret(client, base, "摘要秘密", "summary-secret")
    activated = client.put(
        f"{base}/credentials/profiles/{profile['id']}/set",
        json={"slots": {DEMO_SLOT: secret["latest_version_id"]}},
    )
    assert activated.status_code == 200, activated.text
    ready = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert ready.json()["inheritance"]["identity"]["state"] == "ready"
    extra = _create_profile(client, base, environment["id"], name="未配置身份")
    still_ready = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert still_ready.json()["inheritance"]["identity"]["state"] == "ready"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.credential_sets SET status='unavailable' WHERE id=%s",
            (activated.json()["current_set_id"],),
        )
        connection.commit()
    finally:
        connection.close()
    disabled = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert disabled.json()["inheritance"]["identity"]["state"] == "unavailable"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.credential_sets SET status='active', "
            "expires_at=now() - interval '1 minute' WHERE id=%s",
            (activated.json()["current_set_id"],),
        )
        connection.commit()
    finally:
        connection.close()
    expired = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert expired.json()["inheritance"]["identity"]["state"] == "unavailable"
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.credential_sets SET status='active', expires_at=NULL WHERE id=%s",
            (activated.json()["current_set_id"],),
        )
        connection.execute(
            "UPDATE app.credential_profiles SET status='available' WHERE id=%s",
            (extra["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    ambiguous = client.get(f"{base}/environments/{environment['id']}/service-config")
    assert ambiguous.json()["inheritance"]["identity"]["state"] == "ambiguous"


def test_blank_service_name_is_a_client_error(
    client: TestClient, account: dict, project: dict
) -> None:
    response = client.post(f"{project_base(account, project)}/services", json={"name": "   "})
    assert response.status_code == 400
    assert response.json()["code"] == "service_invalid"
