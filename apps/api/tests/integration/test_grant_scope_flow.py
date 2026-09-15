"""授权选择必须纳入真实目标，且预检与执行给出同一个结论。

对应 R1 第 2 项。只按内容匹配会在多条同内容授权里任取一条，可能取到**范围不允许本次
目标**的那条：执行期报目标不匹配，而预检按同一条规则算却说“可以发送”——两边用同一次
选择，错也错得一致，用户在点击之后才发现。

这里把身份的允许目标放到两个受控 origin 上，让“同内容、不同范围”的两份授权都能合法
存在，从而把“选择”与“执行”两件事分开验证。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import (
    DEMO_SLOT,
    _create_secret,
    _debug_assertions,
    _debug_request,
    _grant_debug_snapshot,
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

pytestmark = pytest.mark.integration


def _two_target_environment(
    client: TestClient, account: dict, project: dict, name: str
) -> tuple[str, dict, dict]:
    """建立允许两个受控 origin 的身份，环境指向其中一个。

    身份声明两个目标，才能合法地签出“只允许另一个目标”的授权：授权只能收窄身份范围，
    因此单目标身份下这种授权会被创建接口直接拒绝，测不到选择逻辑。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name=name)
    created = client.post(
        f"{base}/credentials/profiles",
        json={
            "environment_id": environment["id"],
            "name": f"双目标身份-{name}",
            "allowed_targets": [CONTROLLED_TARGET_BASE_URL, CONTROLLED_TARGET_ALT_BASE_URL],
            "allowed_auth_slots": [DEMO_SLOT],
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    version = client.post(
        f"{base}/credentials/profiles/{profile['id']}/versions",
        json={"config": {"auth_slot": DEMO_SLOT}},
    )
    assert version.status_code == 201, version.text
    secret = _create_secret(client, base, f"双目标令牌-{name}", "demo-token-scope-it")
    activated = client.put(
        f"{base}/credentials/profiles/{profile['id']}/set",
        json={"slots": {DEMO_SLOT: secret["latest_version_id"]}},
    )
    assert activated.status_code == 200, activated.text
    return base, environment, activated.json()


def _grant_with_targets(
    client: TestClient,
    base: str,
    *,
    profile_id: str,
    principal_id: str,
    digest: str,
    allowed_targets: list[str],
) -> dict:
    response = client.post(
        f"{base}/credentials/grants",
        json={
            "profile_id": profile_id,
            "grant_type": "debug_snapshot",
            "principal_id": principal_id,
            "debug_snapshot_hash": digest,
            "allowed_targets": allowed_targets,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_grant_that_excludes_the_target_does_not_shadow_one_that_admits_it(
    client: TestClient, account: dict, project: dict
) -> None:
    """同内容的两份授权里，必须选中覆盖本次目标的那一份，而不是最新签发的那一份。

    复现方式刻意把“不允许本次目标”的授权签在**后面**：按内容匹配并取最新签发的实现
    会选中它，执行期报目标不匹配；按范围过滤的实现会跳过它，选中先签的、覆盖本次目标
    的那一份，请求正常发出。
    """
    base, environment, profile = _two_target_environment(
        client, account, project, "同内容多范围环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)

    # 先签：覆盖本次目标（环境指向控制目标主实例）。
    _grant_with_targets(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
        allowed_targets=[CONTROLLED_TARGET_BASE_URL],
    )
    # 后签：只允许另一个目标，不覆盖本次目标。
    _grant_with_targets(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
        allowed_targets=[CONTROLLED_TARGET_ALT_BASE_URL],
    )

    # 预检与执行必须一致：这里两边都应当认为可以发送。
    preflight = client.post(
        f"{project_base(account, project)}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["auth"]["state"] == "ready", preflight.text

    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )
    outcome = _run_once(project["pool_id"])
    report = get_report(client, account, project, run["id"])
    assert outcome == "passed", [step["error_code"] for step in report["steps"]]
    assert report["response"]["status"] == 200


def test_grant_excluding_the_target_is_reported_the_same_way_everywhere(
    client: TestClient, account: dict, project: dict
) -> None:
    """只有一份不覆盖本次目标的授权时：预检不 ready，执行同码拒绝且零发送。"""
    base, environment, profile = _two_target_environment(
        client, account, project, "目标不覆盖环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)
    _grant_with_targets(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
        allowed_targets=[CONTROLLED_TARGET_ALT_BASE_URL],
    )

    preflight = client.post(
        f"{project_base(account, project)}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["ready"] is False
    assert body["issues"][0]["code"] == "credential_target_mismatch"

    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert [step["error_code"] for step in report["steps"]] == ["credential_target_mismatch"]
    assert report["response"] is None, "范围不覆盖本次目标时不得发出请求"


def test_preflight_rejects_invalid_assertions_like_execution(
    client: TestClient, account: dict, project: dict
) -> None:
    """断言配置非法时预检必须拒绝，不能报 ready 而执行期才报缺少类型。

    执行期用 `validate_assertions` 校验快照里的断言；预检若跳过这一步，一份
    `assertions:[{}]` 会在页面上显示“可以发送”，点击后才在执行前失败。
    """
    environment = create_environment(client, account, project, name="非法断言环境")
    snapshot = {
        "request": {"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        "assertions": [{}],
    }

    preflight = client.post(
        f"{project_base(account, project)}/debug-preflight",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["ready"] is False
    assert body["issues"][0]["code"] == "case_invalid"
    assert body["issues"][0]["action"] == "edit_request"
    # 非法断言给不出来源证明：这份配置根本没有可执行的形态。
    assert body["context"] is None

    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["response"] is None
    # 执行期是同一个原因码（配置错误），预检没有报出一个更好听的结论。
    assert report["run"]["reason_category"] == "configuration"


def test_grant_selection_uses_the_internal_digest_not_the_public_fingerprint(
    client: TestClient, account: dict, project: dict
) -> None:
    """报告公开的关联标记不参与授权匹配：换掉它不会让已签发授权失配。

    授权绑定的是原有的内部快照摘要。若选择逻辑改用对外的 HMAC 标记，所有既有授权会
    因为键变了而全部失配——这是兼容性破坏，不是更安全的实现。
    """
    base, environment, profile = _two_target_environment(
        client, account, project, "摘要兼容环境"
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

    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )
    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    context = report["context"]
    assert context is not None
    # 对外返回的是绑定主体的不透明标记，与用于授权的内部摘要不是同一个值。
    assert context["snapshot_fingerprint"] != digest
    assert context["input_fingerprint"] != digest
