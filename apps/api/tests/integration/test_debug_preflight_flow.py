"""发送前预检与报告来源标记：主体隔离、无原始摘要泄露、状态区分。

对应 design.md 3.2（debug-preflight 最小契约）、3.4（可证明的运行来源）与校正文件
`research/context-fingerprint-correction.md`。

两条容易写错的边界在这里锁死：

- **预检不能变成凭证管理列表的替代入口。** 它按 execute 权限开放给普通编辑者，
  因此只能返回脱敏状态与建议动作；`profile_id` 只有真正能管理身份的人才拿得到，
  否则普通编辑者能读到一个本不该看到的凭证坐标。
- **报告里的关联标记不能是内容摘要。** 正文里的敏感字段会被遮蔽，但裸摘要仍提供了
  低熵比对入口（拿候选值算一遍就知道是不是它），跨主体也会因为内容相同而得到同一
  摘要。因此标记必须是绑定主体与作用域的 HMAC，且公开响应里不得出现原始
  `debug_snapshot_hash`。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import (
    PLAIN_SECRET,
    _debug_assertions,
    _debug_request,
    _grant_debug_snapshot,
    _prepare_authenticated_environment,
    _snapshot_digest,
)

from harness import (
    PASSWORD,
    create_environment,
    migrator_connection,
    project_base,
    seed_account,
)

pytestmark = pytest.mark.integration


def _preflight(
    client: TestClient, account: dict, project: dict, environment_id: str, snapshot: dict
) -> dict:
    response = client.post(
        f"{project_base(account, project)}/debug-preflight",
        json={"environment_id": environment_id, "debug_snapshot": snapshot},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _is_hex_digest(value: object) -> bool:
    """是否看起来像一段 64／16 位十六进制摘要（原始内容摘要就会长这样）。"""
    if not isinstance(value, str) or not value:
        return False
    if len(value) not in (16, 64):
        return False
    return all(char in "0123456789abcdef" for char in value)


def test_preflight_reports_environment_without_any_identity(
    client: TestClient, account: dict, project: dict
) -> None:
    """没有配置任何身份的环境：可以发送，认证状态为 none，且返回来源 context。

    这是普通编辑者的主路径——公开请求不需要凭证管理权限，也不需要先保存用例。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="预检公开环境")
    snapshot = {"request": {"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
                "assertions": _debug_assertions()}

    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is True
    assert result["issues"] == []
    assert result["can_authorize"] is True
    assert result["auth"] == {"required": False, "state": "none", "profile_id": None}
    assert result["context"]["environment"]["id"] == environment["id"]
    assert result["context"]["environment"]["base_url"] == "http://echo:8080"
    # 预检是只读的：不落运行，历史仍为空。
    listed = client.get(f"{base}/runs")
    assert listed.status_code == 200
    assert listed.json() == []


def test_preflight_reports_missing_grant_as_actionable(
    client: TestClient, account: dict, project: dict
) -> None:
    """需要授权但还没有授权时：明确报缺授权，并给出可操作动作，而不是不可点击的按钮。"""
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "预检缺授权环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}

    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is False
    assert result["auth"]["state"] == "needs_authorization"
    assert result["can_authorize"] is True
    # 能管理身份的人拿到就地授权所需的坐标。
    assert result["auth"]["profile_id"] == profile["id"]
    issue = result["issues"][0]
    assert issue["code"] == "credential_not_granted"
    assert issue["action"] == "authorize"
    assert issue["message"]

    # 授权之后同一次预检转为可发送。
    digest = _snapshot_digest(client, base, snapshot)
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    after = _preflight(client, account, project, environment["id"], snapshot)
    assert after["ready"] is True
    assert after["auth"]["state"] == "ready"
    assert after["issues"] == []


def test_preflight_requires_identity_when_request_demands_it(
    client: TestClient, account: dict, project: dict
) -> None:
    """请求带 auth_required 而环境没有可用身份：拒绝并指向配置身份。"""
    environment = create_environment(client, account, project, name="预检需认证环境")
    request = _debug_request()
    request["auth_required"] = True
    snapshot = {"request": request, "assertions": _debug_assertions()}

    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is False
    assert result["auth"]["required"] is True
    assert result["auth"]["state"] == "unavailable"
    assert result["issues"][0]["code"] == "credential_required"
    assert result["issues"][0]["action"] == "authorize"


def test_preflight_reports_ambiguous_identities(
    client: TestClient, account: dict, project: dict
) -> None:
    """环境有两份可用身份时，预检必须报告歧义，而不是任选一条说“可以发送”。"""
    base, environment, _profile = _prepare_authenticated_environment(
        client, account, project, "预检歧义环境"
    )
    from test_credentials_flow import _configure_credential, _create_secret

    secret = _create_secret(client, base, "预检歧义令牌", PLAIN_SECRET)
    _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="预检歧义第二身份",
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}

    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is False
    assert result["auth"]["state"] == "ambiguous"
    assert result["issues"][0]["code"] == "credential_ambiguous"


def test_preflight_editor_gets_no_credential_coordinates(
    client: TestClient, account: dict, project: dict
) -> None:
    """普通编辑者能看到原因与动作，但拿不到身份坐标；且仍然没有凭证管理权限。

    同工作空间里由管理员建项目与身份，编辑者只拥有 editor 角色：这正是“普通成员
    无法自助提权，也不该看到全项目凭证与授权列表”的场景。
    """
    from app.main import app

    base, environment, _profile = _prepare_authenticated_environment(
        client, account, project, "预检编辑者环境"
    )

    connection = migrator_connection()
    username = f"it-preflight-editor-{uuid.uuid4().hex[:8]}"
    editor_id, _ = seed_account(
        connection, username=username, password=PASSWORD, workspace_role=None
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspace_memberships (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'editor')",
            (account["workspace_id"], editor_id),
        )
    connection.commit()

    try:
        with TestClient(app) as editor:
            login = editor.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            editor.headers["X-CSRF-Token"] = editor.cookies["interface_csrf"]

            # 凭证管理接口对编辑者一律 403（含 GET），这正是需要预检补上的真空。
            denied = editor.get(f"{base}/credentials/profiles")
            assert denied.status_code == 403, denied.text

            snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
            response = editor.post(
                f"{base}/debug-preflight",
                json={"environment_id": environment["id"], "debug_snapshot": snapshot},
            )
            assert response.status_code == 200, response.text
            result = response.json()
            # 需要授权：原因与动作都给到，但坐标不给。
            assert result["ready"] is False
            assert result["auth"]["state"] == "needs_authorization"
            assert result["auth"]["profile_id"] is None
            assert result["can_authorize"] is False
            assert result["issues"][0]["action"] == "contact_admin"
            assert PLAIN_SECRET not in response.text
    finally:
        connection.execute("DELETE FROM app.sessions WHERE user_id = %s", (editor_id,))
        connection.execute(
            "DELETE FROM app.workspace_memberships WHERE user_id = %s", (editor_id,)
        )
        connection.commit()
        connection.execute("DELETE FROM app.users WHERE id = %s", (editor_id,))
        connection.commit()
        connection.close()
