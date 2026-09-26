"""环境地址校验的真实库闭环（ENV-01～ENV-03、ENV-05）。

这一层要回答的是“绕过前端也挡得住吗”以及“存量坏数据会怎样”，因此必须走真实
PostgreSQL：创建／PATCH 直接打 API，存量坏地址由迁移身份**直接写库**复现（页面已经
产生不出这种值了），再用真实的预检与运行创建入口观察结果。

两条容易混淆的边界在这里锁死：

- **地址缺协议不是白名单问题。** 它必须是 `environment_url_invalid`，建议动作
  `configure_environment`，否则用户会被引去修改本来正确的用例路径。
- **白名单问题也不能被顺手改成环境问题。** 地址合法但不在执行池白名单内的目标仍然是
  `target_not_allowed`／`edit_request`——把所有目标问题都归成环境问题，同样是误导。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from test_credentials_flow import _debug_assertions, _debug_request

from harness import create_environment, migrator_connection, project_base

pytestmark = pytest.mark.integration

# 受控目标服务：白名单里只有 echo／echo-alt，其它合法地址都会被白名单拒绝。
ALLOWED_BASE_URL = "http://echo:8080"
OUTSIDE_ALLOWLIST_BASE_URL = "http://outside.example:8080"


def _environments(client: TestClient, account: dict, project: dict) -> list[dict]:
    response = client.get(f"{project_base(account, project)}/environments")
    assert response.status_code == 200, response.text
    return response.json()


def _environment_row(environment_id: str) -> tuple[str, str, int, dict]:
    """直接读库里的那一行：接口响应不暴露 rev，而“PATCH 失败不改 rev”只能从库里看。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, base_url, rev, variables FROM app.environments WHERE id = %s",
                (environment_id,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def _plant_legacy_base_url(environment_id: str, base_url: str) -> None:
    """用迁移身份写入历史坏值，复现“修复前存进去的地址仍在库里”的现状。

    页面已经产生不出这种值了；不直接写库就无法验证存量数据的表现。
    """
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.environments SET base_url = %s WHERE id = %s",
                (base_url, environment_id),
            )
        connection.commit()
    finally:
        connection.close()


def _run_and_job_counts(project_id: str) -> tuple[int, int]:
    """本项目下的运行与工作项条数：拒绝创建必须同时不留下工作项。"""
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


