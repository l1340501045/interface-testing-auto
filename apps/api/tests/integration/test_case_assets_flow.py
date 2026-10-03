"""S1 用例资产查询、目录发现与个人偏好真实 PostgreSQL 集成。"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import apply_tenant
from app.db import get_engine, get_session_factory
from app.models import AssetSelection
from harness import PASSWORD, migrator_connection, purge_user, seed_account
from harness import project_base as _base

pytestmark = pytest.mark.integration


def _request(path: str, method: str = "GET") -> dict:
    return {
        "method": method,
        "path": path,
        "query_params": [],
        "headers": [],
        "body_type": "none",
        "body": "",
    }


def _create_case(client: TestClient, base: str, name: str, path: str, **extra) -> dict:
    response = client.post(
        f"{base}/cases",
        json={"name": name, "request": _request(path), "assertions": [], **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_case_library_uses_effective_state_and_real_bound_cursor(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    root = client.post(f"{base}/folders", json={"name": "归档根"}).json()
    child = client.post(
        f"{base}/folders", json={"name": "活动子目录", "parent_id": root["id"]}
    ).json()
    legacy = _create_case(
        client, base, "祖先归档下仍为活动的旧用例", "/legacy", folder_id=child["id"]
    )
    for index in range(21):
        _create_case(client, base, f"分页同组 {index:02d}", f"/paged/{index}")

    archived = client.delete(f"{base}/folders/{root['id']}")
    assert archived.status_code == 204, archived.text

    active = client.get(f"{base}/case-library", params={"q": "分页同组", "limit": 20})
    assert active.status_code == 200, active.text
    body = active.json()
    assert body["total"] == 21
    assert len(body["items"]) == 20
    assert body["next_cursor"]
    second = client.get(
        f"{base}/case-library",
        params={"q": "分页同组", "limit": 20, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 21
    assert len(second.json()["items"]) == 1
    assert {item["id"] for item in body["items"]}.isdisjoint(
        {item["id"] for item in second.json()["items"]}
    )

    # 游标绑定筛选条件，不能换 q 后继续翻页；篡改签名同样明确拒绝。
    rebound = client.get(
        f"{base}/case-library",
        params={"q": "别的条件", "limit": 20, "cursor": body["next_cursor"]},
    )
    assert rebound.status_code == 400
    assert rebound.json()["code"] == "invalid_cursor"
    tampered = body["next_cursor"][:-1] + ("A" if body["next_cursor"][-1] != "A" else "B")
    rejected = client.get(
        f"{base}/case-library",
        params={"q": "分页同组", "limit": 20, "cursor": tampered},
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_cursor"

    default_ids = {item["id"] for item in client.get(f"{base}/case-library").json()["items"]}
    assert legacy["id"] not in default_ids, "active 必须排除被归档祖先阻塞的旧活动用例"
    archived_cases = client.get(
        f"{base}/case-library", params={"state": "archived", "limit": 100}
    )
    assert archived_cases.status_code == 200, archived_cases.text
    legacy_item = next(item for item in archived_cases.json()["items"] if item["id"] == legacy["id"])
    assert legacy_item["asset_status"] == "active"
    assert legacy_item["availability"] == "folder_unavailable"
    assert "request" not in legacy_item and "assertions" not in legacy_item


def test_asset_folders_find_empty_archive_and_archived_child_under_active_parent(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    active_parent = client.post(f"{base}/folders", json={"name": "活动父目录"}).json()
    archived_child = client.post(
        f"{base}/folders", json={"name": "空归档子目录", "parent_id": active_parent["id"]}
    ).json()
    assert client.delete(f"{base}/folders/{archived_child['id']}").status_code == 204

    response = client.get(
        f"{base}/asset-folders",
        params={"parent_mode": "all", "state": "archived", "q": "空归档"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == archived_child["id"]
    assert item["rev"] == archived_child["rev"] + 1
    assert item["availability"] == "archived"
    assert item["archive_operation_id"] is None
    assert item["archive_root_id"] is None
    assert item["restore_mode"] == "legacy_single"
    assert item["has_children"] is False
    assert item["ancestor_path"] == [
        {"id": active_parent["id"], "name": "活动父目录", "archived": False}
    ]

    old_shape = client.get(f"{base}/folders")
    assert old_shape.status_code == 200
    assert isinstance(old_shape.json(), list)
    parent = next(folder for folder in old_shape.json() if folder["id"] == active_parent["id"])
    assert parent["rev"] == 1
    assert parent["availability"] == "available"


def test_preferences_are_personal_viewer_writable_and_recent_is_capped(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    cases = [_create_case(client, base, f"最近 {index:02d}", f"/recent/{index}") for index in range(51)]
    favorite = client.put(
        f"{base}/case-preferences/{cases[0]['id']}/favorite", json={"favorite": True}
    )
    assert favorite.status_code == 200, favorite.text
    for item in cases:
        opened = client.post(f"{base}/case-preferences/{item['id']}/opened")
        assert opened.status_code == 200, opened.text
    recent = client.get(
        f"{base}/case-library",
        params={"collection": "recent", "sort": "recent_desc", "limit": 100},
    )
    assert recent.status_code == 200, recent.text
    assert recent.json()["total"] == 50
    # 被最近裁剪不删除收藏。
    favorites = client.get(
        f"{base}/case-library", params={"collection": "favorites", "limit": 100}
    )
    assert [item["id"] for item in favorites.json()["items"]] == [cases[0]["id"]]

    connection = migrator_connection()
    viewer_name = f"asset-viewer-{uuid.uuid4().hex[:8]}"
    viewer_id, _ = seed_account(
        connection, username=viewer_name, password=PASSWORD, workspace_role=None
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspace_memberships (workspace_id, user_id, role) VALUES (%s, %s, 'viewer')",
            (account["workspace_id"], viewer_id),
        )
    connection.commit()
    try:
        from app.main import app

        with TestClient(app) as viewer:
            login = viewer.post(
                "/api/v1/auth/login", json={"username": viewer_name, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            viewer.headers["X-CSRF-Token"] = viewer.cookies["interface_csrf"]
            viewer_favorites = viewer.get(
                f"{base}/case-library", params={"collection": "favorites"}
            )
            assert viewer_favorites.status_code == 200
            assert viewer_favorites.json()["total"] == 0
            own = viewer.put(
                f"{base}/case-preferences/{cases[1]['id']}/favorite",
                json={"favorite": True},
            )
            assert own.status_code == 200, own.text
            own_view = viewer.post(
                f"{base}/case-views",
                json={"name": "查看者自己的视图", "filters": {"schema_version": 1}},
            )
            assert own_view.status_code == 201, own_view.text
            # 查看者可写个人偏好，但不能改共享用例。
            denied = viewer.patch(
                f"{base}/cases/{cases[1]['id']}",
                json={"name": "越权改名"}, headers={"If-Match": '"1"'},
            )
            assert denied.status_code == 403
        admin_favorites = client.get(
            f"{base}/case-library", params={"collection": "favorites", "limit": 100}
        )
        assert [item["id"] for item in admin_favorites.json()["items"]] == [cases[0]["id"]]
    finally:
        purge_user(connection, viewer_id)
        connection.close()


def test_saved_views_enforce_revision_owner_and_limit(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    created = client.post(
        f"{base}/case-views",
        json={
            "name": "  我的活动视图  ",
            "filters": {"schema_version": 1, "state": "active", "sort": "name_asc"},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "我的活动视图"
    assert created.headers["ETag"] == '"1"'
    stale = client.patch(
        f"{base}/case-views/{created.json()['id']}",
        json={"name": "旧表单覆盖"}, headers={"If-Match": '"9"'},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "precondition_failed"
    updated = client.patch(
        f"{base}/case-views/{created.json()['id']}",
        json={"name": "新名称"}, headers={"If-Match": '"1"'},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rev"] == 2
    assert updated.headers["ETag"] == '"2"'

    for index in range(19):
        response = client.post(
            f"{base}/case-views",
            json={"name": f"视图 {index}", "filters": {"schema_version": 1}},
        )
        assert response.status_code == 201, response.text
    over_limit = client.post(
        f"{base}/case-views",
        json={"name": "第二十一个", "filters": {"schema_version": 1}},
    )
    assert over_limit.status_code == 409
    assert over_limit.json()["code"] == "saved_view_limit_reached"
    assert len(client.get(f"{base}/case-views").json()) == 20

    unknown_delete = client.delete(
        f"{base}/case-views/{uuid.uuid4()}", headers={"If-Match": '"1"'}
    )
    assert unknown_delete.status_code == 204
    deleted = client.delete(
        f"{base}/case-views/{created.json()['id']}", headers={"If-Match": '"2"'}
    )
    assert deleted.status_code == 204


def test_saved_view_limit_remains_twenty_under_concurrent_creates(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    for index in range(19):
        response = client.post(
            f"{base}/case-views",
            json={"name": f"并发前视图 {index}", "filters": {"schema_version": 1}},
        )
        assert response.status_code == 201, response.text

    from app.main import app

    gate = Barrier(2)

    def create_last(name: str) -> int:
        with TestClient(app) as contender:
            login = contender.post(
                "/api/v1/auth/login",
                json={"username": account["username"], "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            contender.headers["X-CSRF-Token"] = contender.cookies["interface_csrf"]
            gate.wait()
            return contender.post(
                f"{base}/case-views",
                json={"name": name, "filters": {"schema_version": 1}},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(create_last, ["并发候选 A", "并发候选 B"]))
    assert statuses == [201, 409]
    assert len(client.get(f"{base}/case-views").json()) == 20


def test_recursive_folder_reads_are_bounded_and_old_patch_rejects_cycles(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    folder_a = client.post(f"{base}/folders", json={"name": "环 A"}).json()
    folder_b = client.post(
        f"{base}/folders", json={"name": "环 B", "parent_id": folder_a["id"]}
    ).json()
    case = _create_case(client, base, "环内用例", "/cycle", folder_id=folder_b["id"])

    rejected = client.patch(
        f"{base}/folders/{folder_a['id']}",
        json={"parent_id": folder_b["id"]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "folder_cycle"

    # 模拟迁移后仍可能由旧管理脚本留下的坏关系，并让环中一个节点处于归档态。
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.folders SET parent_id = %s, archived_at = now() WHERE id = %s",
                (folder_b["id"], folder_a["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    cases = client.get(f"{base}/case-library", params={"state": "archived", "limit": 100})
    assert cases.status_code == 200, cases.text
    item = next(item for item in cases.json()["items"] if item["id"] == case["id"])
    assert item["availability"] == "folder_unavailable"

    folders = client.get(
        f"{base}/asset-folders", params={"parent_mode": "all", "state": "all"}
    )
    assert folders.status_code == 200, folders.text
    cycle_items = {
        item["id"]: item for item in folders.json()["items"]
        if item["id"] in {folder_a["id"], folder_b["id"]}
    }
    assert set(cycle_items) == {folder_a["id"], folder_b["id"]}
    assert {item["availability"] for item in cycle_items.values()} == {"invalid_parent_chain"}

    old_folders = client.get(f"{base}/folders")
    assert old_folders.status_code == 200, old_folders.text
    old_b = next(item for item in old_folders.json() if item["id"] == folder_b["id"])
    assert old_b["availability"] == "invalid_parent_chain"


def test_case_count_and_page_share_repeatable_read_snapshot(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    _create_case(client, base, "快照原用例", "/snapshot/original")
    second = migrator_connection()
    observed: dict[str, object] = {"inserted": False}

    def after_query(_conn, cursor, statement, _parameters, _context, _executemany) -> None:
        if observed["inserted"] or "count(*)" not in statement.lower() or "app.cases" not in statement:
            return
        with cursor.connection.cursor() as isolation_cursor:
            isolation_cursor.execute("SHOW transaction_isolation")
            observed["isolation"] = isolation_cursor.fetchone()[0]
        with second.cursor() as writer:
            writer.execute(
                "INSERT INTO app.cases "
                "(id, workspace_id, project_id, name, request, assertions, rev, status) "
                "VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, 1, 'draft')",
                (
                    uuid.uuid4(), account["workspace_id"], project["id"], "并发新增用例",
                    Jsonb(_request("/snapshot/concurrent")),
                ),
            )
        second.commit()
        observed["inserted"] = True

    event.listen(get_engine(), "after_cursor_execute", after_query)
    try:
        response = client.get(f"{base}/case-library", params={"limit": 20})
    finally:
        event.remove(get_engine(), "after_cursor_execute", after_query)
        second.close()
    assert response.status_code == 200, response.text
    assert observed == {"inserted": True, "isolation": "repeatable read"}
    assert response.json()["total"] == 1
    assert len(response.json()["items"]) == 1
    assert client.get(f"{base}/case-library", params={"limit": 20}).json()["total"] == 2


def test_folder_count_page_and_ancestors_share_snapshot_and_empty_page_keeps_total(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    parent = client.post(f"{base}/folders", json={"name": "快照父目录"}).json()
    child = client.post(
        f"{base}/folders", json={"name": "快照子目录 A", "parent_id": parent["id"]}
    ).json()
    second = migrator_connection()
    observed: dict[str, object] = {"inserted": False, "archived": False}

    def after_query(_conn, cursor, statement, _parameters, _context, _executemany) -> None:
        lowered = statement.lower()
        if not observed["inserted"] and "count(*)" in lowered and "app.folders" in statement:
            with cursor.connection.cursor() as isolation_cursor:
                isolation_cursor.execute("SHOW transaction_isolation")
                observed["isolation"] = isolation_cursor.fetchone()[0]
            with second.cursor() as writer:
                writer.execute(
                    "INSERT INTO app.folders "
                    "(id, workspace_id, project_id, parent_id, name, normalized_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(), account["workspace_id"], project["id"], parent["id"],
                        "快照子目录 B", "快照子目录 b",
                    ),
                )
            second.commit()
            observed["inserted"] = True
            return
        if (
            observed["inserted"] and not observed["archived"]
            and "order by app.folders.normalized_name" in lowered
        ):
            with second.cursor() as writer:
                writer.execute("UPDATE app.folders SET archived_at = now() WHERE id = %s", (parent["id"],))
            second.commit()
            observed["archived"] = True

    event.listen(get_engine(), "after_cursor_execute", after_query)
    try:
        response = client.get(
            f"{base}/asset-folders",
            params={"parent_mode": "exact", "parent_id": parent["id"], "limit": 1},
        )
    finally:
        event.remove(get_engine(), "after_cursor_execute", after_query)
        second.close()
    assert response.status_code == 200, response.text
    assert observed == {"inserted": True, "archived": True, "isolation": "repeatable read"}
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == [child["id"]]
    assert response.json()["items"][0]["availability"] == "available"
    assert response.json()["items"][0]["ancestor_path"][-1]["archived"] is False

    refreshed = client.get(
        f"{base}/asset-folders",
        params={"parent_mode": "exact", "parent_id": parent["id"], "limit": 1},
    ).json()
    assert refreshed["total"] == 2
    assert refreshed["items"][0]["availability"] == "ancestor_archived"
    assert refreshed["items"][0]["ancestor_path"][-1]["archived"] is True

    # 归档第二项后，第一页游标之后没有活动项；空页仍保留过滤集合真实总数 1。
    cursor = refreshed["next_cursor"]
    assert cursor
    connection = migrator_connection()
    try:
        with connection.cursor() as writer:
            writer.execute(
                "UPDATE app.folders SET archived_at = now() "
                "WHERE project_id = %s AND parent_id = %s AND id <> %s",
                (project["id"], parent["id"], refreshed["items"][0]["id"]),
            )
        connection.commit()
    finally:
        connection.close()
    empty = client.get(
        f"{base}/asset-folders",
        params={
            "parent_mode": "exact", "parent_id": parent["id"],
            "state": "active", "limit": 1, "cursor": cursor,
        },
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 1
    assert empty.json()["items"] == []


def test_archive_root_uses_explicit_operation_fact_across_member_gap(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    folder_a = client.post(f"{base}/folders", json={"name": "批次根 A"}).json()
    folder_b = client.post(
        f"{base}/folders", json={"name": "此前归档 B", "parent_id": folder_a["id"]}
    ).json()
    folder_c = client.post(
        f"{base}/folders", json={"name": "批次成员 C", "parent_id": folder_b["id"]}
    ).json()
    folder_d = client.post(f"{base}/folders", json={"name": "缺根事实 D"}).json()
    assert client.delete(f"{base}/folders/{folder_b['id']}").status_code == 204

    operation_id, missing_root_operation_id = uuid.uuid4(), uuid.uuid4()
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.asset_operations "
                "(id, workspace_id, project_id, principal_id, operation_key, action, request_hash, result) "
                "VALUES (%s,%s,%s,%s,%s,'archive','root-test',%s),"
                "(%s,%s,%s,%s,%s,'archive','missing-root-test',%s)",
                (
                    operation_id, account["workspace_id"], project["id"], account["user_id"],
                    f"root-{uuid.uuid4()}", Jsonb({"root": {"resource_type": "folder", "id": folder_a["id"]}}),
                    missing_root_operation_id, account["workspace_id"], project["id"], account["user_id"],
                    f"missing-{uuid.uuid4()}", Jsonb({"root": {"resource_type": "folder", "id": str(uuid.uuid4())}}),
                ),
            )
            cursor.execute(
                "UPDATE app.folders SET archived_at=now(), rev=rev+1, archive_operation_id=%s "
                "WHERE id = ANY(%s)",
                (operation_id, [folder_a["id"], folder_c["id"]]),
            )
            cursor.execute(
                "UPDATE app.folders SET archived_at=now(), rev=rev+1, archive_operation_id=%s WHERE id=%s",
                (missing_root_operation_id, folder_d["id"]),
            )
            for folder, parent in ((folder_a, None), (folder_c, folder_b["id"])):
                cursor.execute(
                    "INSERT INTO app.asset_archive_members "
                    "(workspace_id,project_id,operation_id,folder_id,before_rev,archived_rev,original_parent_id,before_state) "
                    "VALUES (%s,%s,%s,%s,1,2,%s,'active')",
                    (account["workspace_id"], project["id"], operation_id, folder["id"], parent),
                )
            cursor.execute(
                "INSERT INTO app.asset_archive_members "
                "(workspace_id,project_id,operation_id,folder_id,before_rev,archived_rev,original_parent_id,before_state) "
                "VALUES (%s,%s,%s,%s,1,2,NULL,'active')",
                (account["workspace_id"], project["id"], missing_root_operation_id, folder_d["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    response = client.get(
        f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
    )
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[folder_a["id"]]["restore_mode"] == "batch_root"
    assert items[folder_a["id"]]["archive_root_id"] == folder_a["id"]
    assert items[folder_c["id"]]["restore_mode"] == "locate_root"
    assert items[folder_c["id"]]["archive_root_id"] == folder_a["id"]
    assert items[folder_b["id"]]["restore_mode"] == "legacy_single"
    assert items[folder_d["id"]]["restore_mode"] == "unavailable"
    assert items[folder_d["id"]]["archive_root_id"] is None

    # 批次根是共享资产事实，不依赖原操作人的个人偏好；另一 viewer 读取结果相同。
    viewer_connection = migrator_connection()
    viewer_name = f"root-viewer-{uuid.uuid4().hex[:8]}"
    viewer_id, _ = seed_account(
        viewer_connection, username=viewer_name, password=PASSWORD, workspace_role=None
    )
    with viewer_connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspace_memberships (workspace_id,user_id,role) VALUES (%s,%s,'viewer')",
            (account["workspace_id"], viewer_id),
        )
    viewer_connection.commit()
    try:
        from app.main import app

        with TestClient(app) as viewer:
            login = viewer.post(
                "/api/v1/auth/login", json={"username": viewer_name, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            viewer.headers["X-CSRF-Token"] = viewer.cookies["interface_csrf"]
            shared = viewer.get(
                f"{base}/asset-folders", params={"parent_mode": "all", "state": "archived"}
            )
            assert shared.status_code == 200, shared.text
            shared_c = next(item for item in shared.json()["items"] if item["id"] == folder_c["id"])
            assert shared_c["archive_root_id"] == folder_a["id"]
            assert shared_c["restore_mode"] == "locate_root"
    finally:
        purge_user(viewer_connection, viewer_id)
        viewer_connection.close()


def test_asset_selection_none_target_is_sql_null(
    account: dict, project: dict
) -> None:
    selection_id = uuid.uuid4()
    session = get_session_factory()()
    try:
        apply_tenant(session, account["workspace_id"], account["user_id"])
        session.add(
            AssetSelection(
                id=selection_id,
                workspace_id=account["workspace_id"],
                project_id=project["id"],
                principal_id=account["user_id"],
                action="move",
                selector={"mode": "explicit"},
                members=[],
                target=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        session.commit()
    finally:
        session.close()

    connection = migrator_connection()
    try:
        row = connection.execute(
            "SELECT target IS NULL, target = 'null'::jsonb FROM app.asset_selections WHERE id=%s",
            (selection_id,),
        ).fetchone()
        assert row == (True, None)
    finally:
        connection.close()


def _asset_read_url(client: TestClient, account: dict, project: dict, endpoint: str) -> str:
    base = _base(account, project)
    folder = client.post(f"{base}/folders", json={"name": f"重新核权-{uuid.uuid4()}"}).json()
    _create_case(client, base, f"重新核权-{uuid.uuid4()}", "/reauth", folder_id=folder["id"])
    return {
        "case-library": f"{base}/case-library",
        "asset-folders": f"{base}/asset-folders?parent_mode=all",
        "folders": f"{base}/folders",
    }[endpoint]


@pytest.mark.parametrize("endpoint", ["case-library", "asset-folders", "folders"])
@pytest.mark.parametrize("revocation", ["disabled_user", "revoked_session"])
def test_consistent_read_reauthenticates_account_and_session_after_old_transaction(
    client: TestClient,
    account: dict,
    project: dict,
    endpoint: str,
    revocation: str,
) -> None:
    url = _asset_read_url(client, account, project, endpoint)
    writer = migrator_connection()
    changed = False

    def revoke_after_old_snapshot(_session: OrmSession) -> None:
        nonlocal changed
        if changed:
            return
        with writer.cursor() as cursor:
            if revocation == "disabled_user":
                cursor.execute("UPDATE app.users SET status='disabled' WHERE id=%s", (account["user_id"],))
            else:
                cursor.execute(
                    "UPDATE app.sessions SET revoked_at=now() WHERE user_id=%s",
                    (account["user_id"],),
                )
        writer.commit()
        changed = True

    event.listen(OrmSession, "after_commit", revoke_after_old_snapshot)
    try:
        response = client.get(url)
    finally:
        event.remove(OrmSession, "after_commit", revoke_after_old_snapshot)
        writer.close()
    assert changed is True
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "unauthorized"


def test_consistent_read_rejects_expired_original_session(
    client: TestClient, account: dict, project: dict
) -> None:
    url = _asset_read_url(client, account, project, "case-library")
    writer = migrator_connection()
    changed = False

    def expire_after_old_snapshot(_session: OrmSession) -> None:
        nonlocal changed
        if changed:
            return
        with writer.cursor() as cursor:
            cursor.execute(
                "UPDATE app.sessions SET expires_at=now() - interval '1 second' WHERE user_id=%s",
                (account["user_id"],),
            )
        writer.commit()
        changed = True

    event.listen(OrmSession, "after_commit", expire_after_old_snapshot)
    try:
        response = client.get(url)
    finally:
        event.remove(OrmSession, "after_commit", expire_after_old_snapshot)
        writer.close()
    assert changed is True
    assert response.status_code == 401, response.text


def test_consistent_read_rejects_original_session_reassigned_to_other_subject(
    client: TestClient, account: dict, project: dict
) -> None:
    url = _asset_read_url(client, account, project, "asset-folders")
    writer = migrator_connection()
    other_name = f"reauth-other-{uuid.uuid4().hex[:8]}"
    other_id, _ = seed_account(writer, username=other_name, password=PASSWORD, workspace_role=None)
    changed = False

    def reassign_after_old_snapshot(_session: OrmSession) -> None:
        nonlocal changed
        if changed:
            return
        with writer.cursor() as cursor:
            cursor.execute(
                "UPDATE app.sessions SET user_id=%s WHERE user_id=%s",
                (other_id, account["user_id"]),
            )
        writer.commit()
        changed = True

    event.listen(OrmSession, "after_commit", reassign_after_old_snapshot)
    try:
        response = client.get(url)
    finally:
        event.remove(OrmSession, "after_commit", reassign_after_old_snapshot)
        purge_user(writer, other_id)
        writer.close()
    assert changed is True
    assert response.status_code == 401, response.text


def test_consistent_read_uses_fresh_role_and_does_not_allow_admin_bypass(
    client: TestClient, account: dict, project: dict
) -> None:
    url = _asset_read_url(client, account, project, "folders")
    writer = migrator_connection()
    changed = False

    def change_permission_after_old_snapshot(_session: OrmSession) -> None:
        nonlocal changed
        if changed:
            return
        with writer.cursor() as cursor:
            cursor.execute("UPDATE app.users SET is_admin=true WHERE id=%s", (account["user_id"],))
            cursor.execute(
                "UPDATE app.workspace_memberships SET role='revoked' "
                "WHERE workspace_id=%s AND user_id=%s",
                (account["workspace_id"], account["user_id"]),
            )
        writer.commit()
        changed = True

    event.listen(OrmSession, "after_commit", change_permission_after_old_snapshot)
    try:
        response = client.get(url)
    finally:
        event.remove(OrmSession, "after_commit", change_permission_after_old_snapshot)
        writer.close()
    assert changed is True
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "insufficient_role"
