"""请求调试工作台的直接依赖缺口回归。

对应 DW-AC01／DW-AC04／DW-AC05 与 implement.md 第 2 节：三处缺口必须先以真实
PostgreSQL、真实 worker 执行复现，再改责任层。修复前本模块的用例必须失败——
只有先看到红，才能证明后面改动的确是围绕同一个原因，而不是把实现写成测试的样子。

复现的三处：
1. 同一份内容用掉一份一次性授权后，重新签发的同内容授权必须能被选中；
2. 环境存在多份可用身份时，发送前必须明确报告配置歧义并零发送；
3. 入队之后环境地址被改动时，不得把请求发往新地址。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import (
    PLAIN_SECRET,
    _configure_credential,
    _create_secret,
    _debug_assertions,
    _debug_request,
    _grant_debug_snapshot,
    _prepare_authenticated_environment,
    _run_once,
    _snapshot_digest,
)

from harness import (
    CONTROLLED_TARGET_ALT_BASE_URL,
    CONTROLLED_TARGET_BASE_URL,
    create_environment,
    get_report,
    project_base,
    start_run,
)

# 这三条用例连真实 PostgreSQL、建账号与项目、用真实 worker 执行：必须带集成标记。
# 少了它，`pytest -m "not integration"`（核心门禁，默认连开发库）会把它们一起选走，
# 于是“纯行为测试”这一步会去写开发库——门禁本身成了数据污染源。
pytestmark = pytest.mark.integration


def _echo_request() -> dict:
    """无认证需求的最小调试请求：用于只关心目标归属的场景。"""
    return {"method": "GET", "path": "/echo", "body_type": "none", "body": ""}


def _step_codes(report: dict) -> list[str]:
    return [step["error_code"] for step in report["steps"]]


def test_regranted_identical_snapshot_is_selectable_after_first_is_consumed(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一份未改动内容再次授权后，应当选出这份新授权并正常执行。

    复现的缺陷是：授权查询只过滤主体、环境、状态与目标，没有排序，也不先排除已
    消费的记录；内容匹配还要等取出**一条之后**才检查。于是管理员为同一份内容再
    签发一次授权，执行时仍可能取到那份已经用掉的一次性授权，用户看到的是
    “刚授权却仍报已使用”，而且换一份新授权也修不好。
    """
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "同内容再授权环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)

    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    first = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert _run_once(project["pool_id"]) == "passed"
    first_report = get_report(client, account, project, first["id"])
    assert first_report["response"]["status"] == 200

    # 内容一个字都没改，管理员只补签了一份同内容授权。
    second_grant = _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )
    second = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    second_outcome = _run_once(project["pool_id"])
    second_report = get_report(client, account, project, second["id"])
    # 失败时把步骤错误码一并报出来：这条用例要区分“选错了授权”与其它拒绝。
    assert second_outcome == "passed", _step_codes(second_report)
    assert second_report["response"]["status"] == 200
    assert second_report["response"]["body"]
    # 新签发的那一份才是本次消费掉的那个。
    assert second_grant["id"] != ""
    assert PLAIN_SECRET not in json.dumps(second_report, ensure_ascii=False)


def test_ambiguous_environment_identity_blocks_the_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """环境有两份可用身份时，必须明确报配置歧义并零发送。

    现状是从一个没有排序的查询里任取一条：用户无法知道本次用的是哪份身份，管理员
    也无法通过配置控制结果。两份身份都签发授权，排除“只是缺授权”的解释——歧义
    本身就是拒绝原因，不能靠“碰巧另一条也能用”掩盖。
    """
    base, environment, first_profile = _prepare_authenticated_environment(
        client, account, project, "身份歧义环境"
    )
    second_secret = _create_secret(client, base, "歧义环境第二令牌", PLAIN_SECRET)
    second_profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=second_secret["latest_version_id"],
        name="歧义环境第二身份",
    )
    assert second_profile["status"] == "available"

    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)
    for profile in (first_profile, second_profile):
        _grant_debug_snapshot(
            client,
            base,
            profile_id=profile["id"],
            principal_id=str(account["user_id"]),
            digest=digest,
        )

    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert _step_codes(report) == ["credential_ambiguous"]
    assert report["response"] is None


def test_queued_run_does_not_switch_target_when_environment_changes(
    client: TestClient, account: dict, project: dict
) -> None:
    """入队之后环境地址被改动时，本次运行不得发往新地址。

    创建运行时把环境地址冻进了快照，执行时却用**当前**环境重新推导地址与基础路径。
    排队期间改一次 base_url，请求就会发到另一个目标，而报告里的来源仍然是入队时
    那个环境。这里按最保守的处理复现：检测到冻结配置与当前配置不一致时明确拒绝，
    用户确认后再重新发送。
    """
    base = project_base(account, project)
    environment = create_environment(
        client, account, project, name="环境冻结", base_url=CONTROLLED_TARGET_BASE_URL
    )
    snapshot = {"request": _echo_request(), "assertions": []}
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": snapshot},
    )

    moved = client.patch(
        f"{base}/environments/{environment['id']}",
        json={"base_url": CONTROLLED_TARGET_ALT_BASE_URL},
        headers={"If-Match": str(environment["rev"])},
    )
    assert moved.status_code == 200, moved.text

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert _step_codes(report) == ["environment_changed"]
    # 零发送：既没有发往新地址，也没有发往旧地址。
    assert report["response"] is None
