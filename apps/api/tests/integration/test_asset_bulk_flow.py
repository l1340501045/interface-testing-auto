"""S3 普通用例当前页/全部命中冻结与逐项事务。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as OrmSession

from app.main import app
from app.models import Case
from harness import PASSWORD, migrator_connection, project_base, purge_user, seed_account

pytestmark = pytest.mark.integration


def _request(path: str = "/bulk") -> dict:
    return {
        "method": "GET", "path": path, "query_params": [], "headers": [],
        "body_type": "none", "body": "",
    }


def _case(client: TestClient, base: str, name: str) -> dict:
    response = client.post(
        f"{base}/cases",
        json={"name": name, "request": _request(), "assertions": []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _selection(client: TestClient, base: str, action: str, items: list[dict]) -> dict:
    response = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": action, "mode": "explicit",
        "items": [
            {"resource_type": "case", "id": item["id"], "expected_rev": item["rev"]}
            for item in items
        ],
        "parameters": {},
    })
    assert response.status_code == 201, response.text
    return response.json()


def _operate(client: TestClient, base: str, action: str, selection_id: str, key: str):
    return client.post(
        f"{base}/asset-operations",
        json={"schema_version": 1, "action": action, "selection_id": selection_id, "parameters": {}},
        headers={"Idempotency-Key": key},
    )


def test_explicit_batch_preserves_input_order_and_partially_succeeds(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    cases = [_case(client, base, f"批量-{index}") for index in range(3)]
    preview = _selection(client, base, "archive", [cases[2], cases[0], cases[1]])
    current = client.get(f"{base}/cases/{cases[0]['id']}")
    client.patch(
        f"{base}/cases/{cases[0]['id']}",
        json={"name": "预览后改名"},
        headers={"If-Match": current.headers["ETag"]},
    )
    result = _operate(
        client, base, "archive", preview["selection_id"], f"bulk:{uuid.uuid4()}"
    )
    assert result.status_code == 200, result.text
    body = result.json()["result"]
    assert [item["id"] for item in body["items"]] == [
        cases[2]["id"], cases[0]["id"], cases[1]["id"]
    ]
    assert [item["outcome"] for item in body["items"]] == [
        "succeeded", "conflict", "succeeded"
    ]
    assert body["counts"] == {
        "input": 3, "succeeded": 2, "no_change": 0, "conflict": 1, "failed": 0
    }


def test_explicit_move_reports_no_change_without_advancing_revision(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    folder = client.post(f"{base}/folders", json={"name": "批量移动目标"}).json()
    already = client.post(f"{base}/cases", json={
        "name": "已在目标", "folder_id": folder["id"],
        "request": _request(), "assertions": [],
    }).json()
    moving = _case(client, base, "需要移动")
    response = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "move", "mode": "explicit",
        "items": [
            {"resource_type": "case", "id": already["id"], "expected_rev": 1},
            {"resource_type": "case", "id": moving["id"], "expected_rev": 1},
        ],
        "parameters": {"target_folder_id": folder["id"]},
    }).json()
    result = client.post(
        f"{base}/asset-operations",
        json={
            "schema_version": 1, "action": "move",
            "selection_id": response["selection_id"],
            "parameters": {"target_folder_id": folder["id"]},
        },
        headers={"Idempotency-Key": f"move:{uuid.uuid4()}"},
    ).json()["result"]
    assert [item["outcome"] for item in result["items"]] == ["no_change", "succeeded"]
    assert result["counts"]["no_change"] == 1
    assert client.get(f"{base}/cases/{already['id']}").json()["rev"] == 1


def test_filter_selection_equals_case_library_and_keeps_action_inapplicable_item(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    archived = _case(client, base, "筛选-自身归档")
    preview = _selection(client, base, "archive", [archived])
    _operate(client, base, "archive", preview["selection_id"], f"archive:{uuid.uuid4()}")
    folder = client.post(f"{base}/folders", json={"name": "筛选-旧归档父"}).json()
    blocked = client.post(
        f"{base}/cases",
        json={
            "name": "筛选-祖先阻塞活动", "folder_id": folder["id"],
            "request": _request(), "assertions": [],
        },
    ).json()
    connection = migrator_connection()
    connection.execute(
        "UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (folder["id"],)
    )
    connection.commit()
    connection.close()
    filters = {
        "schema_version": 1, "q": "筛选-", "method": None,
        "state": "archived", "folder": "all", "folder_id": None,
        "include_descendants": False, "collection": "all", "sort": "name_asc",
    }
    library = client.get(
        f"{base}/case-library",
        params={"q": "筛选-", "state": "archived", "sort": "name_asc", "limit": 100},
    ).json()
    selection = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "restore", "mode": "filter",
        "filters": filters, "parameters": {},
    })
    assert selection.status_code == 201, selection.text
    body = selection.json()
    assert body["counts"]["selected"] == library["total"] == 2
    assert {item["id"] for item in [*body["preview_items"], *body["excluded_items"]]} == {
        archived["id"], blocked["id"]
    }
    blocked_item = next(item for item in body["excluded_items"] if item["id"] == blocked["id"])
    assert blocked_item["state"] == "active"
    late_id = uuid.uuid4()
    connection = migrator_connection()
    connection.execute(
        "INSERT INTO app.cases "
        "(id,workspace_id,project_id,name,request,assertions,rev,status) "
        "VALUES (%s,%s,%s,%s,%s,'[]'::jsonb,1,'archived')",
        (
            late_id, account["workspace_id"], project["id"],
            "筛选-预览后新增", Jsonb(_request("/late")),
        ),
    )
    connection.commit()
    connection.close()
    result = _operate(
        client, base, "restore", body["selection_id"], f"filter-freeze:{uuid.uuid4()}"
    ).json()["result"]
    assert {item["id"] for item in result["items"]} == {archived["id"], blocked["id"]}
    assert client.get(f"{base}/cases/{late_id}").json()["status"] == "archived"


def test_filter_total_limit_uses_all_selected_inputs(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    prefix = f"limit-{uuid.uuid4().hex[:8]}-"
    rows = [
        (
            uuid.uuid4(), account["workspace_id"], project["id"],
            f"{prefix}{index:03d}", Jsonb(_request(f"/bulk/{index}")),
        )
        for index in range(501)
    ]
    connection = migrator_connection()
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO app.cases "
            "(id,workspace_id,project_id,name,request,assertions,rev,status) "
            "VALUES (%s,%s,%s,%s,%s,'[]'::jsonb,1,'draft')",
            rows,
        )
    connection.commit()
    filters = {
        "schema_version": 1, "q": prefix, "method": None,
        "state": "active", "folder": "all", "folder_id": None,
        "include_descendants": False, "collection": "all", "sort": "name_asc",
    }
    too_many = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "archive", "mode": "filter",
        "filters": filters, "parameters": {},
    })
    assert too_many.status_code == 413
    explicit_too_many = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "archive", "mode": "explicit",
        "items": [
            {"resource_type": "case", "id": str(row[0]), "expected_rev": 1}
            for row in rows
        ],
        "parameters": {},
    })
    assert explicit_too_many.status_code == 413
    connection.execute("DELETE FROM app.cases WHERE id=%s", (rows[-1][0],))
    connection.commit()
    accepted = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "archive", "mode": "filter",
        "filters": filters, "parameters": {},
    })
    connection.close()
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["counts"]["selected"] == 500


def test_filter_selected_with_zero_eligible_has_stable_terminal_result(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    prefix = f"zero-filter-{uuid.uuid4().hex[:8]}"
    cases = [_case(client, base, f"{prefix}-{index}") for index in range(2)]
    filters = {
        "schema_version": 1, "q": prefix, "method": None,
        "state": "active", "folder": "all", "folder_id": None,
        "include_descendants": False, "collection": "all", "sort": "name_asc",
    }
    preview = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "restore", "mode": "filter",
        "filters": filters, "parameters": {},
    })
    assert preview.status_code == 201, preview.text
    assert preview.json()["counts"] == {
        "selected": 2, "eligible": 0, "excluded": 2, "cases": 2, "folders": 0
    }
    key = f"zero-filter:{uuid.uuid4()}"
    result = _operate(client, base, "restore", preview.json()["selection_id"], key)
    assert result.status_code == 200
    assert result.json()["result"]["counts"]["conflict"] == 2
    assert [item["id"] for item in result.json()["result"]["items"]] == [
        item["id"] for item in sorted(cases, key=lambda value: value["name"].casefold())
    ]
    assert _operate(client, base, "restore", preview.json()["selection_id"], key).json()["operation_id"] == result.json()["operation_id"]


def test_restore_without_common_target_reports_per_item_invalid_target(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    folder = client.post(f"{base}/folders", json={"name": "批量恢复原目录"}).json()
    cases = [
        client.post(f"{base}/cases", json={
            "name": "恢复-失效目录", "folder_id": folder["id"],
            "request": _request(), "assertions": [],
        }).json(),
        _case(client, base, "恢复-未分组"),
    ]
    archive = _selection(client, base, "archive", cases)
    _operate(client, base, "archive", archive["selection_id"], f"archive:{uuid.uuid4()}")
    archived_cases = [client.get(f"{base}/cases/{item['id']}").json() for item in cases]
    preview = _selection(client, base, "restore", archived_cases)
    connection = migrator_connection()
    connection.execute(
        "UPDATE app.folders SET archived_at=now(),rev=rev+1 WHERE id=%s", (folder["id"],)
    )
    connection.commit()
    connection.close()
    result = _operate(
        client, base, "restore", preview["selection_id"], f"restore:{uuid.uuid4()}"
    )
    assert [item["outcome"] for item in result.json()["result"]["items"]] == [
        "invalid_target", "succeeded"
    ]
    assert result.json()["result"]["counts"] == {
        "input": 2, "succeeded": 1, "no_change": 0, "conflict": 0, "failed": 1
    }


def test_unknown_error_rolls_back_all_batch_items_and_receipt(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    cases = [_case(client, base, f"回滚-{index}") for index in range(3)]
    preview = _selection(client, base, "archive", cases)
    key = f"rollback:{uuid.uuid4()}"
    flushes = 0

    def fail_second_case_flush(session: OrmSession, _context, _instances) -> None:
        nonlocal flushes
        if any(isinstance(item, Case) and item.status == "archived" for item in session.dirty):
            flushes += 1
            if flushes == 2:
                raise OperationalError("synthetic", {}, RuntimeError("synthetic batch failure"))

    event.listen(OrmSession, "before_flush", fail_second_case_flush)
    try:
        failed = _operate(client, base, "archive", preview["selection_id"], key)
    finally:
        event.remove(OrmSession, "before_flush", fail_second_case_flush)
    assert failed.status_code == 500
    assert client.get(f"{base}/asset-operations/by-key/{key}").status_code == 404
    assert all(client.get(f"{base}/cases/{item['id']}").json()["status"] == "draft" for item in cases)
    retried = _operate(client, base, "archive", preview["selection_id"], key)
    assert retried.status_code == 200
    assert retried.json()["result"]["counts"]["succeeded"] == 3


def test_filter_selection_uses_current_principal_personal_collection(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    case = _case(client, base, "个人收藏批量")
    favorite = client.put(
        f"{base}/case-preferences/{case['id']}/favorite", json={"favorite": True}
    )
    assert favorite.status_code == 200
    filters = {
        "schema_version": 1, "q": None, "method": None,
        "state": "active", "folder": "all", "folder_id": None,
        "include_descendants": False, "collection": "favorites", "sort": "name_asc",
    }
    admin_selection = client.post(f"{base}/asset-selections", json={
        "schema_version": 1, "action": "archive", "mode": "filter",
        "filters": filters, "parameters": {},
    })
    assert admin_selection.status_code == 201
    assert admin_selection.json()["counts"]["selected"] == 1

    connection = migrator_connection()
    username = f"bulk-editor-{uuid.uuid4().hex[:8]}"
    editor_id, _ = seed_account(
        connection, username=username, password=PASSWORD, workspace_role=None
    )
    connection.execute(
        "INSERT INTO app.workspace_memberships (workspace_id,user_id,role) VALUES (%s,%s,'editor')",
        (account["workspace_id"], editor_id),
    )
    connection.commit()
    try:
        with TestClient(app) as editor:
            login = editor.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200
            editor.headers["X-CSRF-Token"] = editor.cookies["interface_csrf"]
            own = editor.post(f"{base}/asset-selections", json={
                "schema_version": 1, "action": "archive", "mode": "filter",
                "filters": filters, "parameters": {},
            })
            assert own.status_code == 400
            assert own.json()["code"] == "selection_empty"
    finally:
        purge_user(connection, editor_id)
        connection.close()
