"""S2 资产生命周期、幂等回执与运行来源真实 PostgreSQL 集成。"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.api.deps import Principal, ProjectScope, apply_tenant
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import app
from app.services.run_coordinator import RunRejected, RunRequest, _request_hash, create_run
from harness import PASSWORD, create_environment, migrator_connection, project_base, publish_case

pytestmark = pytest.mark.integration


def _request(path: str = "/echo") -> dict:
    return {
        "method": "GET", "path": path, "query_params": [], "headers": [],
        "body_type": "none", "body": "",
    }


def _case(client: TestClient, base: str, name: str = "资产用例", folder_id=None) -> dict:
    response = client.post(
        f"{base}/cases",
        json={"name": name, "folder_id": folder_id, "request": _request(), "assertions": []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _select_case(client: TestClient, base: str, action: str, case: dict, parameters: dict) -> dict:
    response = client.post(
        f"{base}/asset-selections",
        json={
            "schema_version": 1, "action": action, "mode": "explicit",
            "items": [{"resource_type": "case", "id": case["id"], "expected_rev": case["rev"]}],
            "parameters": parameters,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _operate(client: TestClient, base: str, body: dict, key: str | None = None):
    return client.post(
        f"{base}/asset-operations", json=body,
        headers={"Idempotency-Key": key or f"op:{uuid.uuid4()}"},
    )


def test_copy_is_idempotent_and_has_no_versions_or_runs(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    source = _case(client, base)
    body = {"schema_version": 1, "action": "case_copy", "source_id": source["id"], "expected_rev": source["rev"]}
    key = f"copy:{uuid.uuid4()}"
    created = _operate(client, base, body, key)
    assert created.status_code == 201, created.text
    copied_id = created.json()["result"]["items"][0]["id"]
    replay = _operate(client, base, body, key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["operation_id"] == created.json()["operation_id"]
    assert _operate(client, base, {**body, "name": "不同副本"}, key).status_code == 409
    assert client.get(f"{base}/cases/{copied_id}/versions").json() == []
    assert all(item["id"] != copied_id for item in client.get(f"{base}/runs").json())
    assert client.delete(f"{base}/cases/{source['id']}").status_code == 409


def test_selection_missing_persists_rejected_and_move_archive_restore(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    case = _case(client, base)
    missing_body = {"schema_version": 1, "action": "archive", "selection_id": str(uuid.uuid4()), "parameters": {}}
    key = f"missing:{uuid.uuid4()}"
    rejected = _operate(client, base, missing_body, key)
    assert rejected.status_code == 200
    assert rejected.json()["result"]["code"] == "selection_missing"
    assert _operate(client, base, missing_body, key).json()["operation_id"] == rejected.json()["operation_id"]

    folder = client.post(f"{base}/folders", json={"name": "移动目标"}).json()
    selection = _select_case(client, base, "move", case, {"target_folder_id": folder["id"]})
    move_body = {"schema_version": 1, "action": "move", "selection_id": selection["selection_id"], "parameters": {"target_folder_id": folder["id"]}}
    move_key = f"move:{uuid.uuid4()}"
    moved = _operate(client, base, move_body, move_key)
    assert moved.status_code == 200, moved.text
    current = client.get(f"{base}/cases/{case['id']}").json()
    assert current["folder_id"] == folder["id"]
    connection = migrator_connection()
    connection.execute("DELETE FROM app.asset_selections WHERE id=%s", (selection["selection_id"],))
    connection.commit()
    connection.close()
    assert _operate(client, base, move_body, move_key).json()["operation_id"] == moved.json()["operation_id"]

    selection = _select_case(client, base, "archive", current, {})
    archived = _operate(client, base, {"schema_version": 1, "action": "archive", "selection_id": selection["selection_id"], "parameters": {}})
    assert archived.json()["result"]["result_kind"] == "completed"
    current = client.get(f"{base}/cases/{case['id']}").json()
    assert current["status"] == "archived"

    selection = _select_case(client, base, "restore", current, {"target_folder_id": None})
    restored = _operate(client, base, {"schema_version": 1, "action": "restore", "selection_id": selection["selection_id"], "parameters": {"target_folder_id": None}})
    assert restored.json()["result"]["result_kind"] == "completed"
    current = client.get(f"{base}/cases/{case['id']}").json()
    assert current["status"] == "draft" and current["folder_id"] is None


def test_folder_archive_and_restore_only_actual_batch_members(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    a = client.post(f"{base}/folders", json={"name": "A"}).json()
    b = client.post(f"{base}/folders", json={"name": "B", "parent_id": a["id"]}).json()
    c = client.post(f"{base}/folders", json={"name": "C", "parent_id": b["id"]}).json()
    case = _case(client, base, folder_id=c["id"])
    connection = migrator_connection()
    connection.execute("UPDATE app.folders SET archived_at=now(), rev=rev+1 WHERE id=%s", (b["id"],))
    connection.commit()
    connection.close()

    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": a["id"], "expected_rev": a["rev"]}, "parameters": {},
    })
    assert preview.status_code == 201, preview.text
    operation = _operate(client, base, {"schema_version": 1, "action": "folder_archive", "selection_id": preview.json()["selection_id"], "parameters": {}})
    assert operation.status_code == 200, operation.text
    assert operation.json()["result"]["root"]["id"] == a["id"]
    member_ids = {item["id"] for item in operation.json()["result"]["members"]}
    assert member_ids == {a["id"], c["id"], case["id"]}

    current_a = next(item for item in client.get(f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}).json()["items"] if item["id"] == a["id"])
    restore_preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": a["id"], "expected_rev": current_a["rev"]}, "parameters": {},
    })
    assert restore_preview.status_code == 409, restore_preview.text
    assert restore_preview.json()["code"] == "folder_unavailable"
    archived_ids = {item["id"] for item in client.get(f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}).json()["items"]}
    assert {a["id"], b["id"], c["id"]}.issubset(archived_ids)
    assert client.get(f"{base}/cases/{case['id']}").json()["status"] == "archived"

    # 没有旧归档依赖的正常树可完整恢复，回执 members 与实际成功集合一致。
    x = client.post(f"{base}/folders", json={"name": "X"}).json()
    y = client.post(f"{base}/folders", json={"name": "Y", "parent_id": x["id"]}).json()
    x_case = _case(client, base, name="正常恢复用例", folder_id=y["id"])
    x_preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": x["id"], "expected_rev": x["rev"]}, "parameters": {},
    }).json()
    x_archive = _operate(client, base, {"schema_version": 1, "action": "folder_archive", "selection_id": x_preview["selection_id"], "parameters": {}}).json()
    archived_x = next(item for item in client.get(f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}).json()["items"] if item["id"] == x["id"])
    x_restore_preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": x["id"], "expected_rev": archived_x["rev"]}, "parameters": {},
    }).json()
    x_restored = _operate(client, base, {"schema_version": 1, "action": "folder_restore", "selection_id": x_restore_preview["selection_id"], "parameters": {}}).json()
    assert x_archive["result"]["result_kind"] == "completed"
    assert x_restored["result"]["result_kind"] == "completed"
    assert {item["id"] for item in x_restored["result"]["members"]} == {x["id"], y["id"], x_case["id"]}


def test_run_source_blocks_new_after_archive_but_replays_old_run(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project)
    case = _case(client, base)
    version = publish_case(client, account, project, case["id"])
    active_debug = client.post(f"{base}/runs", json={
        "environment_id": environment["id"], "debug_snapshot": {"request": _request(), "assertions": []},
        "source_case_id": case["id"],
    })
    assert active_debug.status_code == 202, active_debug.text
    assert active_debug.json()["debug_source_case_id"] == case["id"]
    run_body = {"environment_id": environment["id"], "case_version_id": version["id"]}
    key = f"run:{uuid.uuid4()}"
    accepted = client.post(f"{base}/runs", json=run_body, headers={"Idempotency-Key": key})
    assert accepted.status_code == 202, accepted.text

    selection = _select_case(client, base, "archive", case, {})
    assert _operate(client, base, {"schema_version": 1, "action": "archive", "selection_id": selection["selection_id"], "parameters": {}}).status_code == 200
    replay = client.post(f"{base}/runs", json=run_body, headers={"Idempotency-Key": key})
    assert replay.status_code == 202 and replay.json()["id"] == accepted.json()["id"]
    blocked = client.post(f"{base}/runs", json=run_body, headers={"Idempotency-Key": f"run:{uuid.uuid4()}"})
    assert blocked.status_code == 400 and blocked.json()["code"] == "case_archived"

    debug = client.post(f"{base}/runs", json={
        "environment_id": environment["id"], "debug_snapshot": {"request": _request(), "assertions": []},
        "source_case_id": case["id"],
    })
    assert debug.status_code == 400 and debug.json()["code"] == "case_archived"


def test_folder_selection_rejects_structure_change_and_folder_patch_requires_revision(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    root = client.post(f"{base}/folders", json={"name": "冻结根"}).json()
    missing = client.patch(f"{base}/folders/{root['id']}", json={"name": "无修订"})
    assert missing.status_code == 428
    stale = client.patch(
        f"{base}/folders/{root['id']}", json={"name": "旧修订"}, headers={"If-Match": '"99"'}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"

    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": root["rev"]}, "parameters": {},
    }).json()
    client.post(f"{base}/folders", json={"name": "迟到后代", "parent_id": root["id"]})
    result = _operate(client, base, {
        "schema_version": 1, "action": "folder_archive",
        "selection_id": preview["selection_id"], "parameters": {},
    })
    assert result.status_code == 200
    assert result.json()["result"]["result_kind"] == "rejected"
    assert result.json()["result"]["code"] == "selection_changed"


def test_same_operation_key_converges_under_concurrency(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    source = _case(client, base)
    body = {"schema_version": 1, "action": "case_copy", "source_id": source["id"], "expected_rev": 1}
    key = f"concurrent:{uuid.uuid4()}"
    gate = Barrier(2)

    def submit() -> tuple[int, str]:
        with TestClient(app) as contender:
            login = contender.post("/api/v1/auth/login", json={"username": account["username"], "password": PASSWORD})
            assert login.status_code == 200
            contender.headers["X-CSRF-Token"] = contender.cookies["interface_csrf"]
            gate.wait()
            response = contender.post(
                f"{base}/asset-operations", json=body, headers={"Idempotency-Key": key}
            )
            return response.status_code, response.json()["operation_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: submit(), range(2)))
    assert sorted(status for status, _operation in results) == [200, 201]
    assert len({operation for _status, operation in results}) == 1
    copies = [item for item in client.get(f"{base}/cases").json() if item["name"].endswith("副本")]
    assert len(copies) == 1


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [("disabled_user", 401), ("revoked_session", 401), ("revoked_role", 403)],
)
def test_project_gate_rechecks_identity_and_role_before_asset_write(
    client: TestClient, account: dict, project: dict, mutation: str, expected_status: int
) -> None:
    base = project_base(account, project)
    source = _case(client, base)
    writer = migrator_connection()
    changed = False

    def after_lock(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        nonlocal changed
        if changed or "FOR NO KEY UPDATE" not in statement or "app.projects" not in statement:
            return
        if mutation == "disabled_user":
            writer.execute("UPDATE app.users SET status='disabled' WHERE id=%s", (account["user_id"],))
        elif mutation == "revoked_session":
            writer.execute("UPDATE app.sessions SET revoked_at=now() WHERE user_id=%s", (account["user_id"],))
        else:
            writer.execute(
                "UPDATE app.workspace_memberships SET role='viewer' WHERE workspace_id=%s AND user_id=%s",
                (account["workspace_id"], account["user_id"]),
            )
        writer.commit()
        changed = True

    event.listen(get_engine(), "after_cursor_execute", after_lock)
    try:
        response = _operate(client, base, {
            "schema_version": 1, "action": "case_copy", "source_id": source["id"], "expected_rev": 1,
        })
    finally:
        event.remove(get_engine(), "after_cursor_execute", after_lock)
        copy_count = writer.execute(
            "SELECT count(*) FROM app.cases WHERE project_id=%s AND name LIKE %s",
            (project["id"], "%副本"),
        ).fetchone()[0]
        writer.close()
    assert changed is True
    assert response.status_code == expected_status
    assert copy_count == 0


def test_run_idempotency_missing_result_never_creates_replacement(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project)
    case = _case(client, base)
    version = publish_case(client, account, project, case["id"])
    key = f"missing-run:{uuid.uuid4()}"
    request = RunRequest(
        environment_id=uuid.UUID(environment["id"]),
        case_version_id=uuid.UUID(version["id"]),
        idempotency_key=key,
    )
    connection = migrator_connection()
    connection.execute(
        "INSERT INTO app.idempotency_records "
        "(workspace_id,project_id,principal_id,action,idempotency_key,request_hash,result_ref,expires_at) "
        "VALUES (%s,%s,%s,'run:create',%s,%s,NULL,now()+interval '1 day')",
        (account["workspace_id"], project["id"], account["user_id"], key, _request_hash(request)),
    )
    connection.commit()
    before = connection.execute(
        "SELECT count(*) FROM app.runs WHERE project_id=%s", (project["id"],)
    ).fetchone()[0]
    response = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "case_version_id": version["id"]},
        headers={"Idempotency-Key": key},
    )
    after = connection.execute(
        "SELECT count(*) FROM app.runs WHERE project_id=%s", (project["id"],)
    ).fetchone()[0]
    connection.close()
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_result_unavailable"
    assert before == after == 0


def test_cross_project_concurrent_run_key_never_returns_other_project_result(
    client: TestClient, account: dict, project: dict
) -> None:
    other = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects",
        json={"key": f"run{uuid.uuid4().hex[:8]}", "name": "并发运行项目"},
    ).json()
    project_rows = []
    for target in (project, other):
        base = project_base(account, target)
        environment = create_environment(client, account, target)
        case = _case(client, base, name=f"运行-{target['id']}")
        version = publish_case(client, account, target, case["id"])
        project_rows.append((base, environment["id"], version["id"], target["id"]))
    key = f"cross-project:{uuid.uuid4()}"
    gate = Barrier(2)

    def submit(row) -> tuple[int, str | None]:
        base, environment_id, version_id, _project_id = row
        with TestClient(app) as contender:
            login = contender.post("/api/v1/auth/login", json={"username": account["username"], "password": PASSWORD})
            assert login.status_code == 200
            contender.headers["X-CSRF-Token"] = contender.cookies["interface_csrf"]
            gate.wait()
            response = contender.post(
                f"{base}/runs",
                json={"environment_id": environment_id, "case_version_id": version_id},
                headers={"Idempotency-Key": key},
            )
            body = response.json()
            return response.status_code, body.get("id")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, project_rows))
    assert sorted(status for status, _run_id in results) == [202, 400]
    accepted = next(run_id for status, run_id in results if status == 202)
    connection = migrator_connection()
    rows = connection.execute(
        "SELECT id, project_id FROM app.runs WHERE project_id=ANY(%s::uuid[])",
        ([row[3] for row in project_rows],),
    ).fetchall()
    connection.close()
    assert len(rows) == 1 and str(rows[0][0]) == accepted


def test_restore_rejects_active_root_and_target_that_becomes_archived(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    root = client.post(f"{base}/folders", json={"name": "恢复根"}).json()
    child = client.post(f"{base}/folders", json={"name": "恢复子", "parent_id": root["id"]}).json()
    active = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": 1},
        "parameters": {"target_parent_id": child["id"]},
    })
    assert active.status_code == 409 and active.json()["code"] == "asset_state_conflict"

    connection = migrator_connection()
    connection.execute("UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (root["id"],))
    connection.commit()
    connection.close()
    target = client.post(f"{base}/folders", json={"name": "恢复目标"}).json()
    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": 2},
        "parameters": {"target_parent_id": target["id"]},
    }).json()
    connection = migrator_connection()
    connection.execute("UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (target["id"],))
    connection.commit()
    connection.close()
    key = f"restore-target:{uuid.uuid4()}"
    body = {"schema_version": 1, "action": "folder_restore", "selection_id": preview["selection_id"], "parameters": {"target_parent_id": target["id"]}}
    rejected = _operate(client, base, body, key)
    assert rejected.status_code == 200
    assert rejected.json()["result"]["result_kind"] == "rejected"
    assert client.get(f"{base}/asset-operations/by-key/{key}").json()["operation_id"] == rejected.json()["operation_id"]


def test_excluded_member_change_and_blocked_source_are_rejected_without_leak(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    root = client.post(f"{base}/folders", json={"name": "冻结排除根"}).json()
    case = _case(client, base, folder_id=root["id"])
    case_selection = _select_case(client, base, "archive", case, {})
    _operate(client, base, {"schema_version": 1, "action": "archive", "selection_id": case_selection["selection_id"], "parameters": {}})
    root_preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": 1}, "parameters": {},
    }).json()
    archived_case = client.get(f"{base}/cases/{case['id']}").json()
    restore = _select_case(client, base, "restore", archived_case, {"target_folder_id": root["id"]})
    _operate(client, base, {"schema_version": 1, "action": "restore", "selection_id": restore["selection_id"], "parameters": {"target_folder_id": root["id"]}})
    stale = _operate(client, base, {"schema_version": 1, "action": "folder_archive", "selection_id": root_preview["selection_id"], "parameters": {}})
    assert stale.json()["result"]["code"] == "selection_changed"
    assert client.get(f"{base}/folders").status_code == 200

    connection = migrator_connection()
    connection.execute("UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (root["id"],))
    connection.commit()
    connection.close()
    current = client.get(f"{base}/cases/{case['id']}")
    patch = client.patch(
        f"{base}/cases/{case['id']}", json={"name": "不应修改"},
        headers={"If-Match": current.headers["ETag"]},
    )
    assert patch.status_code == 409
    copy_key = f"blocked-copy:{uuid.uuid4()}"
    copy_body = {"schema_version": 1, "action": "case_copy", "source_id": case["id"], "expected_rev": current.json()["rev"], "folder_id": None}
    copy_result = _operate(client, base, copy_body, copy_key)
    assert copy_result.status_code == 200 and copy_result.json()["result"]["result_kind"] == "rejected"
    assert client.get(f"{base}/asset-operations/by-key/{copy_key}").status_code == 200
    move = _select_case(client, base, "move", current.json(), {"target_folder_id": None})
    moved = _operate(client, base, {"schema_version": 1, "action": "move", "selection_id": move["selection_id"], "parameters": {"target_folder_id": None}})
    assert moved.json()["result"]["result_kind"] == "completed"


def test_cross_project_inaccessible_result_never_contains_asset_metadata(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    other = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects",
        json={"key": f"leak{uuid.uuid4().hex[:8]}", "name": "隔离项目"},
    ).json()
    foreign = _case(client, project_base(account, other), name="不可泄露名称")
    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "archive", "mode": "explicit",
        "items": [{"resource_type": "case", "id": foreign["id"], "expected_rev": 1}], "parameters": {},
    }).json()
    assert preview["excluded_items"][0]["name"] is None
    key = f"leak:{uuid.uuid4()}"
    result = _operate(client, base, {"schema_version": 1, "action": "archive", "selection_id": preview["selection_id"], "parameters": {}}, key).json()
    assert result["result"]["items"][0]["asset"] is None
    assert "不可泄露名称" not in str(result)
    assert "不可泄露名称" not in client.get(f"{base}/asset-operations/by-key/{key}").text


def test_names_are_strict_and_expired_selection_cleanup_is_bounded(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    case = _case(client, base)
    for bad_name in ("   ", None, 123, {"x": 1}):
        body = {"schema_version": 1, "action": "case_copy", "source_id": case["id"], "expected_rev": 1, "name": bad_name}
        assert _operate(client, base, body).status_code == 422
    folder = client.post(f"{base}/folders", json={"name": "旧恢复"}).json()
    connection = migrator_connection()
    connection.execute("UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (folder["id"],))
    connection.commit()
    connection.close()
    for bad_name in (None, 123, {"x": 1}, "   "):
        response = client.post(f"{base}/asset-selections", json={
            "schema_version": 1, "action": "folder_restore", "mode": "folder",
            "root": {"resource_type": "folder", "id": folder["id"], "expected_rev": 2},
            "parameters": {"root_name": bad_name},
        })
        assert response.status_code == 422

    selection = _select_case(client, base, "archive", case, {})
    connection = migrator_connection()
    connection.execute("UPDATE app.asset_selections SET expires_at=now()-interval '1 day' WHERE id=%s", (selection["selection_id"],))
    connection.commit()
    connection.close()
    _select_case(client, base, "archive", case, {})
    connection = migrator_connection()
    assert connection.execute("SELECT 1 FROM app.asset_selections WHERE id=%s", (selection["selection_id"],)).fetchone() is None
    connection.close()


def test_project_gate_refreshes_preloaded_case_before_run_admission(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project)
    case = _case(client, base)
    version = publish_case(client, account, project, case["id"])
    connection = migrator_connection()
    login_id = connection.execute(
        "SELECT id FROM app.sessions WHERE user_id=%s AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (account["user_id"],),
    ).fetchone()[0]
    connection.close()
    session = get_session_factory()()
    try:
        apply_tenant(session, account["workspace_id"], account["user_id"])
        # 门闩前故意把 draft Case 放进 identity map。
        from app.models import Case

        cached = session.get(Case, uuid.UUID(case["id"]))
        assert cached is not None and cached.status == "draft"
        selection = _select_case(client, base, "archive", case, {})
        _operate(client, base, {
            "schema_version": 1, "action": "archive",
            "selection_id": selection["selection_id"], "parameters": {},
        })
        scope = ProjectScope(
            workspace_id=account["workspace_id"],
            project_id=uuid.UUID(project["id"]),
            role="admin",
            principal=Principal(
                user_id=account["user_id"], username=account["username"],
                display_name=account["username"], is_admin=False, session_id=login_id,
            ),
        )
        with pytest.raises(RunRejected) as rejected:
            create_run(
                session, get_settings(), scope=scope,
                payload=RunRequest(
                    environment_id=uuid.UUID(environment["id"]),
                    case_version_id=uuid.UUID(version["id"]),
                ),
            )
        assert rejected.value.code == "case_archived"
    finally:
        session.close()


@pytest.mark.parametrize("target_kind", ["root", "new_parent"])
def test_folder_restore_explicit_target_replaces_archived_old_parent(
    client: TestClient, account: dict, project: dict, target_kind: str
) -> None:
    base = project_base(account, project)
    parent = client.post(f"{base}/folders", json={"name": f"旧父-{target_kind}"}).json()
    root = client.post(
        f"{base}/folders", json={"name": f"恢复根-{target_kind}", "parent_id": parent["id"]}
    ).json()
    case = _case(client, base, name=f"恢复用例-{target_kind}", folder_id=root["id"])
    archive_preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": 1},
        "parameters": {},
    }).json()
    _operate(client, base, {
        "schema_version": 1, "action": "folder_archive",
        "selection_id": archive_preview["selection_id"], "parameters": {},
    })
    connection = migrator_connection()
    connection.execute(
        "UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (parent["id"],)
    )
    connection.commit()
    connection.close()
    archived_root = next(
        item for item in client.get(
            f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
        ).json()["items"] if item["id"] == root["id"]
    )
    omitted = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": archived_root["rev"]},
        "parameters": {},
    })
    assert omitted.status_code == 409 and omitted.json()["code"] == "folder_unavailable"
    parameters = {"target_parent_id": None}
    if target_kind == "new_parent":
        target = client.post(f"{base}/folders", json={"name": "新父"}).json()
        parameters = {"target_parent_id": target["id"]}
    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": archived_root["rev"]},
        "parameters": parameters,
    })
    assert preview.status_code == 201, preview.text
    restored = _operate(client, base, {
        "schema_version": 1, "action": "folder_restore",
        "selection_id": preview.json()["selection_id"], "parameters": parameters,
    })
    assert restored.json()["result"]["result_kind"] == "completed"
    assert client.get(f"{base}/cases/{case['id']}").json()["status"] == "draft"
    assert parent["id"] in {
        item["id"] for item in client.get(
            f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
        ).json()["items"]
    }


def test_zero_eligible_restore_has_stable_terminal_result(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    active = _case(client, base, name="活动恢复")
    inputs = [
        {"id": active["id"], "expected_rev": active["rev"]},
        {"id": str(uuid.uuid4()), "expected_rev": 1},
    ]
    for item in inputs:
        preview = client.post(f"{base}/asset-selections", json={
            "schema_version": 1, "action": "restore", "mode": "explicit",
            "items": [{"resource_type": "case", **item}], "parameters": {},
        }).json()
        assert preview["counts"]["eligible"] == 0
        key = f"zero:{uuid.uuid4()}"
        result = _operate(client, base, {
            "schema_version": 1, "action": "restore",
            "selection_id": preview["selection_id"], "parameters": {},
        }, key)
        assert result.status_code == 200
        body = result.json()["result"]
        assert body["counts"]["input"] == 1
        assert sum(body["counts"][key] for key in ("succeeded", "no_change", "conflict", "failed")) == 1
        if item["id"] != active["id"]:
            assert body["items"][0]["asset"] is None
        assert _operate(client, base, {
            "schema_version": 1, "action": "restore",
            "selection_id": preview["selection_id"], "parameters": {},
        }, key).json()["operation_id"] == result.json()["operation_id"]


def test_restore_source_parser_distinguishes_locate_root_and_invalid_fact(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    root = client.post(f"{base}/folders", json={"name": "可信根"}).json()
    child = client.post(f"{base}/folders", json={"name": "可信子", "parent_id": root["id"]}).json()
    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_archive", "mode": "folder",
        "root": {"resource_type": "folder", "id": root["id"], "expected_rev": 1},
        "parameters": {},
    }).json()
    archive = _operate(client, base, {
        "schema_version": 1, "action": "folder_archive",
        "selection_id": preview["selection_id"], "parameters": {},
    }).json()
    archived_child = next(
        item for item in client.get(
            f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
        ).json()["items"] if item["id"] == child["id"]
    )
    locate = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": child["id"], "expected_rev": archived_child["rev"]},
        "parameters": {},
    })
    assert locate.status_code == 409 and locate.json()["code"] == "archive_root_required"
    assert locate.json()["archive_root_id"] == root["id"]

    connection = migrator_connection()
    operation_id = archive["operation_id"]
    missing_member = connection.execute(
        "DELETE FROM app.asset_archive_members WHERE operation_id=%s AND folder_id=%s "
        "RETURNING id,workspace_id,project_id,operation_id,folder_id,case_id,before_rev,"
        "archived_rev,original_parent_id,before_state,created_at",
        (operation_id, child["id"]),
    ).fetchone()
    connection.commit()
    missing_view = next(
        item for item in client.get(
            f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
        ).json()["items"] if item["id"] == child["id"]
    )
    assert missing_view["restore_mode"] == "unavailable"
    assert missing_view["archive_root_id"] is None
    missing = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": child["id"], "expected_rev": archived_child["rev"]},
        "parameters": {},
    })
    assert missing.status_code == 409
    assert missing.json()["code"] == "archive_source_unavailable"
    assert "archive_root_id" not in missing.json()
    connection.execute(
        "INSERT INTO app.asset_archive_members "
        "(id,workspace_id,project_id,operation_id,folder_id,case_id,before_rev,archived_rev,"
        "original_parent_id,before_state,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        missing_member,
    )
    connection.execute(
        "UPDATE app.asset_operations SET result=jsonb_set(result,'{root,resource_type}','\"case\"') WHERE id=%s",
        (operation_id,),
    )
    connection.commit()
    connection.close()
    folder_view = next(
        item for item in client.get(
            f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
        ).json()["items"] if item["id"] == child["id"]
    )
    assert folder_view["restore_mode"] == "unavailable"
    invalid = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "folder_restore", "mode": "folder",
        "root": {"resource_type": "folder", "id": child["id"], "expected_rev": archived_child["rev"]},
        "parameters": {},
    })
    assert invalid.status_code == 409 and invalid.json()["code"] == "archive_source_unavailable"
