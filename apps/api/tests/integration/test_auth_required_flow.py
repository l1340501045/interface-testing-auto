"""auth_required 的执行保护：无可用身份时零发送，不退回匿名。

对应 design.md 3.3：导入识别到认证头／Cookie 时，请求带着“必须使用环境登录态”的
约束进入版本、调试摘要与运行快照。页面上的标记只是提示，拦不住队列期间的配置变化，
真正的判据是执行前的那一刻。受控目标只在收到认证头时才放行，因此“变成了 200”就是
约束没有生效的可观察证据。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import (
    PLAIN_SECRET,
    _debug_assertions,
    _grant_debug_snapshot,
    _prepare_authenticated_environment,
    _run_once,
    _snapshot_digest,
)

from harness import create_environment, get_report, start_run

pytestmark = pytest.mark.integration

_REQUIRE_HEADER = {"method": "GET", "path": "/require-header", "body_type": "none", "body": ""}


def _codes(report: dict) -> list[str]:
    return [step["error_code"] for step in report["steps"]]


def test_auth_required_blocks_send_when_no_identity_is_configured(
    client: TestClient, account: dict, project: dict
) -> None:
    """标了必须认证、但环境没有任何可用身份：拒绝发送，不匿名降级。"""
    environment = create_environment(client, account, project, name="必需认证无身份环境")
    request = {**_REQUIRE_HEADER, "auth_required": True}
    snapshot = {"request": request, "assertions": _debug_assertions()}
    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert _codes(report) == ["credential_required"]
    assert report["run"]["reason_category"] == "authentication"
    assert report["response"] is None, "没有身份时必须零发送，不能匿名打到目标"
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_auth_required_sends_with_the_configured_identity(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一份内容在配好身份并授权之后可以正常发送，认证头确实到达目标。"""
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "必需认证有身份环境"
    )
    request = {**_REQUIRE_HEADER, "auth_required": True}
    snapshot = {"request": request, "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )

    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_identity_removed_after_queueing_blocks_the_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """入队之后身份不再可用时，执行前必须拦住——约束不能只存在于提交那一刻。

    复现的路径是：提交时环境还有可用身份（提交本身成功），排队期间身份被停用。
    页面上的标记早已过期，只有执行前按当前事实重新判定才拦得住。
    """
    from harness import migrator_connection

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "身份排队后失效环境"
    )
    request = {**_REQUIRE_HEADER, "auth_required": True}
    snapshot = {"request": request, "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.credential_profiles SET status = 'unavailable' WHERE id = %s",
                (profile["id"],),
            )
        connection.commit()
    finally:
        connection.close()

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert _codes(report) == ["credential_required"]
    assert report["response"] is None