def _preflight(
    client: TestClient, account: dict, project: dict, environment_id: str, snapshot: dict
) -> dict:
    response = client.post(
        f"{project_base(account, project)}/debug-preflight",
        json={"environment_id": environment_id, "debug_snapshot": snapshot},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot() -> dict:
    return {"request": _debug_request("/echo"), "assertions": _debug_assertions()}


# —— ENV-01：缺协议地址在创建与 PATCH 上都明确失败 ——


@pytest.mark.parametrize(
    "bad_url",
    [
        "target-service:8080",
        "echo",
        "echo:8080",
        "ftp://echo:8080",
        "http://echo:8080/api?v=1",
        # 后端审查确认的漏验：越界 IPv4 与前导零曾被当成普通域名放行，IPvFuture 也一样。
        # 它们在客户端构造请求时就失败，必须在保存边界就被拒绝。
        "http://256.256.256.256:8080/api",
        "http://1.2.3.04/api",
        "http://[v1.echo]:8080/api",
    ],
)
def test_create_rejects_address_that_cannot_be_joined(
    client: TestClient, account: dict, project: dict, bad_url: str
) -> None:
    before = _environments(client, account, project)
    response = client.post(
        f"{project_base(account, project)}/environments",
        json={"name": f"坏地址环境-{uuid.uuid4().hex[:6]}", "kind": "test", "base_url": bad_url},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "environment_url_invalid"
    # 错误信息不回显原文：粘进来的地址里可能带凭证。
    assert bad_url not in body["message"]
    assert _environments(client, account, project) == before, "被拒绝的地址不能留下记录"


def test_patch_rejects_bad_address_without_touching_the_record(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(
        client, account, project, name="原环境", base_url=f"{ALLOWED_BASE_URL}/api/v1"
    )
    base = project_base(account, project)
    before = _environment_row(environment["id"])

    response = client.patch(
        f"{base}/environments/{environment['id']}",
        json={
            "name": "改过的名字",
            "base_url": "echo:8080",
            "variables": {"count": {"type": "number", "text": "1"}},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "environment_url_invalid"

    # 整条 PATCH 都不生效：名称、地址、变量与 rev 全部保持原样，不存在“改了一半”。
    assert _environment_row(environment["id"]) == before


# —— ENV-02：合法地址原样保存，基础路径不丢 ——


@pytest.mark.parametrize(
    "base_url",
    [
        "http://echo:8080",
        "https://api.example.test",
        "http://echo:8080/api/v1",
        # IP＋端口必须继续支持；地址用 TEST-NET-1（RFC 5737），不指向任何真实主机。
        "http://192.0.2.10:8080/base",
        "http://[2001:db8::1]:9000/api",
        # 内网短主机名必须继续支持。
        "http://intranet-service",
        # 合法国际化域名必须能存进去；原文与基础路径都不能被改写。
        "http://例子.测试:8080/api",
    ],
)
def test_valid_address_is_saved_verbatim(
    client: TestClient, account: dict, project: dict, base_url: str
) -> None:
    environment = create_environment(
        client, account, project, name=f"环境-{uuid.uuid4().hex[:6]}", base_url=base_url
    )
    assert environment["base_url"] == base_url, "路径与编码不能被 URL 规范化改写"
    assert _environment_row(environment["id"])[1] == base_url, "落库的也是同一份文本"


def test_save_trims_only_outer_spaces(client: TestClient, account: dict, project: dict) -> None:
    environment = create_environment(
        client, account, project, name="带空格环境", base_url="  http://echo:8080/api/v1  "
    )
    assert environment["base_url"] == "http://echo:8080/api/v1"


# —— ENV-03：存量坏数据的预检结论与运行拒绝 ——


@pytest.mark.parametrize(
    "legacy_base_url",
    [
        # 缺协议：本次要修的主要缺陷。
        "target-service:8080",
        # 后端审查确认的漏验：越界 IPv4 曾被当成域名放行。它同样必须归到**地址**问题，
        # 而不是白名单问题——否则用户会去修改本来正确的用例路径。
        "http://256.256.256.256:8080",
    ],
)
def test_legacy_bad_address_is_reported_as_an_environment_problem(
    client: TestClient, account: dict, project: dict, legacy_base_url: str
) -> None:
    environment = create_environment(client, account, project, name="存量坏环境")
    _plant_legacy_base_url(environment["id"], legacy_base_url)
    base = project_base(account, project)
    snapshot = _snapshot()

    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is False
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["code"] == "environment_url_invalid"
    assert issue["action"] == "configure_environment", "下一步必须是改环境地址，不是改用例"
    assert legacy_base_url not in issue["message"], "错误信息不回显原始地址"

    runs_before, jobs_before = _run_and_job_counts(project["id"])
    rejected = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "environment_url_invalid"
    assert _run_and_job_counts(project["id"]) == (runs_before, jobs_before), (
        "被拒绝的运行不能入队，也不能留下工作项（零被测发送）"
    )


def test_valid_address_outside_allowlist_stays_a_request_problem(
    client: TestClient, account: dict, project: dict
) -> None:
    """地址语法正确但不在白名单内：仍然归到“请求／目标”，不能被顺手改成环境问题。"""
    environment = create_environment(
        client, account, project, name="白名单外环境", base_url=OUTSIDE_ALLOWLIST_BASE_URL
    )
    result = _preflight(client, account, project, environment["id"], _snapshot())
    assert result["ready"] is False
    issue = result["issues"][0]
    assert issue["code"] == "target_not_allowed"
    assert issue["action"] == "edit_request"


# —— ENV-05：修好环境后，同一份草稿继续可用 ——


def test_fixing_the_environment_makes_the_same_draft_sendable(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = create_environment(client, account, project, name="待修环境")
    _plant_legacy_base_url(environment["id"], "echo:8080")
    base = project_base(account, project)
    snapshot = _snapshot()

    assert _preflight(client, account, project, environment["id"], snapshot)["ready"] is False

    fixed = client.patch(
        f"{base}/environments/{environment['id']}", json={"base_url": ALLOWED_BASE_URL}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["base_url"] == ALLOWED_BASE_URL

    # 同一份未保存的草稿：环境修好之后预检通过，不需要重新编辑请求内容。
    result = _preflight(client, account, project, environment["id"], snapshot)
    assert result["ready"] is True
    assert result["issues"] == []

    started = client.post(
        f"{base}/runs",
        json={"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    assert started.status_code == 202, started.text
    assert started.json()["environment_id"] == environment["id"]
