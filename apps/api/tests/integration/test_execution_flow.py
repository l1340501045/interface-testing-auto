"""真实执行闭环集成：独立 worker 领取、受控发送、终态、租约与目标管控。

与 test_api_flow 的分工：那边验证管理 API 的契约与入队；这里让同一进程直接调用
worker 使用的 `claim_job` / `execute_claim`（与 `python -m app.worker` 完全同一份
内核与同一份数据库），对 compose 网络内的受控目标服务发出**真实 HTTP**，再通过
管理 API 读取持久化报告。

直接调用而非另起进程，是为了让「领取 → 发送 → 终态 → 报告」在同一处可观察；
被验证的执行路径、SQL、RLS 与租约逻辑与后台 worker 完全相同。

对应 SC-06（真实执行与持久报告）、SC-07（入参失败不发 HTTP、失败证据）、
SC-08（租约、旧 token、终态拒改、排队超时）、SC-09（目标与生产管控）。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from harness import (
    CONTROLLED_TARGET_ALT_BASE_URL,
    CONTROLLED_TARGET_BASE_URL,
    create_case,
    create_environment,
    get_report,
    migrator_connection,
    project_base,
    publish_case,
    start_run,
)

pytestmark = pytest.mark.integration


# 执行内核按 settings.worker_id 校验租约归属，领取与执行必须用同一个 worker 身份，
# 否则只会得到“租约不属于本 worker”的假失败。两个 worker 的争抢测试靠替换该字段区分。
_DEFAULT_WORKER = "it-worker-1"

# 白名单之外、但**策略上允许**的目标：私网地址不属于禁区，因此“被拒绝”只能归因于
# 白名单本身。用环回地址会让拒绝同时有两重原因（不在白名单 + 本来就是禁区），
# 这样的断言无法证明白名单真的在拦——两种实现（有白名单、没有白名单）都会通过。
OUTSIDE_TARGET = "http://10.9.9.9:9999"


def _settings(worker_id: str = _DEFAULT_WORKER):
    from dataclasses import replace

    from app.config import get_settings

    return replace(get_settings(), worker_id=worker_id)


def _claim(pool_id: str, worker_id: str = _DEFAULT_WORKER, lease_seconds: int = 30):
    """按 worker 的真实方式原子领取一个工作项；返回 claim 或 None。"""
    from app.db import get_session_factory
    from app.services.executor import claim_job

    session = get_session_factory()()
    try:
        claim = claim_job(session, worker_id, [uuid.UUID(pool_id)], lease_seconds)
        session.commit()
        return claim
    finally:
        session.close()


def _execute(claim, worker_id: str = _DEFAULT_WORKER) -> str:
    from app.db import get_session_factory
    from app.services.executor import execute_claim

    return execute_claim(get_session_factory(), _settings(worker_id), claim)


def _run_once(pool_id: str, worker_id: str = _DEFAULT_WORKER) -> str:
    claim = _claim(pool_id, worker_id)
    assert claim is not None, "执行池中应有可领取的工作项"
    return _execute(claim, worker_id)


def _echo_case_request(**overrides) -> dict:
    request = {
        "method": "POST",
        "path": "/echo",
        "query_params": [{"name": "tag", "value": "闭环"}],
        "headers": [{"name": "X-Request-Id", "value": "req-1"}],
        "body_type": "json",
        "body": '{"id":9007199254740993,"amount":25,"name":"abc"}',
    }
    request.update(overrides)
    return request


def _post_response_assertion(assertion_id: str, target_source: str, type_id: str,
                             parameters: dict, selector: list[dict] | None = None) -> dict:
    return {
        "id": assertion_id,
        "target_source": target_source,
        "selector": selector or [],
        "type": type_id,
        "parameters": parameters,
        "severity": "error",
    }


def _plant_legacy_unchecked_report(
    run_id: str,
    *,
    reason_category: str | None = None,
    error_code: str | None = None,
    earlier_sending: bool = False,
) -> None:
    """只在隔离测试库造一条旧 worker 的有损步骤结果，不触发任何 HTTP。"""
    from psycopg.types.json import Jsonb

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.runs SET state = 'finished', outcome = 'completed_unchecked', "
                "reason_category = %s WHERE id = %s",
                (reason_category, run_id),
            )
            cursor.execute("UPDATE app.jobs SET state = 'done' WHERE run_id = %s", (run_id,))
            if earlier_sending:
                cursor.execute(
                    "INSERT INTO app.run_step_attempts "
                    "(id, workspace_id, project_id, run_id, step_key, attempt_no, state, outcome, "
                    "send_intent_at, started_at, request) "
                    "SELECT %s, workspace_id, project_id, id, 'main', 1, 'sending', NULL, "
                    "now(), now(), %s FROM app.runs WHERE id = %s",
                    (
                        uuid.uuid4(),
                        Jsonb({"method": "GET", "url": "http://echo:8080/echo"}),
                        run_id,
                    ),
                )
            cursor.execute(
                "INSERT INTO app.run_step_attempts "
                "(id, workspace_id, project_id, run_id, step_key, attempt_no, state, outcome, "
                "send_intent_at, started_at, finished_at, request, response, error_code) "
                "SELECT %s, workspace_id, project_id, id, 'main', %s, 'finished', 'error', "
                "now(), now(), now(), %s, %s, %s FROM app.runs WHERE id = %s",
                (
                    uuid.uuid4(),
                    2 if earlier_sending else 1,
                    Jsonb({"method": "GET", "url": "http://echo:8080/echo"}),
                    Jsonb({"status": 200, "body": None}),
                    error_code,
                    run_id,
                ),
            )
        connection.commit()
    finally:
        connection.close()


# —— SC-06：真实 HTTP 与持久报告 ——


def test_legacy_unchecked_mapping_is_read_only_and_shared_by_both_endpoints(
    client: TestClient,
    account: dict,
    project: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可信旧记录保留原值，只给最终 main 尝试附同一份兼容解释。"""
    environment = create_environment(client, account, project, name="历史解释环境")
    case = create_case(
        client,
        account,
        project,
        name="历史未校验用例",
        request=_echo_case_request(),
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })
    _plant_legacy_unchecked_report(run["id"])

    from app.services import executor

    sends: list[str] = []
    monkeypatch.setattr(executor, "_send", lambda *_args, **_kwargs: sends.append("unexpected"))
    connection = migrator_connection()
    try:
        before = connection.execute(
            "SELECT to_jsonb(r), "
            "(SELECT jsonb_agg(to_jsonb(s) ORDER BY s.attempt_no) "
            " FROM app.run_step_attempts s WHERE s.run_id = r.id), "
            "(SELECT count(*) FROM app.jobs j WHERE j.run_id = r.id), "
            "(SELECT count(*) FROM app.credential_use_grants), "
            "(SELECT count(*) FROM app.runner_pool_project_grants) "
            "FROM app.runs r WHERE r.id = %s",
            (run["id"],),
        ).fetchone()
    finally:
        connection.close()

    base = project_base(account, project)
    steps_response = client.get(f"{base}/runs/{run['id']}/steps")
    report_response = client.get(f"{base}/runs/{run['id']}/report")
    assert steps_response.status_code == 200, steps_response.text
    assert report_response.status_code == 200, report_response.text
    expected_interpretation = {
        "outcome": "completed_unchecked",
        "reason_code": "legacy_unchecked_mapping",
    }
    assert steps_response.json()[0]["outcome"] == "error"
    assert steps_response.json()[0]["interpretation"] == expected_interpretation
    assert report_response.json()["steps"][0] == steps_response.json()[0]
    assert sends == [], "读取历史报告不得调用执行器发送 HTTP"

    connection = migrator_connection()
    try:
        after = connection.execute(
            "SELECT to_jsonb(r), "
            "(SELECT jsonb_agg(to_jsonb(s) ORDER BY s.attempt_no) "
            " FROM app.run_step_attempts s WHERE s.run_id = r.id), "
            "(SELECT count(*) FROM app.jobs j WHERE j.run_id = r.id), "
            "(SELECT count(*) FROM app.credential_use_grants), "
            "(SELECT count(*) FROM app.runner_pool_project_grants) "
            "FROM app.runs r WHERE r.id = %s",
            (run["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert after == before, "兼容解释读取不得改写运行、步骤，或新增工作项与授权"


@pytest.mark.parametrize(
    ("reason_category", "error_code", "interpreted"),
    [
        ("assertion", "assertion_not_evaluated", True),
        ("assertion", None, False),
        (None, "assertion_not_evaluated", False),
    ],
)
def test_legacy_interpretation_requires_the_complete_reason_pair_on_both_endpoints(
    client: TestClient,
    account: dict,
    project: dict,
    reason_category: str | None,
    error_code: str | None,
    interpreted: bool,
) -> None:
    environment = create_environment(client, account, project, name="历史原因组合环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })
    _plant_legacy_unchecked_report(
        run["id"], reason_category=reason_category, error_code=error_code
    )
    base = project_base(account, project)
    step = client.get(f"{base}/runs/{run['id']}/steps").json()[0]
    report_step = client.get(f"{base}/runs/{run['id']}/report").json()["steps"][0]
    assert (step["interpretation"] is not None) is interpreted
    assert report_step == step


@pytest.mark.parametrize("final_eligible", [True, False])
def test_only_final_attempt_can_be_interpreted_after_an_unresolved_sending_attempt(
    client: TestClient,
    account: dict,
    project: dict,
    final_eligible: bool,
) -> None:
    environment = create_environment(client, account, project, name="历史多尝试环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })
    _plant_legacy_unchecked_report(
        run["id"],
        earlier_sending=True,
        error_code=None if final_eligible else "read_timeout",
    )
    base = project_base(account, project)
    steps = client.get(f"{base}/runs/{run['id']}/steps").json()
    report_steps = client.get(f"{base}/runs/{run['id']}/report").json()["steps"]
    assert report_steps == steps
    assert steps[0]["state"] == "sending"
    assert steps[0]["outcome"] is None
    assert steps[0]["interpretation"] is None
    assert (steps[1]["interpretation"] is not None) is final_eligible


def test_worker_sends_real_http_and_persists_report(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(client, account, project, name="真实执行环境")
    case = create_case(
        client,
        account,
        project,
        name="回显用例",
        request=_echo_case_request(),
        assertions=[
            _post_response_assertion("status-ok", "response.status", "equals",
                                     {"expected": {"type": "number", "text": "200"}}),
            _post_response_assertion(
                "echo-name", "response.body", "equals",
                {"expected": {"type": "string", "text": "abc"}},
                selector=[{"kind": "key", "key": "json"}, {"kind": "key", "key": "name"}],
            ),
            _post_response_assertion(
                "echo-big-int", "response.body", "equals",
                {"expected": {"type": "number", "text": "9007199254740993"}},
                selector=[{"kind": "key", "key": "json"}, {"kind": "key", "key": "id"}],
            ),
            _post_response_assertion(
                "echo-tag", "response.body", "equals",
                {"expected": {"type": "string", "text": "闭环"}},
                selector=[
                    {"kind": "key", "key": "query"},
                    {"kind": "repeat_key", "key": "tag", "occurrence": 0},
                ],
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    outcome = _run_once(project["pool_id"])
    assert outcome == "passed", outcome

    report = get_report(client, account, project, run["id"])
    assert report["run"]["state"] == "finished"
    assert report["run"]["outcome"] == "passed"
    # 真实请求与响应证据都被持久化，重开页面仍可读到。
    assert report["request"]["method"] == "POST"
    assert "/echo" in report["request"]["url"]
    assert report["response"]["status"] == 200
    # 回显正文里带原样返回的长整数，说明请求确实到达受控目标服务。
    assert "9007199254740993" in report["response"]["body"]
    statuses = {item["assertion_id"]: item["status"] for item in report["assertions"]}
    assert statuses == {
        "status-ok": "passed",
        "echo-name": "passed",
        "echo-big-int": "passed",
        "echo-tag": "passed",
    }
    # 步骤证据显示真实耗时，说明确实经历过一次网络往返。
    assert report["steps"] and report["steps"][-1]["state"] == "finished"


def test_big_integer_survives_real_http_round_trip(
    client: TestClient, account: dict, project: dict
) -> None:
    """9007199254740993 经真实 HTTP 与响应断言后仍是原值，不与相邻数混淆。"""
    environment = create_environment(client, account, project, name="无损环境")
    case = create_case(
        client,
        account,
        project,
        name="长整数用例",
        request=_echo_case_request(path="/numbers", method="GET", body_type="none", body=""),
        assertions=[
            _post_response_assertion(
                "big-int", "response.body", "equals",
                {"expected": {"type": "number", "text": "9007199254740993"}},
                selector=[{"kind": "key", "key": "big_integer"}],
            ),
            _post_response_assertion(
                "big-int-text", "response.body", "equals",
                {"expected": {"type": "string", "text": "9007199254740993"}},
                selector=[{"kind": "key", "key": "big_integer_text"}],
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    assert all(item["status"] == "passed" for item in report["assertions"])
    assert "9007199254740993" in report["response"]["body"]


# —— SC-07：入参失败不发 HTTP、响应失败保留证据 ——


def test_each_assertion_is_reported_once_per_phase(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一断言在同一阶段只出现一条结果。

    入参结果在发送意图那一步就已提交（保证“意图与入参校验一起持久化”），
    终态提交若把入参结果再写一遍，报告里同一字段会出现两条一模一样的记录。
    这里同时覆盖两种路径：正常发送（既有入参又有响应断言）和入参拦截
    （只有入参结果，另外补一组未执行的响应结果）。
    """
    environment = create_environment(client, account, project, name="结果去重环境")
    case = create_case(
        client,
        account,
        project,
        name="多阶段断言用例",
        request=_echo_case_request(),
        assertions=[
            _post_response_assertion("status-ok", "response.status", "equals",
                                     {"expected": {"type": "number", "text": "200"}}),
            {
                "id": "amount-range",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "amount"}],
                "type": "range",
                "parameters": {
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "100"},
                    "include_min": False,
                    "include_max": False,
                },
                "severity": "error",
            },
            {
                "id": "name-equals",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "name"}],
                "type": "equals",
                "parameters": {"expected": {"type": "string", "text": "abc"}},
                "severity": "error",
            },
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    keys = [(item["assertion_id"], item["phase"]) for item in report["assertions"]]
    assert len(keys) == len(set(keys)), f"同一断言在同一阶段出现多次：{keys}"
    # 三条断言各出现一次：两条发送前、一条响应后。
    assert sorted(keys) == [
        ("amount-range", "pre_request"),
        ("name-equals", "pre_request"),
        ("status-ok", "post_response"),
    ]


def test_pre_request_failure_blocks_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """入参断言失败必须在发送前拦截：不写发送意图，响应断言记为未执行。"""
    environment = create_environment(client, account, project, name="入参拦截环境")
    case = create_case(
        client,
        account,
        project,
        name="入参断言用例",
        request=_echo_case_request(),
        assertions=[
            {
                "id": "amount-range",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "amount"}],
                "type": "range",
                "parameters": {
                    # 实际入参是 25，故意配成上界 10 让入参断言失败。
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "10"},
                    "include_min": False,
                    "include_max": False,
                },
                "severity": "error",
            },
            _post_response_assertion("status-ok", "response.status", "equals",
                                     {"expected": {"type": "number", "text": "200"}}),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    outcome = _run_once(project["pool_id"])
    assert outcome == "failed", outcome

    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "failed"
    assert report["run"]["reason_category"] == "assertion"
    assert report["steps"][-1]["state"] == "skipped"
    assert report["steps"][-1]["error_code"] == "pre_request_assertion_failed"
    # 没有响应证据，说明并未发出请求；响应断言记为未执行而不是通过。
    assert report["response"] is None
    statuses = {item["assertion_id"]: item["status"] for item in report["assertions"]}
    assert statuses["amount-range"] == "failed"
    assert statuses["status-ok"] == "skipped"


def test_response_failure_keeps_expected_and_actual(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(client, account, project, name="失败证据环境")
    case = create_case(
        client,
        account,
        project,
        name="状态码失败用例",
        request=_echo_case_request(path="/status/503", method="GET", body_type="none", body=""),
        assertions=[
            _post_response_assertion("status-ok", "response.status", "equals",
                                     {"expected": {"type": "number", "text": "200"}}),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "failed"

    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 503, "失败时仍保留真实响应证据"
    failed = report["assertions"][0]
    assert failed["status"] == "failed"
    assert failed["expected"] == "200"
    assert failed["actual"] == "503"


def test_configuration_error_is_distinct_from_assertion_failure(
    client: TestClient, account: dict, project: dict
) -> None:
    """配置错误（区间 min ≥ max）与业务断言失败必须区分，且不能被当成通过。"""
    environment = create_environment(client, account, project, name="配置错误环境")
    case = create_case(
        client,
        account,
        project,
        name="配置错误用例",
        request=_echo_case_request(),
        assertions=[
            _post_response_assertion(
                "bad-range", "response.body", "range",
                {"min": {"type": "number", "text": "100"},
                 "max": {"type": "number", "text": "0"},
                 "include_min": False, "include_max": False},
                selector=[{"kind": "key", "key": "amount"}],
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "error"
    assert report["run"]["reason_category"] == "configuration"
    assert report["assertions"][0]["status"] == "error"


def test_response_not_checked_when_only_pre_request_assertions(
    client: TestClient, account: dict, project: dict
) -> None:
    """只有入参断言时，报告必须显示“响应未校验”，不能算接口健康通过。"""
    environment = create_environment(client, account, project, name="未校验环境")
    case = create_case(
        client,
        account,
        project,
        name="仅入参用例",
        request=_echo_case_request(),
        assertions=[
            {
                "id": "amount-range",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "amount"}],
                "type": "range",
                "parameters": {
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "100"},
                    "include_min": False,
                    "include_max": False,
                },
                "severity": "error",
            },
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    outcome = _run_once(project["pool_id"])
    report = get_report(client, account, project, run["id"])
    assert outcome == "completed_unchecked", report
    assert report["run"]["outcome"] == "completed_unchecked"
    assert report["steps"][-1]["outcome"] == "completed_unchecked"
    assert _send_intent_rows(run["id"])[-1][2] == "completed_unchecked"
    assert report["response"] is not None, "请求确实发出并收到了响应"
    steps = client.get(f"{project_base(account, project)}/runs/{run['id']}/steps")
    assert steps.status_code == 200, steps.text
    assert steps.json()[-1]["outcome"] == "completed_unchecked"


@pytest.mark.parametrize("status", [200, 503])
def test_response_without_assertions_stays_unchecked_for_any_http_status(
    client: TestClient, account: dict, project: dict, status: int
) -> None:
    """HTTP 状态只证明收到响应；没有响应断言时，200 与 503 都不能冒充健康结论。"""
    environment = create_environment(client, account, project, name=f"无断言{status}环境")
    case = create_case(
        client,
        account,
        project,
        name=f"无断言{status}用例",
        request=_echo_case_request(
            method="GET", path=f"/status/{status}", body_type="none", body=""
        ),
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "completed_unchecked"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "completed_unchecked"
    assert report["steps"][-1]["outcome"] == "completed_unchecked"
    assert report["steps"][-1]["interpretation"] is None
    assert report["response"]["status"] == status
    assert _send_intent_rows(run["id"])[-1][2] == "completed_unchecked"


def test_a_required_condition_that_could_not_run_is_not_a_pass(
    client: TestClient, account: dict, project: dict
) -> None:
    """一条必需条件没跑成时，另一条通过也不算整体通过（多条件默认全部满足）。

    入参侧的条件配在 `request.body` 上，而请求根本没有正文：该来源不存在，条件被
    跳过。响应侧还有一条确实通过的条件——只按“至少有一条响应条件被执行过”判定就会
    显示接口健康通过，而那条没跑成的检查谁也没证明它满足。
    """
    environment = create_environment(client, account, project, name="未跑完环境")
    case = create_case(
        client,
        account,
        project,
        name="必需条件未跑完用例",
        request=_echo_case_request(body_type="none", body=""),
        assertions=[
            {
                "id": "amount-range",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "amount"}],
                "type": "range",
                "parameters": {
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "100"},
                    "include_min": False,
                    "include_max": False,
                },
                "severity": "error",
            },
            _post_response_assertion(
                "status-ok",
                "response.status",
                "equals",
                {"expected": {"type": "number", "text": "200"}},
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    outcome = _run_once(project["pool_id"])
    report = get_report(client, account, project, run["id"])
    assert outcome == "completed_unchecked", report
    assert report["run"]["outcome"] == "completed_unchecked"
    assert report["run"]["reason_category"] == "assertion", "报告要读得出为什么不是通过"
    assert report["steps"][-1]["outcome"] == "completed_unchecked"
    assert _send_intent_rows(run["id"])[-1][2] == "completed_unchecked"
    assert report["steps"][-1]["error_code"] == "assertion_not_evaluated"
    assert {item["assertion_id"]: item["status"] for item in report["assertions"]} == {
        "amount-range": "skipped",
        "status-ok": "passed",
    }


def test_a_condition_on_the_parsed_body_is_not_silently_skipped(
    client: TestClient, account: dict, project: dict
) -> None:
    """用例对解析后的正文提了条件、目标却返回纯文本：正文省略、条件报错、整体不通过。

    目标把正文标成 `text/plain`，内容也不是 `{`／`[` 开头的残缺 JSON。只看类型头与
    首字符时它按纯文本入库，`response.body` 这个来源不存在，条件被静默跳过——配置在
    解析后正文上的检查于是从未真正执行，报告却是 passed。用例自己表达了“正文应当是
    JSON”，这条预期必须参与格式判定：无法解析就整段省略（报告里带原因），条件报错，
    状态码等安全来源照常求值。
    """
    environment = create_environment(client, account, project, name="非JSON环境")
    case = create_case(
        client,
        account,
        project,
        name="正文格式不符用例",
        request={
            "method": "GET",
            "path": "/slow-body",
            "query_params": [],
            "headers": [],
            "body_type": "none",
            "body": "",
        },
        assertions=[
            _post_response_assertion(
                "status-ok",
                "response.status",
                "equals",
                {"expected": {"type": "number", "text": "200"}},
            ),
            _post_response_assertion(
                "body-field",
                "response.body",
                "equals",
                {"expected": {"type": "string", "text": "ok"}},
                selector=[{"kind": "key", "key": "message"}],
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["body"] is None
    assert report["response"]["body_omitted_reason"] == "unparsable_json_body"
    assert {item["assertion_id"]: item["status"] for item in report["assertions"]} == {
        "status-ok": "passed",
        "body-field": "error",
    }
    body_result = next(
        item for item in report["assertions"] if item["assertion_id"] == "body-field"
    )
    assert body_result["reason_code"] == "source_omitted"
    assert body_result["actual"] is None, "不允许把被省略的原文带进结果"


def test_unresolved_variable_fails_before_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """未定义变量必须在发送前失败，不把 {{...}} 当字面量发出去。"""
    environment = create_environment(client, account, project, name="变量环境")
    case = create_case(
        client,
        account,
        project,
        name="未定义变量用例",
        request=_echo_case_request(query_params=[{"name": "tag", "value": "{{未定义变量}}"}]),
    )
    version = publish_case(client, account, project, case["id"])
    rejected = client.post(
        f"{project_base(account, project)}/runs",
        json={"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "variable_undefined"
    listed = client.get(f"{project_base(account, project)}/runs")
    assert listed.status_code == 200
    assert listed.json() == [], "普通变量缺失必须在创建 Run/Job 前确定拒绝"


def test_environment_variable_is_resolved_into_request(
    client: TestClient, account: dict, project: dict
) -> None:
    """环境普通变量参与请求解析：前端只填名，值随环境切换。"""
    environment = create_environment(client, account, project, name="变量生效环境")
    # 普通变量按 ValueLiteral 契约提交：文本 "" 与数字 0 不混淆，也不允许秘密。
    updated = client.patch(
        f"{project_base(account, project)}/environments/{environment['id']}",
        json={"variables": {"租户": {"type": "string", "text": "alpha"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text

    case = create_case(
        client,
        account,
        project,
        name="变量取值用例",
        request=_echo_case_request(query_params=[{"name": "tag", "value": "{{租户}}"}]),
        assertions=[
            _post_response_assertion(
                "tag-value", "response.body", "equals",
                {"expected": {"type": "string", "text": "alpha"}},
                selector=[
                    {"kind": "key", "key": "query"},
                    {"kind": "repeat_key", "key": "tag", "occurrence": 0},
                ],
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    assert _run_once(project["pool_id"]) == "passed"
    assert get_report(client, account, project, run["id"])["assertions"][0]["status"] == "passed"


# —— JV1：结构化正文按字段绑定 ——


def _run_with_variables(
    client: TestClient,
    account: dict,
    project: dict,
    variables: dict,
    *,
    name: str,
    request: dict,
    assertions: list[dict] | None = None,
) -> tuple[str, dict]:
    """写入环境普通变量 → 建用例 → 发布 → 真实执行 → 返回结论与报告。"""
    environment = create_environment(client, account, project, name=f"{name}-环境")
    updated = client.patch(
        f"{project_base(account, project)}/environments/{environment['id']}",
        json={"variables": variables},
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text
    case = create_case(
        client,
        account,
        project,
        name=name,
        request=request,
        assertions=assertions or [],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })
    outcome = _run_once(project["pool_id"])
    return outcome, get_report(client, account, project, run["id"])


def _echo_key(key: str) -> list[dict]:
    return [{"kind": "key", "key": "json"}, {"kind": "key", "key": key}]


def test_a_number_variable_reaches_the_target_as_a_literal_number(
    client: TestClient, account: dict, project: dict
) -> None:
    """真实复现：数字变量 `shared=1` 曾以字符串 "1" 进入 JSON 正文。

    准备后的 score 是字符串时，入参的开区间断言判 `type_error`，真实目标收到的也是
    "1" 而不是 1。两个方向一起锁死：入参断言按**最终正文**求值并通过，且目标回显的
    字段确实是数字。长整数同批送出，证明绑定没有经过二进制浮点。
    """
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {"shared": {"type": "number", "text": "1"}},
        name="数值JSON变量用例",
        request=_echo_case_request(
            body='{"id":9007199254740993,"score":"{{shared}}","label":"abc"}'
        ),
        assertions=[
            {
                "id": "body-score-range",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "score"}],
                "type": "range",
                "parameters": {
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "100"},
                    "include_min": False,
                    "include_max": False,
                },
                "severity": "error",
            },
            _post_response_assertion(
                "echo-score-type", "response.body", "is_type",
                {"type": "integer"}, selector=_echo_key("score"),
            ),
            _post_response_assertion(
                "echo-score-value", "response.body", "equals",
                {"expected": {"type": "number", "text": "1"}}, selector=_echo_key("score"),
            ),
            _post_response_assertion(
                "echo-id-value", "response.body", "equals",
                {"expected": {"type": "number", "text": "9007199254740993"}},
                selector=_echo_key("id"),
            ),
        ],
    )

    assert {item["assertion_id"]: item["status"] for item in report["assertions"]} == {
        "body-score-range": "passed",
        "echo-score-type": "passed",
        "echo-score-value": "passed",
        "echo-id-value": "passed",
    }, [(item["assertion_id"], item["status"], item.get("reason_code")) for item in report["assertions"]]
    assert outcome == "passed"
    # 线上真正发出去的那份正文里，score 不带引号。
    assert '"score":1' in report["request"]["body"]


def test_structured_variables_keep_their_types_at_the_target(
    client: TestClient, account: dict, project: dict
) -> None:
    """布尔、null、对象与数组在真实往返后仍是原来的类型；嵌入文本仍是字符串。"""
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {
            "flag": {"type": "boolean", "value": True},
            "nothing": {"type": "null"},
            "obj": {"type": "json", "text": '{"k": 9007199254740993}'},
            "arr": {"type": "json", "text": "[1, 2]"},
            "num": {"type": "number", "text": "1"},
        },
        name="结构类型用例",
        request=_echo_case_request(
            body=(
                '{"flag":"{{flag}}","nothing":"{{nothing}}","obj":"{{obj}}",'
                '"arr":"{{arr}}","embedded":"pre{{num}}post"}'
            )
        ),
        assertions=[
            _post_response_assertion("flag-type", "response.body", "is_type",
                                     {"type": "boolean"}, selector=_echo_key("flag")),
            _post_response_assertion("nothing-null", "response.body", "is_null",
                                     {}, selector=_echo_key("nothing")),
            _post_response_assertion("obj-type", "response.body", "is_type",
                                     {"type": "object"}, selector=_echo_key("obj")),
            _post_response_assertion("obj-value", "response.body", "equals",
                                     {"expected": {"type": "number", "text": "9007199254740993"}},
                                     selector=[*_echo_key("obj"), {"kind": "key", "key": "k"}]),
            _post_response_assertion("arr-type", "response.body", "is_type",
                                     {"type": "array"}, selector=_echo_key("arr")),
            _post_response_assertion("arr-value", "response.body", "equals",
                                     {"expected": {"type": "json", "text": "[1,2]"}},
                                     selector=_echo_key("arr")),
            _post_response_assertion("embedded-string", "response.body", "equals",
                                     {"expected": {"type": "string", "text": "pre1post"}},
                                     selector=_echo_key("embedded")),
        ],
    )

    assert all(item["status"] == "passed" for item in report["assertions"]), [
        (item["assertion_id"], item["status"], item.get("reason_code"))
        for item in report["assertions"]
    ]
    assert outcome == "passed"


def test_a_string_variable_with_quotes_still_reaches_the_target_as_valid_json(
    client: TestClient, account: dict, project: dict
) -> None:
    """取值含引号与反斜线时，整段替换会拼出残缺正文；按 JSON 规则转义才是正文。"""
    value = 'a"b\\c'
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {"quote": {"type": "string", "text": value}},
        name="字符串转义用例",
        request=_echo_case_request(body='{"label":"{{quote}}"}'),
        assertions=[
            # 目标解析成功才会有 json 字段树；解析失败时报文字段仍在，这条会判缺失。
            _post_response_assertion("label-type", "response.body", "is_type",
                                     {"type": "string"}, selector=_echo_key("label")),
            _post_response_assertion("label-value", "response.body", "equals",
                                     {"expected": {"type": "string", "text": value}},
                                     selector=_echo_key("label")),
        ],
    )

    assert outcome == "passed"
    assert all(item["status"] == "passed" for item in report["assertions"])


def test_an_unequal_object_in_the_request_body_sends_nothing(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主审复现的假通过路径端到端锁死：只差一个字段的对象不得判相等，也不得发出请求。

    变量 obj 为 {"name":"x","value":1,"enabled":false}，正文 {"payload":"{{obj}}"}，
    期望 {"name":"x","value":1,"enabled":true}。旧实现按形状把两侧都缩减成
    name/value，enabled 被丢弃后判 passed 并发请求——断言通过而内容不相等。
    """
    from app.services import executor

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    environment = create_environment(client, account, project, name="对象不等拦截环境")
    updated = client.patch(
        f"{project_base(account, project)}/environments/{environment['id']}",
        json={"variables": {"obj": {"type": "json", "text": '{"name":"x","value":1,"enabled":false}'}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text
    case = create_case(
        client,
        account,
        project,
        name="入参对象不等用例",
        request=_echo_case_request(body='{"payload":"{{obj}}"}'),
        assertions=[
            {
                "id": "payload-equals",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "payload"}],
                "type": "equals",
                "parameters": {"expected": {"type": "json", "text": '{"name":"x","value":1,"enabled":true}'}},
                "severity": "error",
            },
            _post_response_assertion("status-ok", "response.status", "equals",
                                     {"expected": {"type": "number", "text": "200"}}),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    outcome = _run_once(project["pool_id"])
    assert outcome == "failed", outcome

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "assertion"
    assert report["steps"][-1]["error_code"] == "pre_request_assertion_failed"
    assert report["response"] is None
    assert sent == [], "入参对象不等必须在发送前拦截，一个请求都不发出"
    statuses = {item["assertion_id"]: item["status"] for item in report["assertions"]}
    assert statuses == {"payload-equals": "failed", "status-ok": "skipped"}
    # 证据里两侧都必须留着第三个字段，不能再被缩减成 name/value 两个键。
    failed = next(item for item in report["assertions"] if item["assertion_id"] == "payload-equals")
    assert set(failed["actual"]) == {"name", "value", "enabled"}
    assert set(failed["expected"]) == {"name", "value", "enabled"}
    assert failed["actual"]["enabled"] is False
    assert failed["expected"]["enabled"] is True


def test_an_equal_object_in_the_request_body_is_sent_whole(
    client: TestClient, account: dict, project: dict
) -> None:
    """相等时断言必须真的通过，且目标收到的是完整对象——不是靠丢字段换来的静默通过。"""
    body_object = '{"name":"x","value":1,"enabled":true}'
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {"obj": {"type": "json", "text": body_object}},
        name="入参对象相等用例",
        request=_echo_case_request(body='{"payload":"{{obj}}"}'),
        assertions=[
            {
                "id": "payload-equals",
                "target_source": "request.body",
                "selector": [{"kind": "key", "key": "payload"}],
                "type": "equals",
                "parameters": {"expected": {"type": "json", "text": body_object}},
                "severity": "error",
            },
            _post_response_assertion("echo-payload", "response.body", "equals",
                                     {"expected": {"type": "json", "text": body_object}},
                                     selector=_echo_key("payload")),
        ],
    )

    assert outcome == "passed"
    assert {item["assertion_id"]: item["status"] for item in report["assertions"]} == {
        "payload-equals": "passed",
        "echo-payload": "passed",
    }, [(item["assertion_id"], item["status"], item.get("reason_code")) for item in report["assertions"]]
    # 上线的那份正文里第三个字段确实在，目标回显也因此逐字段相等。
    assert '"enabled":true' in report["request"]["body"]


def test_a_form_value_with_separators_reaches_the_target_as_one_field(
    client: TestClient, account: dict, project: dict
) -> None:
    """真实复现：表单取值 `a&b=c` 在线解码后变成 q=a 与 b=c 两项。

    断言读的是**目标按表单协议解码后的字段列表**：项数与取值同时核对，才能区分
    “取值里的 & 没被当成分隔符”与“被当成分隔符但看起来差不多”。
    """
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {"value": {"type": "string", "text": "a&b=c"}},
        name="表单特殊字符用例",
        request=_echo_case_request(path="/form", body_type="form", body="q={{value}}"),
        assertions=[
            _post_response_assertion("field-count", "response.body", "length_equals",
                                     {"expected": {"type": "number", "text": "1"}},
                                     selector=[{"kind": "key", "key": "fields"}]),
            _post_response_assertion(
                "field-value", "response.body", "equals",
                {"expected": {"type": "string", "text": "a&b=c"}},
                selector=[{"kind": "key", "key": "fields"},
                          {"kind": "repeat_key", "key": "q", "occurrence": 0}],
            ),
        ],
    )

    assert outcome == "passed"
    assert all(item["status"] == "passed" for item in report["assertions"]), [
        (item["assertion_id"], item["status"], item.get("reason_code"))
        for item in report["assertions"]
    ]


def test_repeated_form_fields_survive_with_their_order_and_count(
    client: TestClient, account: dict, project: dict
) -> None:
    """重复字段必须原样保留：折叠成一项会让“同名参数取第几次”失去意义。

    取值特意带分隔符：若绑定退回整段文本替换，`x&a=9` 会被拆成两项、总项数变成 5，
    项数断言先红。否则这条用例在“逐字段绑定”与“整段替换”下都成立，等于没测。
    """
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {"value": {"type": "string", "text": "x&a=9"}},
        name="表单重复字段用例",
        request=_echo_case_request(
            path="/form", body_type="form", body="a=1&a=2&b={{value}}&a=3"
        ),
        assertions=[
            _post_response_assertion("field-count", "response.body", "length_equals",
                                     {"expected": {"type": "number", "text": "4"}},
                                     selector=[{"kind": "key", "key": "fields"}]),
            _post_response_assertion(
                "third-field", "response.body", "equals",
                {"expected": {"type": "string", "text": "x&a=9"}},
                selector=[{"kind": "key", "key": "fields"},
                          {"kind": "repeat_key", "key": "b", "occurrence": 0}],
            ),
        ],
    )

    assert outcome == "passed"
    assert all(item["status"] == "passed" for item in report["assertions"])


def test_a_body_without_variables_reaches_the_target_unchanged(
    client: TestClient, account: dict, project: dict
) -> None:
    """无变量的正文原文直发：排版、键顺序与重复写法都必须与用户写的一致。

    这条用例没有任何变量（环境也没有变量），是“原有请求不受这次改动影响”的
    真实往返证据：正文里保留了空格、换行与两个 `note` 键。
    """
    body = '{ "amount" : 25 ,\n  "note": "第一个", "note": "第二个" }'
    outcome, report = _run_with_variables(
        client,
        account,
        project,
        {},
        name="无变量原文用例",
        request=_echo_case_request(body=body),
        assertions=[
            _post_response_assertion("body-unchanged", "response.body", "equals",
                                     {"expected": {"type": "string", "text": body}},
                                     selector=[{"kind": "key", "key": "body_text"}]),
        ],
    )

    assert outcome == "passed"
    assert report["assertions"][0]["status"] == "passed"


def test_an_unknown_variable_in_the_body_sends_nothing(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正文里的未知变量必须在发送前失败：一个请求都不发出，响应为空。"""
    from app.services import executor

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    environment = create_environment(client, account, project, name="正文未知变量用例-环境")
    case = create_case(
        client,
        account,
        project,
        name="正文未知变量用例",
        request=_echo_case_request(body='{"a":"{{未定义变量}}"}'),
    )
    version = publish_case(client, account, project, case["id"])
    rejected = client.post(
        f"{project_base(account, project)}/runs",
        json={"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "variable_undefined"
    assert sent == [], "变量未解析的正文不得发出任何请求"


def test_path_assertion_is_evaluated_against_the_prepared_path(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径里带变量时，入参断言要对着**真正上线**的那条路径求值。

    `path=/{{operation}}` 发出去的是 `/echo`。取请求模板去做断言，两条结论会正好反过
    来：核对实际路径被判失败、而核对一个从未出现在线上的字符串被判通过——最该拦住
    写错路径的那道门反而在放行。这里两个方向都用真实执行验证一次，并确认“判失败”那
    一条是在发送之前拦下的（一个请求都没有发出）。
    """
    from app.services import executor

    environment = create_environment(client, account, project, name="路径断言环境")
    updated = client.patch(
        f"{project_base(account, project)}/environments/{environment['id']}",
        json={"variables": {"operation": {"type": "string", "text": "echo"}}},
        headers={"If-Match": str(environment["rev"])},
    )
    assert updated.status_code == 200, updated.text

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    def run_with_path_assertion(expected: str) -> tuple[str, dict]:
        case = create_case(
            client,
            account,
            project,
            name=f"路径断言用例-{expected}",
            request=_echo_case_request(
                path="/{{operation}}", query_params=[], headers=[], body_type="none", body=""
            ),
            assertions=[
                _post_response_assertion(
                    "path-value", "request.path", "equals",
                    {"expected": {"type": "string", "text": expected}},
                ),
                _post_response_assertion(
                    "status-ok", "response.status", "equals",
                    {"expected": {"type": "number", "text": "200"}},
                ),
            ],
        )
        version = publish_case(client, account, project, case["id"])
        run = start_run(client, account, project, {
            "environment_id": environment["id"],
            "case_version_id": version["id"],
        })
        outcome = _run_once(project["pool_id"])
        return outcome, get_report(client, account, project, run["id"])

    # 正：核对实际发出去的那条路径 —— 通过，且请求确实发出。
    outcome, report = run_with_path_assertion("/echo")
    assert outcome == "passed", outcome
    by_id = {item["assertion_id"]: item for item in report["assertions"]}
    assert by_id["path-value"]["status"] == "passed", by_id["path-value"]
    assert sent == [f"{CONTROLLED_TARGET_BASE_URL}/echo"], sent

    # 反：核对自己写的模板 —— 判失败，且发生在发送之前（请求数不增加）。
    outcome, report = run_with_path_assertion("/{{operation}}")
    assert outcome == "failed", outcome
    by_id = {item["assertion_id"]: item for item in report["assertions"]}
    assert by_id["path-value"]["status"] == "failed", by_id["path-value"]
    assert by_id["path-value"]["actual"] == "/echo", "报告里要留下实际取值，便于定位"
    assert [step["error_code"] for step in report["steps"]] == ["pre_request_assertion_failed"]
    assert len(sent) == 1, "入参断言失败必须在发送之前拦住"


# —— SC-03：同一用例、两个受控测试环境，真实发往各自服务 ——


def test_same_case_runs_against_two_environments_and_each_target_responds(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一已发布版本在两个环境上各执行一次，物理目标是两个不同的受控服务。

    两个环境的差异全部来自环境自身：服务地址与普通变量。用例只有一份，不复制
    也不改写。判定“确实发到了各自的服务”用的是响应正文里由服务端给出的实例名，
    不是环境名，也不是提交顺序推断出来的结论。
    """
    primary = create_environment(
        client, account, project, name="测试环境甲", base_url=CONTROLLED_TARGET_BASE_URL
    )
    secondary = create_environment(
        client, account, project, name="测试环境乙", base_url=CONTROLLED_TARGET_ALT_BASE_URL
    )
    for environment, tenant in ((primary, "alpha"), (secondary, "beta")):
        updated = client.patch(
            f"{project_base(account, project)}/environments/{environment['id']}",
            json={"variables": {"租户": {"type": "string", "text": tenant}}},
            headers={"If-Match": str(environment["rev"])},
        )
        assert updated.status_code == 200, updated.text

    case = create_case(
        client,
        account,
        project,
        name="两环境共用用例",
        request=_echo_case_request(query_params=[{"name": "tag", "value": "{{租户}}"}]),
        assertions=[
            _post_response_assertion(
                "status-ok", "response.status", "equals",
                {"expected": {"type": "number", "text": "200"}},
            ),
        ],
    )
    version = publish_case(client, account, project, case["id"])

    observed: list[tuple[str, str]] = []
    for environment, tenant, expected_service in (
        (primary, "alpha", "controlled-target"),
        (secondary, "beta", "controlled-target-alt"),
    ):
        run = start_run(client, account, project, {
            "environment_id": environment["id"],
            "case_version_id": version["id"],
        })
        assert _run_once(project["pool_id"]) == "passed"
        report = get_report(client, account, project, run["id"])
        assert report["assertions"][0]["status"] == "passed"

        body = json.loads(report["response"]["body"])
        assert body["service"] == expected_service, (
            f"环境「{environment['name']}」应真实发往 {expected_service}"
        )
        assert body["query"] == [{"name": "tag", "value": tenant}], (
            "环境普通变量只在本环境解析，不能沿用另一个环境的值"
        )
        observed.append((body["service"], body["query"][0]["value"]))

    # 两次执行的结果必须彼此不同：既没有复用同一个目标，也没有把上一次的值缓存下来。
    assert len(set(observed)) == 2, observed


# —— SC-08：租约、旧 token、终态拒改、截止时间 ——


def test_two_workers_race_and_only_one_claims(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(client, account, project, name="争抢环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    first = _claim(project["pool_id"], worker_id="worker-a")
    assert first is not None
    # 同一工作项已被租约锁定，第二个 worker 在同一时刻领不到它。
    second = _claim(project["pool_id"], worker_id="worker-b")
    assert second is None, "同一工作项不能被两个 worker 同时领取"
    _execute(first, "worker-a")
    assert get_report(client, account, project, run["id"])["run"]["outcome"] == "completed_unchecked"


def test_stale_fencing_token_cannot_write_result(
    client: TestClient, account: dict, project: dict
) -> None:
    """租约过期被重新领取后，旧 token 的执行结果不得覆写当前状态。"""
    environment = create_environment(client, account, project, name="旧令牌环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    stale_claim = _claim(project["pool_id"], worker_id="worker-old", lease_seconds=-1)
    assert stale_claim is not None, "租约立即失效用于模拟被抢占"
    # 另一个 worker 凭已过期的租约重新领取，fencing token 递增。
    fresh_claim = _claim(project["pool_id"], worker_id="worker-new")
    assert fresh_claim is not None
    assert fresh_claim.fencing_token > stale_claim.fencing_token

    assert _execute(stale_claim, "worker-old") == "stale", "旧 token 必须被拒绝"

    # 用迁移身份读取：运行角色受 RLS 约束，未绑定租户时读不到这一行，
    # 会让“旧 token 没写终态”的断言永远成立而失去验证意义。
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, outcome FROM app.runs WHERE id = %s", (run["id"],)
            )
            state = cursor.fetchone()
        connection.commit()
    finally:
        connection.close()
    assert state is not None
    assert state[0] != "finished" or state[1] is None, "旧 token 不得写入终态"

    # 当前有效 token 仍可正常完成；必须用实际持有租约的 worker 身份执行，
    # 租约校验同时比对 leased_by，换成别的 worker 名只会得到无意义的 stale。
    assert _execute(fresh_claim, "worker-new") in ("completed_unchecked", "passed")


def test_expired_lease_with_current_token_cannot_advance_or_commit(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """租约到期即失效：token 没变、也还没被接管的领取不能推进状态，更不能提交终态。

    fencing token 相同不代表仍然持有工作项——租约本身就是有效期。若状态推进与终态
    提交只比对 token，一个已经过期的领取仍会写结果，而此刻另一台机器可能刚开始执行
    同一条运行。上一条用例里旧 token 与过期租约同时不成立，分不清是哪一条拦住的；
    这里只回拨 `lease_until`、保持 token 不变，单独验证租约条件本身在起作用。

    拒绝之后必须由既定恢复路径收敛：重新领取（token 递增）后正常运行，不挂住任务。
    """
    from app.services import executor

    environment = create_environment(client, account, project, name="租约到期环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    claim = _claim(project["pool_id"], worker_id="worker-expired")
    assert claim is not None

    delivered: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        delivered.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    _expire_lease(run["id"])
    # token 仍是当前值，唯一变化是租约到期——状态推进必须因此被拒。
    assert _execute(claim, "worker-expired") == "stale", "过期租约不得推进状态"
    assert delivered == [], "过期租约不得发出任何请求"

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT state, outcome FROM app.runs WHERE id = %s", (run["id"],))
            state = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM app.run_step_attempts WHERE run_id = %s", (run["id"],)
            )
            attempts = cursor.fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    assert state[0] != "finished" or state[1] is None, "过期租约不得提交终态"
    assert attempts == 0, "过期租约不得写入尝试记录"

    # 被拒绝的陈旧领取不挂住任务：重新领取后按正常路径收敛。
    fresh_claim = _claim(project["pool_id"], worker_id="worker-next")
    assert fresh_claim is not None, "过期租约必须可被重新领取"
    assert fresh_claim.fencing_token > claim.fencing_token
    assert _execute(fresh_claim, "worker-next") in ("completed_unchecked", "passed")
    assert len(delivered) == 1, "新领取应正常发出一次请求"


def test_terminal_state_is_not_overwritten(
    client: TestClient, account: dict, project: dict
) -> None:
    """取消后的运行已进入终态，worker 不得再发出请求或改写结果。"""
    environment = create_environment(client, account, project, name="终态环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })
    claim = _claim(project["pool_id"])
    assert claim is not None

    canceled = client.post(f"{project_base(account, project)}/runs/{run['id']}/cancel")
    assert canceled.status_code == 200, canceled.text

    assert _execute(claim) == "canceled"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "canceled"
    assert report["response"] is None, "取消后不得产生任何目标副作用"
    assert report["steps"] == [], "发送前取消允许没有步骤，读取层不能补造尝试"
    steps = client.get(f"{project_base(account, project)}/runs/{run['id']}/steps")
    assert steps.status_code == 200, steps.text
    assert steps.json() == []


def test_queue_deadline_exceeded_does_not_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """排队超时不再发出请求，终态为超时而不是失败或通过。"""
    environment = create_environment(client, account, project, name="排队超时环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.runs SET queue_deadline_at = now() - interval '1 minute' WHERE id = %s",
                (run["id"],),
            )
        connection.commit()
    finally:
        connection.close()

    assert _run_once(project["pool_id"]) == "timed_out"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "timed_out"
    assert report["response"] is None
    assert report["steps"][-1]["error_code"] == "queue_deadline_exceeded"


def test_write_side_effect_timeout_is_interrupted_and_not_replayed(
    client: TestClient, account: dict, project: dict
) -> None:
    """写副作用请求在读取超时后结果不明：记为 interrupted，不自动重放。

    网络中断与“断言失败”不同：请求可能已经送达并对目标产生了副作用，平台无法
    从超时本身判断对方是否处理成功。此时若按普通失败处理或自动重试，就可能造成
    重复写入。执行契约要求这类结果进入 interrupted 交由人工确认，且不换用新
    凭证或新租约继续执行。
    """
    from dataclasses import replace as _replace

    from app.config import get_settings
    from app.db import get_session_factory
    from app.services.executor import execute_claim

    environment = create_environment(client, account, project, name="结果不明环境")
    case = create_case(
        client,
        account,
        project,
        name="写操作超时用例",
        # 受控目标的 /delay 专用于超时分类；延迟远大于下面的读取超时。
        request={
            "method": "GET",
            "path": "/delay/1500",
            "query_params": [],
            "headers": [],
            "body_type": "none",
            "body": "",
        },
    )
    version = publish_case(client, account, project, case["id"], side_effect="write")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    claim = _claim(project["pool_id"])
    assert claim is not None, "执行池中应有可领取的工作项"
    settings = _replace(get_settings(), worker_id=_DEFAULT_WORKER, http_read_timeout=0.3)
    assert execute_claim(get_session_factory(), settings, claim) == "interrupted"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "interrupted"
    assert report["steps"][-1]["error_code"] == "write_result_unknown"
    # 终态已固定：不得再有可领取的工作项被自动重放。
    assert _claim(project["pool_id"]) is None


def test_read_side_effect_timeout_stays_a_plain_network_error(
    client: TestClient, account: dict, project: dict
) -> None:
    """同样的超时，声明为只读时是普通网络错误，不升级为“结果不明”。"""
    from dataclasses import replace as _replace

    from app.config import get_settings
    from app.db import get_session_factory
    from app.services.executor import execute_claim

    environment = create_environment(client, account, project, name="只读超时环境")
    case = create_case(
        client,
        account,
        project,
        name="只读超时用例",
        request={
            "method": "GET",
            "path": "/delay/1500",
            "query_params": [],
            "headers": [],
            "body_type": "none",
            "body": "",
        },
    )
    version = publish_case(client, account, project, case["id"], side_effect="read")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    claim = _claim(project["pool_id"])
    assert claim is not None, "执行池中应有可领取的工作项"
    settings = _replace(get_settings(), worker_id=_DEFAULT_WORKER, http_read_timeout=0.3)
    assert execute_claim(get_session_factory(), settings, claim) == "error"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "error"
    assert report["steps"][-1]["error_code"] == "read_timeout"


def test_pool_grant_revoked_after_enqueue_blocks_the_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """授权在入队之后被撤销：已排队的运行也不得再发出请求。

    创建运行时校验一次是不够的。工作项在入队与被领取之间可能已经撤权，而运行快照
    会把创建时的结论一直保留下去；若执行时沿用快照，撤权就只能拦住“之后新建的运行”，
    对已经在队列里的运行完全无效——撤权成了假动作，且后果是真实 HTTP 仍然发出。
    """
    environment = create_environment(client, account, project, name="入队后撤权环境")
    case = create_case(
        client,
        account,
        project,
        name="入队后撤权用例",
        request=_echo_case_request(),
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    # 入队成功之后、worker 领取之前撤销执行池对本项目的授权。
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.runner_pool_project_grants SET status = 'revoked' "
                "WHERE pool_id = %s AND project_id = %s",
                (project["pool_id"], project["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    claim = _claim(project["pool_id"])
    assert claim is not None, "工作项已入队"

    assert _execute(claim) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "policy"
    assert [step["error_code"] for step in report["steps"]] == ["pool_not_granted"]
    # 关键断言：确实没有发出请求，受控目标没有收到任何内容。
    assert report["response"] is None


def test_whitelist_edit_through_admin_api_gates_real_execution(
    client: TestClient, account: dict, project: dict
) -> None:
    """管理入口改的白名单必须真的管住发送，而不是只改一份展示用的配置。

    两段都要验：改窄之后新建的运行在创建时就被拒；已经排在队列里的运行在 worker
    发送前被拦下。只验一段会漏掉“配置改了但执行路径读的是别的来源”这种假修复。
    """
    environment = create_environment(client, account, project, name="白名单收窄环境")
    queued = start_run(
        client,
        account,
        project,
        {
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
        },
    )

    # 通过受权 API 把 echo 从白名单里移走，只留一个与受控目标无关、但策略上允许的来源。
    updated = client.put(
        f"{project_base(account, project)}/pools/{project['pool_id']}/targets",
        json={"allowed_targets": [OUTSIDE_TARGET]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["allowed_targets"] == [OUTSIDE_TARGET]

    # 已经在队列里的运行：worker 领取后必须在发送前拦下，且没有真实请求发出。
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, queued["id"])
    assert report["run"]["reason_category"] == "policy"
    assert report["response"] is None
    assert report["steps"][-1]["error_code"] == "target_not_allowed"

    # 之后再新建的运行同样在创建阶段被拒，不进入队列。
    rejected = client.post(
        f"{project_base(account, project)}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "target_not_allowed"


def test_finished_run_cannot_be_canceled_twice(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(client, account, project, name="重复取消环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })
    _run_once(project["pool_id"])
    again = client.post(f"{project_base(account, project)}/runs/{run['id']}/cancel")
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "run_already_finished"


# —— SC-09：目标与生产管控 ——


def test_target_outside_allowlist_is_rejected_before_creation(
    client: TestClient, account: dict, project: dict
) -> None:
    """不在白名单内的环境目标在创建运行时即被拒绝，不进入队列。

    目标本身是策略上允许的私网地址，所以这里的拒绝只能来自白名单。
    """
    environment = create_environment(
        client, account, project, name="白名单外环境", base_url=OUTSIDE_TARGET
    )
    response = client.post(
        f"{project_base(account, project)}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "target_not_allowed"


def test_worker_rechecks_target_and_production_before_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """环境在入队后被改成生产时，worker 必须在发送前拦下，不发请求。"""
    environment = create_environment(client, account, project, name="被改环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.environments SET kind = 'production' WHERE id = %s",
                (environment["id"],),
            )
        connection.commit()
    finally:
        connection.close()

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "policy"
    assert report["response"] is None, "生产环境必须被阻止，不能发出真实请求"
    assert report["steps"][-1]["error_code"] == "target_not_allowed"


def test_worker_rechecks_target_change_after_queueing(
    client: TestClient, account: dict, project: dict
) -> None:
    """环境地址在入队后被改到白名单外时，worker 在发送前拦下。

    改成一个策略上允许、但不在白名单里的私网地址：拒绝必须来自白名单。
    """
    environment = create_environment(client, account, project, name="地址改环境")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": _echo_case_request(), "assertions": []},
    })

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.environments SET base_url = %s WHERE id = %s",
                (OUTSIDE_TARGET, environment["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["response"] is None
    assert report["steps"][-1]["error_code"] == "target_not_allowed"


def test_report_does_not_expose_credentials(
    client: TestClient, account: dict, project: dict
) -> None:
    """报告中不得出现请求头里的凭证形态内容；认证头由身份层管理，不进用例正文。"""
    environment = create_environment(client, account, project, name="脱敏环境")
    case = create_case(
        client,
        account,
        project,
        name="脱敏用例",
        request=_echo_case_request(),
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })
    assert _run_once(project["pool_id"]) in ("completed_unchecked", "passed")

    response = client.get(f"{project_base(account, project)}/runs/{run['id']}/report")
    assert response.status_code == 200
    assert "Authorization" not in response.text


class _SimulatedProcessExit(BaseException):
    """模拟 worker 进程在“请求已发出、结果尚未落库”之间被强制终止。

    继承 BaseException 而不是 Exception：真实的进程被杀不会被业务 except 捕获，
    执行内核里任何 ``except Exception`` 都不应该把这种中断当成可处理的失败。
    """


def _expire_lease(run_id: str) -> None:
    """把租约回拨到过去，复现“worker 崩溃后租约自然到期”，不需要真实等待。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.jobs SET lease_until = now() - interval '1 minute'"
                " WHERE run_id = %s",
                (run_id,),
            )
        connection.commit()
    finally:
        connection.close()


def _send_intent_rows(run_id: str) -> list[tuple]:
    """读取该运行的发送意图尝试：state/outcome/send_intent_at 是否落全。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempt_no, state, outcome, error_code,"
                " (send_intent_at IS NOT NULL) FROM app.run_step_attempts"
                " WHERE run_id = %s ORDER BY attempt_no",
                (run_id,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def test_write_intent_survives_crash_without_replay(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写请求在“已发出、结果未落库”时崩溃：重新领取不得重发。

    发送意图先于实际请求持久化。若进程恰好在请求送达目标之后、终态提交之前退出，
    租约到期后的重新领取会从头执行一遍，默认就会把同一请求再发一次——对目标系统
    而言这是一次重复写。恢复时必须先查历史尝试：已有意图而没有可信完成证据的
    write/unknown，只能收敛为 interrupted，目标调用次数保持在 1。
    """
    from app.db import get_session_factory
    from app.services import executor

    environment = create_environment(client, account, project, name="崩溃恢复环境")
    case = create_case(
        client, account, project, name="写操作崩溃用例", request=_echo_case_request()
    )
    version = publish_case(client, account, project, case["id"], side_effect="write")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    real_send = executor._send
    delivered: list[str] = []

    def crash_after_send(settings, pin, prepared):
        # 真正把请求发到受控目标，再模拟进程被杀：这一笔副作用确实已经产生。
        real_send(settings, pin, prepared)
        delivered.append(prepared.url)
        raise _SimulatedProcessExit("worker killed after the request reached the target")

    monkeypatch.setattr(executor, "_send", crash_after_send)
    claim = _claim(project["pool_id"])
    assert claim is not None
    settings = _settings()
    with pytest.raises(_SimulatedProcessExit):
        executor.execute_claim(get_session_factory(), settings, claim)
    assert len(delivered) == 1, "第一次尝试应真实发出一次请求"

    # 意图已入库、结果未落库：这正是执行内核无法自行判定“目标是否处理过”的状态。
    assert _send_intent_rows(run["id"]) == [(1, "sending", None, None, True)]

    _expire_lease(run["id"])
    monkeypatch.setattr(executor, "_send", real_send)
    replay_claim = _claim(project["pool_id"], worker_id="it-worker-2", )
    assert replay_claim is not None, "租约过期后应能被重新领取"
    assert replay_claim.fencing_token == claim.fencing_token + 1

    attempts_after_recovery: list[str] = []

    def counting_send(settings, pin, prepared):
        attempts_after_recovery.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)
    assert _execute(replay_claim, "it-worker-2") == "interrupted"
    assert attempts_after_recovery == [], "结果不明的写请求不得被重新发送"
    assert len(delivered) == 1, "目标调用次数必须保持 1"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "interrupted"

    rows = _send_intent_rows(run["id"])
    # 原来那行保留为“已写意图未落结果”的历史证据，不被新尝试覆盖或删除。
    assert rows[0] == (1, "sending", None, None, True)
    # 恢复尝试自己只记录“因历史意图不明而跳过”，不产生任何发送意图。
    assert rows[1] == (2, "skipped", "interrupted", "write_result_unknown", False)


def test_read_intent_survives_crash_and_is_safe_to_retry(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同样的崩溃窗口，声明为只读的用例仍可重试：只读请求没有重复写风险。

    这条与上一条成对存在，用来证明恢复逻辑看的是“本次运行是否可能产生副作用”，
    而不是把“有过发送意图”一律当成不可恢复。
    """
    from app.db import get_session_factory
    from app.services import executor

    environment = create_environment(client, account, project, name="只读崩溃环境")
    case = create_case(
        client, account, project, name="只读崩溃用例", request=_echo_case_request()
    )
    version = publish_case(client, account, project, case["id"], side_effect="read")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    real_send = executor._send
    delivered: list[str] = []

    def crash_after_send(settings, pin, prepared):
        real_send(settings, pin, prepared)
        delivered.append(prepared.url)
        raise _SimulatedProcessExit("worker killed after the request reached the target")

    monkeypatch.setattr(executor, "_send", crash_after_send)
    claim = _claim(project["pool_id"])
    assert claim is not None
    settings = _settings()
    with pytest.raises(_SimulatedProcessExit):
        executor.execute_claim(get_session_factory(), settings, claim)
    assert len(delivered) == 1

    _expire_lease(run["id"])
    monkeypatch.setattr(executor, "_send", real_send)
    replay_claim = _claim(project["pool_id"], worker_id="it-worker-2")
    assert replay_claim is not None

    retried: list[str] = []

    def counting_send(settings, pin, prepared):
        retried.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)
    outcome = _execute(replay_claim, "it-worker-2")
    assert len(retried) == 1, "只读请求允许安全重试"
    assert len(delivered) + len(retried) == 2
    assert outcome in ("pass", "passed", "completed_unchecked", "failed")
    assert _send_intent_rows(run["id"])[0][1] == "sending"


def test_cancel_during_claim_advance_blocks_the_send(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消发生在“领取之后、状态推进之前”：不得复活终态，更不得发出请求。

    这是取消竞态的确定交错：worker 已经拿到租约，正准备把运行从 queued 推进到
    running；同一时刻用户在页面上点了取消。旧实现用无条件赋值写 running，会把
    取消刚提交的终态翻回运行中，请求随后照常发出，取消变成一句空话。
    """
    from app.services import executor

    environment = create_environment(client, account, project, name="取消竞态环境")
    case = create_case(
        client, account, project, name="取消竞态用例", request=_echo_case_request()
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    claim = _claim(project["pool_id"])
    assert claim is not None

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    real_advance = executor._advance_to_running
    interleaved: list[str] = []

    def cancel_then_advance(session, job_claim, worker_id):
        # 故障点：在 worker 推进状态的同一步里，先让取消提交。
        response = client.post(
            f"{project_base(account, project)}/runs/{run['id']}/cancel"
        )
        interleaved.append(response.json().get("outcome"))
        return real_advance(session, job_claim, worker_id)

    monkeypatch.setattr(executor, "_advance_to_running", cancel_then_advance)

    assert _execute(claim) == "canceled"
    assert interleaved == ["canceled"], "取消本身应当成功"
    assert sent == [], "取消之后不得再发出任何请求"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["state"] == "finished"
    assert report["run"]["outcome"] == "canceled", "终态不得被 worker 翻回 running"
    assert report["response"] is None


def test_cancel_after_finish_does_not_overwrite_the_result(
    client: TestClient, account: dict, project: dict
) -> None:
    """运行已经正常结束：取消必须失败，且真实结果与证据都不被覆写。

    与“不能重复取消”一条成对存在：那条只看状态码，这条确认取消失败后终态、
    失败分类和执行证据都保持原样，而不是被抹成 canceled。
    """
    environment = create_environment(client, account, project, name="终态保护环境")
    case = create_case(
        client, account, project, name="终态保护用例", request=_echo_case_request()
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    worker_outcome = _run_once(project["pool_id"])
    before = get_report(client, account, project, run["id"])
    assert before["run"]["outcome"] == worker_outcome

    canceled = client.post(f"{project_base(account, project)}/runs/{run['id']}/cancel")
    assert canceled.status_code == 409, canceled.text

    after = get_report(client, account, project, run["id"])
    assert after["run"]["outcome"] == before["run"]["outcome"]
    assert after["run"]["reason_category"] != "policy"
    # 已有证据不被抹掉：历史报告仍然可以解释这次执行。
    assert after["steps"], "取消失败不得清空已有的执行证据"


def _set_principal_status(account: dict, status: str) -> None:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.users SET status = %s WHERE id = %s",
                (status, account["user_id"]),
            )
        connection.commit()
    finally:
        connection.close()


def _set_membership_role(account: dict, role: str) -> None:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.workspace_memberships SET role = %s"
                " WHERE user_id = %s AND workspace_id = %s",
                (role, account["user_id"], account["workspace_id"]),
            )
        connection.commit()
    finally:
        connection.close()


def _delete_membership(account: dict) -> None:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM app.workspace_memberships"
                " WHERE user_id = %s AND workspace_id = %s",
                (account["user_id"], account["workspace_id"]),
            )
        connection.commit()
    finally:
        connection.close()


def _set_project_status(account: dict, project: dict, status: str) -> None:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.projects SET status = %s WHERE id = %s AND workspace_id = %s",
                (status, project["id"], account["workspace_id"]),
            )
        connection.commit()
    finally:
        connection.close()


def _set_environment_status(environment: dict, status: str) -> None:
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.environments SET status = %s WHERE id = %s",
                (status, environment["id"]),
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("case_id", "revoke", "expected_code"),
    [
        ("account_disabled", lambda a, p, e: _set_principal_status(a, "disabled"),
         "principal_inactive"),
        ("downgraded_to_viewer", lambda a, p, e: _set_membership_role(a, "viewer"),
         "principal_not_authorized"),
        ("membership_removed", lambda a, p, e: _delete_membership(a), "membership_revoked"),
        ("project_archived", lambda a, p, e: _set_project_status(a, p, "archived"),
         "project_archived"),
        ("environment_archived", lambda a, p, e: _set_environment_status(e, "archived"),
         "environment_archived"),
    ],
)
def test_revocation_after_enqueue_blocks_the_send(
    client: TestClient,
    account: dict,
    project: dict,
    case_id: str,
    revoke,
    expected_code: str,
) -> None:
    """入队之后收回许可：停用账号、降级、移除成员、归档项目或环境都必须拦住发送。

    这些动作都表示用户已经明确不再授权这次执行。创建运行时检查过一次并不代表
    发送时仍然成立；运行可以在队列里停留任意长时间。如果只在创建时校验，撤回就
    只能拦住之后新建的运行，对已经在队列里的运行毫无作用，而真实 HTTP 照样发出。
    """
    environment = create_environment(client, account, project, name=f"撤权环境-{case_id}")
    case = create_case(
        client, account, project, name=f"撤权用例-{case_id}", request=_echo_case_request()
    )
    version = publish_case(client, account, project, case["id"], side_effect="write")
    run = start_run(client, account, project, {
        "environment_id": environment["id"],
        "case_version_id": version["id"],
    })

    # 入队成功之后才撤权：这正是快照校验覆盖不到的时间窗。
    revoke(account, project, environment)

    assert _run_once(project["pool_id"]) == "error"

    # 撤权同样收回了报告读取权限，因此终态直接按迁移身份从库里核对：这里要证明
    # 的是执行内核在发送前拦下了请求，而不是接口仍然能读到它。
    final = _run_and_attempt(run["id"])
    assert final["state"] == "finished"
    assert final["outcome"] == "error"
    assert final["reason_category"] == "policy"
    assert final["attempt_state"] == "skipped"
    assert final["error_code"] == expected_code
    assert final["response"] is None, "撤权之后不得产生任何目标副作用"


def _run_and_attempt(run_id: str) -> dict:
    """按迁移身份读取运行的终态与最后一次尝试的发送证据。

    撤权类用例会连带收回 API 读取权限，报告接口因此读不到；这些断言要验证的是
    执行内核的行为，直接用库里的行更准确。
    """
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, outcome, reason_category FROM app.runs WHERE id = %s",
                (run_id,),
            )
            state, outcome, reason = cursor.fetchone()
            cursor.execute(
                "SELECT state, error_code, response FROM app.run_step_attempts"
                " WHERE run_id = %s ORDER BY attempt_no DESC LIMIT 1",
                (run_id,),
            )
            attempt_state, error_code, response = cursor.fetchone()
        return {
            "state": state,
            "outcome": outcome,
            "reason_category": reason,
            "attempt_state": attempt_state,
            "error_code": error_code,
            "response": response,
        }
    finally:
        connection.close()
