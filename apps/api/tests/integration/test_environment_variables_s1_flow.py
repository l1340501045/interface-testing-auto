"""S1 配置修订、同源解析、首次早拒和幂等优先的真实 PostgreSQL 回归。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_credentials_flow import (
    DEMO_SLOT,
    PLAIN_SECRET,
    QUERY_SLOT,
    _create_secret,
    _debug_assertions,
    _debug_request,
    _grant_case_version,
    _grant_debug_snapshot,
    _prepare_authenticated_environment,
    _run_once,
    _snapshot_digest,
)

from harness import (
    PASSWORD,
    create_case,
    create_environment,
    get_report,
    migrator_connection,
    project_base,
    publish_case,
    seed_account,
)

pytestmark = pytest.mark.integration


def _counts(project_id: str) -> tuple[int, int]:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM app.runs WHERE project_id = %s", (project_id,))
            runs = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM app.jobs WHERE project_id = %s", (project_id,))
            jobs = cursor.fetchone()[0]
        return runs, jobs
    finally:
        connection.close()


def _put_variables(client: TestClient, base: str, version: int, variables: list[dict]):
    return client.put(
        f"{base}/variables",
        json={"variables": variables},
        headers={"If-Match": str(version)},
    )


def _snapshot(path: str = "/echo/{{地区.代码}}", body: str = "") -> dict:
    return {
        "request": {
            "method": "POST" if body else "GET",
            "path": path,
            "query_params": [],
            "headers": [],
            "body_type": "text" if body else "none",
            "body": body,
        },
        "assertions": [],
    }


def test_config_writes_require_fresh_revision_and_append_environment_snapshot(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="配置修订环境")
    assert (environment["rev"], environment["config_version"]) == (1, 1)

    missing = client.patch(
        f"{base}/environments/{environment['id']}", json={"name": "不应保存"}
    )
    assert missing.status_code == 409
    assert missing.json()["code"] == "config_revision_required"

    saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"地区.代码": {"type": "string", "text": "cn"}}},
        headers={"If-Match": '"1"'},
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["rev"], saved.json()["config_version"]) == (2, 2)

    stale = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {}},
        headers={"If-Match": "1"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "config_revision_conflict"

    project_missing = client.put(f"{base}/variables", json={"variables": []})
    assert project_missing.status_code == 409
    assert project_missing.json()["code"] == "config_revision_required"
    first = _put_variables(
        client,
        base,
        0,
        [{"name": "共享", "value": {"type": "number", "text": "9007199254740993"}}],
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    stale_first = _put_variables(client, base, 0, [])
    assert stale_first.status_code == 409
    assert stale_first.json()["code"] == "config_revision_conflict"

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT e.rev, v.version, v.schema_version, v.snapshot, v.created_by "
                "FROM app.environments e JOIN app.environment_config_versions v "
                "ON v.id = e.current_config_version_id WHERE e.id = %s",
                (environment["id"],),
            )
            row = cursor.fetchone()
            assert row[:3] == (2, 2, 1)
            assert row[3]["variables"]["地区.代码"]["text"] == "cn"
            assert str(row[4]) == str(account["user_id"])
            cursor.execute(
                "SELECT count(*) FROM app.environment_config_versions WHERE environment_id = %s",
                (environment["id"],),
            )
            assert cursor.fetchone()[0] == 2
    finally:
        connection.close()


def test_preview_and_first_admission_share_binding_and_idempotency_semantics(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="变量解析环境")
    project_saved = _put_variables(
        client,
        base,
        0,
        [
            {"name": "地区.代码", "value": {"type": "string", "text": "project"}},
            {"name": "共享", "value": {"type": "number", "text": "9007199254740993"}},
        ],
    )
    assert project_saved.status_code == 200, project_saved.text
    environment_saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"地区.代码": {"type": "string", "text": "env"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert environment_saved.status_code == 200, environment_saved.text

    context = client.get(
        f"{base}/variable-context", params={"environment_id": environment["id"]}
    )
    assert context.status_code == 200, context.text
    selected = next(item for item in context.json()["variables"] if item["name"] == "地区.代码")
    assert selected["value"] == {"type": "string", "text": "env"}
    assert selected["effective_source"]["level"] == "environment"
    assert selected["overridden_sources"][0]["value"]["text"] == "project"

    snapshot = _snapshot()
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preview.status_code == 200, preview.text
    resolved = preview.json()
    assert resolved["ready"] is True
    assert resolved["ordinary_resolution"] == "ready"
    assert resolved["bindings"][0]["source"]["level"] == "environment"
    assert "row_id" not in resolved["bindings"][0]["location"]
    assert "selector" not in resolved["bindings"][0]["location"]
    token = resolved["resolution_context"]
    assert isinstance(token, str) and token
    assert _counts(project["id"]) == (0, 0), "预览不能创建运行或工作项"

    missing_snapshot = _snapshot(path="/echo", body="😀{{缺失}}")
    missing = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": missing_snapshot},
    )
    assert missing.status_code == 200, missing.text
    issue = missing.json()["issues"][0]
    assert issue["code"] == "variable_undefined"
    assert issue["location"]["utf16_span"]["start"] == 2
    rejected = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": missing_snapshot},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "variable_undefined"
    assert _counts(project["id"]) == (0, 0)

    accepted_payload = {
        "environment_id": environment["id"],
        "debug_snapshot": snapshot,
        "resolution_context": token,
    }
    accepted = client.post(
        f"{base}/runs",
        json=accepted_payload,
        headers={"Idempotency-Key": "s1-same-intent"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["id"]

    changed = _put_variables(
        client,
        base,
        1,
        [{"name": "地区.代码", "value": {"type": "string", "text": "changed"}}],
    )
    assert changed.status_code == 200, changed.text
    same_intent = client.post(
        f"{base}/runs",
        json=accepted_payload,
        headers={"Idempotency-Key": "s1-same-intent"},
    )
    assert same_intent.status_code == 202, same_intent.text
    assert same_intent.json()["id"] == run_id, "已受理同键必须优先返回原运行"

    expired_context = client.post(
        f"{base}/runs",
        json=accepted_payload,
        headers={"Idempotency-Key": "s1-new-intent"},
    )
    assert expired_context.status_code == 409, expired_context.text
    assert expired_context.json()["code"] == "resolution_context_changed"
    assert _counts(project["id"]) == (1, 1)

    assert _run_once(project["pool_id"], worker_id="it-s1-resolution-worker") == "completed_unchecked"
    report = get_report(client, account, project, run_id)
    assert report["resolution"]["context_fingerprint"] == resolved["context_fingerprint"]
    assert report["context"]["resolution"]["guard"] == "ordinary_binding_enforced_v1"
    assert report["context"]["resolution"]["context_fingerprint"] == resolved["context_fingerprint"]
    assert report["context"]["resolution"]["binding_fingerprint"]


def test_auth_slot_conflict_keeps_user_row_and_blocks_before_run(
    client: TestClient, account: dict, project: dict
) -> None:
    base, environment, _profile = _prepare_authenticated_environment(
        client, account, project, "槽位冲突环境"
    )
    header_name = DEMO_SLOT.split(".", 1)[1]
    missing_and_conflicting = {
        "request": {
            "method": "GET",
            "path": "/echo",
            "query_params": [],
            "headers": [{"name": header_name.lower(), "value": "{{缺失令牌}}"}],
            "body_type": "none",
            "body": "",
        },
        "assertions": [],
    }
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": missing_and_conflicting},
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["auth"]["status"] == "needs_authorization"
    assert result["auth"]["injection_slots"] == [
        {"kind": "header", "name": header_name, "status": "conflict"}
    ]
    assert {item["code"] for item in result["issues"]} >= {
        "variable_undefined",
        "credential_slot_conflict",
    }
    assert result["resolution_context"] is None
    assert _counts(project["id"]) == (0, 0)

    literal_conflict = {
        **missing_and_conflicting,
        "request": {
            **missing_and_conflicting["request"],
            "headers": [{"name": header_name, "value": "user-value"}],
        },
    }
    rejected = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": literal_conflict},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "credential_slot_conflict"
    assert _counts(project["id"]) == (0, 0)


def test_needs_authorization_keeps_ordinary_context_for_one_grant_and_run(
    client: TestClient, account: dict, project: dict
) -> None:
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "待授权解析环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["ordinary_resolution"] == "ready"
    assert result["auth"]["status"] == "needs_authorization"
    assert result["ready"] is False
    assert result["resolution_context"], "普通配置并发依据不等同于身份授权"
    assert _counts(project["id"]) == (0, 0)

    digest = _snapshot_digest(client, base, snapshot)
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    accepted = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": result["resolution_context"],
        },
        headers={"Idempotency-Key": "s1-needs-auth-single-run"},
    )
    assert accepted.status_code == 202, accepted.text
    assert _counts(project["id"]) == (1, 1)

    changed = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"变更": {"type": "string", "text": "new"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert changed.status_code == 200, changed.text
    rejected = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": result["resolution_context"],
        },
        headers={"Idempotency-Key": "s1-needs-auth-changed-config"},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "resolution_context_changed"
    assert _counts(project["id"]) == (1, 1)

def test_fixed_version_preview_uses_case_version_grant_and_rejects_null_source(
    client: TestClient, account: dict, project: dict
) -> None:
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "固定版本预览环境"
    )
    case = create_case(
        client,
        account,
        project,
        name="固定版本预览用例",
        request=_debug_request(),
        assertions=_debug_assertions(),
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["auth"]["status"] == "ready"
    assert preview.json()["resolution_context"]
    accepted = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "case_version_id": version["id"],
            "resolution_context": preview.json()["resolution_context"],
        },
    )
    assert accepted.status_code == 202, accepted.text

    explicit_null = client.post(
        f"{base}/resolution-preview",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _debug_request(), "assertions": []},
            "source_case_id": None,
        },
    )
    assert explicit_null.status_code == 422
    assert explicit_null.json()["code"] == "invalid_request"


def test_unknown_shadow_source_is_explained_without_blocking_or_leaking(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="影子来源环境")
    saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"共享": {"type": "string", "text": "effective"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert saved.status_code == 200, saved.text

    protected_text = "must-not-leak-shadow-value"
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.project_config_versions "
                "(workspace_id, project_id, version, variables) VALUES (%s, %s, 1, %s)",
                (
                    account["workspace_id"],
                    project["id"],
                    Jsonb({"共享": {"type": "secret", "text": protected_text}}),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    context = client.get(
        f"{base}/variable-context", params={"environment_id": environment["id"]}
    )
    assert context.status_code == 200, context.text
    item = next(value for value in context.json()["variables"] if value["name"] == "共享")
    assert item["value"] == {"type": "string", "text": "effective"}
    assert item["overridden_sources"][0]["value"] is None
    assert item["overridden_sources"][0]["unavailable_reason"]
    assert protected_text not in context.text

    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": _snapshot("/echo/{{共享}}")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["ordinary_resolution"] == "ready"
    assert protected_text not in preview.text


def test_concurrent_first_project_variable_writes_have_one_winner(
    account: dict, project: dict
) -> None:
    from app.main import app

    base = project_base(account, project)
    barrier = Barrier(2)

    def write(value: str) -> tuple[int, str]:
        with TestClient(app) as concurrent_client:
            login = concurrent_client.post(
                "/api/v1/auth/login",
                json={"username": account["username"], "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            concurrent_client.headers["X-CSRF-Token"] = concurrent_client.cookies["interface_csrf"]
            barrier.wait()
            response = _put_variables(
                concurrent_client,
                base,
                0,
                [{"name": "并发", "value": {"type": "string", "text": value}}],
            )
            return response.status_code, response.json()["code"] if response.status_code != 200 else "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ["甲", "乙"]))
    assert sorted(outcomes) == [(200, "ok"), (409, "config_revision_conflict")]


def test_inconsistent_current_config_uses_endpoint_specific_error_semantics(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="配置指针异常环境")
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.environment_config_versions SET snapshot = %s "
                "WHERE id = (SELECT current_config_version_id FROM app.environments WHERE id = %s)",
                (Jsonb({"schema_version": 1, "variables": {"漂移": {"type": "string", "text": "x"}}}), environment["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    catalog = client.get(
        f"{base}/variable-context", params={"environment_id": environment["id"]}
    )
    assert catalog.status_code == 409
    assert catalog.json()["code"] == "config_inconsistent"

    snapshot = _snapshot("/echo")
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preview.status_code == 409
    assert preview.json()["code"] == "config_inconsistent"

    legacy_preflight = client.post(
        f"{base}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert legacy_preflight.status_code == 200, legacy_preflight.text
    assert legacy_preflight.json()["ready"] is False
    assert legacy_preflight.json()["resolution"] is None
    assert legacy_preflight.json()["issues"][-1]["code"] == "config_inconsistent"
    assert legacy_preflight.json()["issues"][-1]["action"] == "configure_environment"

    rejected = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "config_inconsistent"
    assert _counts(project["id"]) == (0, 0)


def test_new_presence_fields_reject_explicit_null_without_tightening_old_omission(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="Presence环境")
    snapshot = _snapshot("/echo")
    old_omission = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert old_omission.status_code == 202, old_omission.text

    null_context = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": None,
        },
    )
    assert null_context.status_code == 422
    empty_context = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": "",
        },
    )
    assert empty_context.status_code == 422
    null_version = client.post(
        f"{base}/resolution-preview",
        json={
            "environment_id": environment["id"],
            "case_version_id": None,
            "debug_snapshot": snapshot,
        },
    )
    assert null_version.status_code == 422
    null_snapshot = client.post(
        f"{base}/resolution-preview",
        json={
            "environment_id": environment["id"],
            "case_version_id": project["id"],
            "debug_snapshot": None,
        },
    )
    assert null_snapshot.status_code == 422


def test_json_escaped_names_and_structured_previews_follow_real_prepare(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="结构化绑定环境")
    variables = {
        "地区.代码": {"type": "string", "text": "华东"},
        "长号": {"type": "number", "text": "9007199254740993"},
        "空值": {"type": "null"},
        "对象": {"type": "json", "text": '{"a":1}'},
    }
    saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": variables},
        headers={"If-Match": str(environment["rev"])},
    )
    assert saved.status_code == 200, saved.text
    body = (
        '{"region":"{{\\u5730\\u533a.\\u4ee3\\u7801}}",'
        '"n":"{{长号}}","nil":"{{空值}}","obj":"{{对象}}",'
        '"embedded":"前{{空值}}后"}'
    )
    snapshot = {
        "request": {
            "method": "POST",
            "path": "/echo",
            "query_params": [],
            "headers": [],
            "body_type": "json",
            "body": body,
        },
        "assertions": [],
    }
    preview = client.post(
        f"{base}/resolution-preview",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["ordinary_resolution"] == "ready"
    assert result["issues"] == []
    by_name = {item["name"]: item for item in result["bindings"]}
    assert by_name["地区.代码"]["rendered_preview"] == '"华东"'
    assert by_name["地区.代码"]["location"]["selector"] == [
        {"kind": "key", "key": "region"}
    ]
    assert "utf16_span" not in by_name["地区.代码"]["location"], (
        "JSON 转义名称无法可靠映射回原文 span 时必须省略"
    )
    assert by_name["长号"]["rendered_preview"] == "9007199254740993"
    assert by_name["空值"]["rendered_preview"] in {"null", '"前后"'}
    null_previews = {
        item["rendered_preview"] for item in result["bindings"] if item["name"] == "空值"
    }
    assert null_previews == {"null", '"前后"'}
    assert by_name["对象"]["rendered_preview"] == '{"a":1}'

    accepted = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": snapshot,
            "resolution_context": result["resolution_context"],
        },
    )
    assert accepted.status_code == 202, accepted.text


def test_variable_catalog_marks_unrepresentable_names_and_form_limits(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="旧名称目录环境")
    saved = _put_variables(
        client,
        base,
        0,
        [
            {"name": "请求 ID", "value": {"type": "string", "text": "legacy"}},
            {"name": "含{括号", "value": {"type": "string", "text": "legacy"}},
            {"name": "中文.点:冒号", "value": {"type": "string", "text": "ok"}},
            {"name": "form+name", "value": {"type": "string", "text": "ok"}},
        ],
    )
    assert saved.status_code == 200, saved.text
    response = client.get(
        f"{base}/variable-context", params={"environment_id": environment["id"]}
    )
    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["variables"]}
    for name in ("请求 ID", "含{括号"):
        assert items[name]["reference"] is None
        assert items[name]["available_locations"] == []
        assert items[name]["unavailable_reason"]
    assert items["中文.点:冒号"]["reference"] == "{{中文.点:冒号}}"
    assert items["中文.点:冒号"]["unavailable_reason"] is None
    assert items["form+name"]["restricted_body_types"] == ["form"]


def test_viewer_can_read_ordinary_preview_without_execution_or_secret_power(
    client: TestClient, account: dict, project: dict
) -> None:
    from app.main import app

    base = project_base(account, project)
    environment = create_environment(client, account, project, name="查看者预览环境")
    connection = migrator_connection()
    username = "it-s1-viewer-" + str(account["user_id"])[:8]
    viewer_id, _unused_workspace = seed_account(
        connection, username=username, password=PASSWORD, workspace_role=None
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspace_memberships (workspace_id, user_id, role) "
            "VALUES (%s, %s, 'viewer')",
            (account["workspace_id"], viewer_id),
        )
    connection.commit()
    try:
        with TestClient(app) as viewer:
            login = viewer.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            viewer.headers["X-CSRF-Token"] = viewer.cookies["interface_csrf"]
            snapshot = _snapshot("/echo")
            preview = viewer.post(
                f"{base}/resolution-preview",
                json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            )
            assert preview.status_code == 200, preview.text
            result = preview.json()
            assert result["ordinary_resolution"] == "ready"
            assert result["auth"] == {
                "required": False,
                "status": "unchecked",
                "injection_slots": [],
                "requires_worker_verification": True,
            }
            assert result["ready"] is False
            assert result["resolution_context"]

            old_preflight = viewer.post(
                f"{base}/debug-preflight",
                json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            )
            assert old_preflight.status_code == 403
            run = viewer.post(
                f"{base}/runs",
                json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            )
            assert run.status_code == 403
            credentials = viewer.get(f"{base}/credentials/profiles")
            assert credentials.status_code == 403
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM app.sessions WHERE user_id = %s", (viewer_id,))
            cursor.execute(
                "DELETE FROM app.workspace_memberships WHERE user_id = %s", (viewer_id,)
            )
            cursor.execute("DELETE FROM app.users WHERE id = %s", (viewer_id,))
        connection.commit()
        connection.close()


@pytest.mark.parametrize("mode", ["catalog", "legacy", "preview_debug", "preview_fixed"])
def test_read_models_use_one_repeatable_snapshot_during_concurrent_config_write(
    client: TestClient,
    account: dict,
    project: dict,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from app.main import app
    from app.services import resolution as resolution_service

    base = project_base(account, project)
    environment = create_environment(client, account, project, name=f"一致读取-{mode}")
    initial = _put_variables(
        client,
        base,
        0,
        [{"name": "并发来源", "value": {"type": "string", "text": "old"}}],
    )
    assert initial.status_code == 200, initial.text
    request = {
        "method": "GET",
        "path": "/echo",
        "query_params": [{"name": "value", "value": "{{并发来源}}"}],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    snapshot = {"request": request, "assertions": []}
    version_id = None
    if mode == "preview_fixed":
        case = create_case(client, account, project, name="一致读取固定版本", request=request)
        version_id = publish_case(client, account, project, case["id"])["id"]

    entered, release = Event(), Event()
    real_load = resolution_service.load_variable_state

    def paused_load(session, current_environment):
        entered.set()
        assert release.wait(5), "并发写完成后应释放读取"
        return real_load(session, current_environment)

    monkeypatch.setattr(resolution_service, "load_variable_state", paused_load)

    def read() -> tuple[int, dict]:
        with TestClient(app) as reader:
            login = reader.post(
                "/api/v1/auth/login",
                json={"username": account["username"], "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            reader.headers["X-CSRF-Token"] = reader.cookies["interface_csrf"]
            if mode == "catalog":
                response = reader.get(
                    f"{base}/variable-context", params={"environment_id": environment["id"]}
                )
            elif mode == "legacy":
                response = reader.post(
                    f"{base}/debug-preflight",
                    json={"environment_id": environment["id"], "debug_snapshot": snapshot},
                )
            else:
                payload = (
                    {"environment_id": environment["id"], "case_version_id": version_id}
                    if mode == "preview_fixed"
                    else {"environment_id": environment["id"], "debug_snapshot": snapshot}
                )
                response = reader.post(f"{base}/resolution-preview", json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        assert entered.wait(5), "读取必须进入配置加载阶段"
        changed = _put_variables(
            client,
            base,
            1,
            [{"name": "并发来源", "value": {"type": "string", "text": "new"}}],
        )
        assert changed.status_code == 200, changed.text
        release.set()
        status, result = future.result(timeout=10)
    assert status == 200, result
    resolution = result if mode.startswith("preview") else result.get("resolution")
    if mode == "catalog":
        assert result["config_basis"]["project_variables_version"] == 1
        item = next(value for value in result["variables"] if value["name"] == "并发来源")
        assert item["value"]["text"] == "old"
    else:
        assert resolution["config_basis"]["project_variables_version"] == 1
        assert resolution["bindings"][0]["rendered_preview"] == "old"


def test_repeated_references_have_unique_stable_issue_and_binding_ids(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="重复引用环境")

    missing_snapshot = {
        "request": {
            "method": "POST",
            "path": "/echo",
            "query_params": [],
            "headers": [],
            "body_type": "json",
            "body": '{"x":"{{缺失甲}}-{{缺失乙}}-{{缺失甲}}"}',
        },
        "assertions": [],
    }

    def preview(snapshot: dict) -> dict:
        response = client.post(
            f"{base}/resolution-preview",
            json={"environment_id": environment["id"], "debug_snapshot": snapshot},
        )
        assert response.status_code == 200, response.text
        return response.json()

    first_missing = preview(missing_snapshot)
    second_missing = preview(missing_snapshot)
    issue_ids = [item["issue_id"] for item in first_missing["issues"]]
    assert len(issue_ids) == 3
    assert len(set(issue_ids)) == 3
    assert issue_ids == [item["issue_id"] for item in second_missing["issues"]]
    assert [item["code"] for item in first_missing["issues"]] == [
        "variable_undefined",
        "variable_undefined",
        "variable_undefined",
    ]
    assert len(
        {
            str(item["location"]["selector"])
            for item in first_missing["issues"]
        }
    ) == 1, "ID 唯一不能依赖伪造不同定位"

    saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"重复": {"type": "string", "text": "v"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert saved.status_code == 200, saved.text

    json_snapshot = {
        "request": {
            "method": "POST",
            "path": "/echo",
            "query_params": [],
            "headers": [],
            "body_type": "json",
            "body": '{"{{重复}}":"{{重复}}/{{重复}}"}',
        },
        "assertions": [],
    }
    json_first = preview(json_snapshot)
    json_second = preview(json_snapshot)
    binding_ids = [item["binding_id"] for item in json_first["bindings"]]
    assert len(binding_ids) == 3
    assert len(set(binding_ids)) == 3
    assert binding_ids == [item["binding_id"] for item in json_second["bindings"]]

    form_snapshot = {
        "request": {
            "method": "POST",
            "path": "/echo",
            "query_params": [],
            "headers": [],
            "body_type": "form",
            "body": "first={{重复}}&second={{重复}}",
        },
        "assertions": [],
    }
    form_first = preview(form_snapshot)
    form_second = preview(form_snapshot)
    form_ids = [item["binding_id"] for item in form_first["bindings"]]
    assert len(form_ids) == 2
    assert len(set(form_ids)) == 2
    assert form_ids == [item["binding_id"] for item in form_second["bindings"]]
    assert all(item["location"]["selector"] == [] for item in form_first["bindings"])


def test_masked_target_keeps_prepared_url_and_never_projects_query_secret(
    client: TestClient, account: dict, project: dict
) -> None:
    from app.kernel.request_spec import prepare, validate_request
    from app.kernel.variables import build_resolver

    base = project_base(account, project)
    environment = create_environment(
        client,
        account,
        project,
        name="实际目标环境",
        base_url="http://echo:8080/base",
    )
    variables = {
        "路径": {"type": "string", "text": "子 路径"},
        "查询": {"type": "string", "text": "甲 乙&?"},
    }
    saved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": variables},
        headers={"If-Match": str(environment["rev"])},
    )
    assert saved.status_code == 200, saved.text
    raw_request = {
        "method": "GET",
        "path": "/echo/{{路径}}",
        "query_params": [
            {"name": "dup", "value": "{{查询}}"},
            {"name": "dup", "value": ""},
            {"name": "中文", "value": "前{{查询}}后"},
        ],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    expected = prepare(
        validate_request(raw_request),
        environment["base_url"],
        build_resolver(
            [{"name": name, "value": value} for name, value in variables.items()]
        ),
    ).url
    preview = client.post(
        f"{base}/resolution-preview",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": raw_request, "assertions": []},
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["masked_target"]["url"] == expected
    assert preview.json()["masked_target"]["url"].count("dup=") == 2
    assert "dup=&" in preview.json()["masked_target"]["url"]

    auth_environment = create_environment(client, account, project, name="Query身份预览环境")
    secret = _create_secret(client, base, "Query身份预览令牌", PLAIN_SECRET)
    profile = client.post(
        f"{base}/credentials/profiles",
        json={
            "environment_id": auth_environment["id"],
            "name": "Query身份预览",
            "allowed_targets": ["http://echo:8080"],
            "allowed_auth_slots": [QUERY_SLOT],
            "config": {
                "auth_locations": [{"slot": QUERY_SLOT, "scheme": "query", "prefix": ""}],
                "invalidation": {"status_codes": [401], "redirect_to_login": False},
            },
        },
    )
    assert profile.status_code == 201, profile.text
    activated = client.put(
        f"{base}/credentials/profiles/{profile.json()['id']}/set",
        json={"slots": {QUERY_SLOT: secret["latest_version_id"]}},
    )
    assert activated.status_code == 200, activated.text
    auth_preview = client.post(
        f"{base}/resolution-preview",
        json={
            "environment_id": auth_environment["id"],
            "debug_snapshot": {
                "request": {
                    "method": "GET",
                    "path": "/echo",
                    "query_params": [{"name": "visible", "value": "ordinary"}],
                    "headers": [],
                    "body_type": "none",
                    "body": "",
                },
                "assertions": [],
            },
        },
    )
    assert auth_preview.status_code == 200, auth_preview.text
    auth_result = auth_preview.json()
    assert auth_result["auth"]["status"] == "needs_authorization"
    assert auth_result["auth"]["injection_slots"] == [
        {"kind": "query", "name": "api_key", "status": "pending_worker_verification"}
    ]
    assert auth_result["masked_target"]["url"].endswith("/echo?visible=ordinary")
    assert "api_key" not in auth_result["masked_target"]["url"]
    assert PLAIN_SECRET not in auth_preview.text
