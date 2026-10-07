"""报告来源标记：主体隔离、输入变化、公开 payload 不含原始摘要。

对应 design.md 3.4 与校正文件 `research/context-fingerprint-correction.md`：标记是
绑定 workspace／project／发起主体的 HMAC-SHA256，不是内容摘要；报告公开的 JSON 里
不得出现原始的 `debug_snapshot_hash`，否则遮蔽了敏感字段的值、却留下了一个可比对的
入口。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import _debug_assertions, _debug_request, _run_once

from harness import (
    PASSWORD,
    create_environment,
    get_report,
    migrator_connection,
    project_base,
    seed_account,
    start_run,
)

pytestmark = pytest.mark.integration


def _wait_for_report(client: TestClient, account: dict, project: dict, run_id: str) -> dict:
    """执行一次并读回报告；context 只在终态记录上完整。"""
    assert _run_once(project["pool_id"]) in ("passed", "completed_unchecked", "failed", "error")
    return get_report(client, account, project, run_id)


def _debug_run(client: TestClient, account: dict, project: dict, environment_id: str, snapshot: dict) -> dict:
    run = start_run(
        client, account, project, {"environment_id": environment_id, "debug_snapshot": snapshot}
    )
    return _wait_for_report(client, account, project, run["id"])


def test_context_is_emitted_for_a_run_executed_by_the_upgraded_worker(
    client: TestClient, account: dict, project: dict
) -> None:
    """升级后的执行器跑过的运行才提供 context，且两个标记都是不透明值。"""
    environment = create_environment(client, account, project, name="来源标记环境")
    snapshot = {"request": _debug_request(path="/echo"), "assertions": _debug_assertions()}
    report = _debug_run(client, account, project, environment["id"], snapshot)

    context = report["context"]
    assert context is not None, report
    assert context["environment"]["id"] == environment["id"]
    assert context["environment"]["kind"] == "test"
    assert context["environment"]["base_url"] == "http://echo:8080"
    assert len(context["snapshot_fingerprint"]) == 64
    assert len(context["input_fingerprint"]) == 64
    # 标记绑定运行创建者，因此与其它主体的预检结果不可能相等；这里先确认两次同一
    # 份内容的报告得到同一标记（来源稳定）。
    again = _debug_run(client, account, project, environment["id"], snapshot)
    assert again["context"]["snapshot_fingerprint"] == context["snapshot_fingerprint"]
    assert again["context"]["input_fingerprint"] == context["input_fingerprint"]


def test_report_payload_never_exposes_the_raw_snapshot_digest(
    client: TestClient, account: dict, project: dict
) -> None:
    """公开报告里不得出现原始 debug_snapshot_hash，也不得出现内容摘要式的字段。

    原始摘要是稳定的内容指纹：谁读到报告，就能拿低熵候选（固定口令、固定 JSON 前缀）
    离线算一遍确认“是不是这一条”。遮蔽了值却留下这个入口，等于遮蔽没做完。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="无原始摘要环境")
    snapshot = {"request": _debug_request(path="/echo"), "assertions": _debug_assertions()}
    digest = client.post(f"{base}/debug-snapshot-digest", json=snapshot)
    assert digest.status_code == 200, digest.text
    raw_hash = digest.json()["hash"]

    report = _debug_run(client, account, project, environment["id"], snapshot)
    payload = json.dumps(report, ensure_ascii=False)
    assert raw_hash not in payload, "报告不得回显原始调试快照摘要"
    assert report["context"]["snapshot_fingerprint"] != raw_hash
    assert report["context"]["input_fingerprint"] != raw_hash


def test_context_fingerprint_differs_across_principals(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一份内容、同一个项目，不同发起主体得到不同标记。

    若两个主体算出同一个标记，标记就成了跨主体的关联键：能从两份报告里认出“这是
    同一份请求”，而报告本身是按项目共享可见的。绑定主体正是为了堵住这条比对路径。
    """
    from app.main import app

    environment = create_environment(client, account, project, name="主体隔离环境")
    snapshot = {"request": _debug_request(path="/echo"), "assertions": []}
    first_report = _debug_run(client, account, project, environment["id"], snapshot)

    connection = migrator_connection()
    second_username = f"it-context-second-{uuid.uuid4().hex[:8]}"
    second_id, _ = seed_account(
        connection, username=second_username, password=PASSWORD, workspace_role=None
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.workspace_memberships (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'editor')",
            (account["workspace_id"], second_id),
        )
    connection.commit()
    second_account = {
        "user_id": second_id,
        "workspace_id": account["workspace_id"],
        "username": second_username,
    }

    try:
        with TestClient(app) as second:
            login = second.post(
                "/api/v1/auth/login",
                json={"username": second_username, "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            second.headers["X-CSRF-Token"] = second.cookies["interface_csrf"]
            run = start_run(
                second,
                second_account,
                project,
                {"environment_id": environment["id"], "debug_snapshot": snapshot},
            )
            assert _run_once(project["pool_id"]) in ("passed", "completed_unchecked")
            second_report = get_report(second, second_account, project, run["id"])
    finally:
        connection.execute("DELETE FROM app.sessions WHERE user_id = %s", (second_id,))
        connection.execute(
            "DELETE FROM app.workspace_memberships WHERE user_id = %s", (second_id,)
        )
        connection.commit()
        connection.execute("DELETE FROM app.users WHERE id = %s", (second_id,))
        connection.commit()
        connection.close()

    assert first_report["context"]["snapshot_fingerprint"] != (
        second_report["context"]["snapshot_fingerprint"]
    )
    assert first_report["context"]["input_fingerprint"] != (
        second_report["context"]["input_fingerprint"]
    )
    # 两个主体的内容标记都不得等于原始快照摘要。
    payload = json.dumps({"first": first_report, "second": second_report}, ensure_ascii=False)
    assert "debug_snapshot_hash" not in payload


def test_input_fingerprint_changes_when_variables_change(
    client: TestClient, account: dict, project: dict
) -> None:
    """普通变量变化只影响输入标记，内容标记保持不变。

    两个标记的分工就在这里：`snapshot_fingerprint` 回答“执行内容是什么”，
    `input_fingerprint` 回答“用哪批输入解析出来的”。请求模板没变、变量变了，只有
    后者应当变化。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="输入标记环境")
    request = {"method": "GET", "path": "/echo?mark={{shared}}", "body_type": "none", "body": ""}
    snapshot = {"request": request, "assertions": []}

    initialized = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"shared": {"type": "string", "text": "first"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert initialized.status_code == 200, initialized.text
    environment = initialized.json()
    first = _debug_run(client, account, project, environment["id"], snapshot)
    updated = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"variables": {"shared": {"type": "string", "text": "second"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text
    second = _debug_run(client, account, project, environment["id"], snapshot)

    assert first["context"]["snapshot_fingerprint"] == second["context"]["snapshot_fingerprint"]
    assert first["context"]["input_fingerprint"] != second["context"]["input_fingerprint"]
