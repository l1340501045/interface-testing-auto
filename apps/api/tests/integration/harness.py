"""集成测试共享夹具：账号、工作空间、登录客户端与项目。

两个集成测试模块共用这里的夹具，避免同一套租户准备逻辑各写一遍而逐渐分叉。
每次测试使用独立账号与工作空间，结束后按依赖顺序清理，不触碰开发库数据。
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from helpers import migrator_connection, purge_user, purge_workspace

PASSWORD = "本机集成测试口令-9f3a"

# 受控目标服务在 compose 网络内的地址；与 CONTROLLED_TARGETS 白名单一致。
CONTROLLED_TARGET_BASE_URL = "http://echo:8080"
# 第二个受控目标实例（同一镜像、不同服务名）：用于“一个用例、两个测试环境”
# 的验收。判定依据是响应里由服务端给出的实例名，而不是环境名本身。
CONTROLLED_TARGET_ALT_BASE_URL = "http://echo-alt:8080"


def seed_account(
    connection: psycopg.Connection,
    *,
    username: str,
    password: str,
    workspace_role: str | None = "admin",
) -> tuple[uuid.UUID, uuid.UUID]:
    """用迁移身份写入账号；workspace_role 为 None 时只建账号，不建工作空间。"""
    from app.security import hash_password

    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.users (id, username, password_hash, display_name, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (user_id, username, hash_password(password), username),
        )
        if workspace_role is not None:
            cursor.execute(
                "INSERT INTO app.workspaces (id, name, status) VALUES (%s, %s, 'active')",
                (workspace_id, f"工作空间-{username}"),
            )
            cursor.execute(
                "INSERT INTO app.workspace_memberships (workspace_id, user_id, role)"
                " VALUES (%s, %s, %s)",
                (workspace_id, user_id, workspace_role),
            )
    connection.commit()
    return user_id, workspace_id


@pytest.fixture
def account():
    """每次测试使用独立账号与工作空间，结束后按依赖顺序清理。"""
    connection = migrator_connection()
    username = f"it-{uuid.uuid4().hex[:10]}"
    user_id, workspace_id = seed_account(connection, username=username, password=PASSWORD)
    try:
        yield {"user_id": user_id, "workspace_id": workspace_id, "username": username}
    finally:
        purge_workspace(connection, workspace_id)
        purge_user(connection, user_id)
        connection.close()


@pytest.fixture
def client(account: dict) -> TestClient:
    """已登录且带 CSRF 头的管理员客户端。"""
    from app.main import app

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/auth/login",
            json={"username": account["username"], "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        test_client.headers["X-CSRF-Token"] = test_client.cookies["interface_csrf"]
        yield test_client


@pytest.fixture
def project(client: TestClient, account: dict) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects",
        json={"key": f"it{uuid.uuid4().hex[:8]}", "name": "集成测试项目"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def project_base(account: dict, project: dict) -> str:
    return f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}"


def create_environment(
    client: TestClient,
    account: dict,
    project: dict,
    name: str = "测试环境",
    base_url: str = CONTROLLED_TARGET_BASE_URL,
) -> dict:
    response = client.post(
        f"{project_base(account, project)}/environments",
        json={"name": name, "kind": "test", "base_url": base_url},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_case(client: TestClient, account: dict, project: dict, *, name: str, request: dict,
                assertions: list[dict] | None = None) -> dict:
    response = client.post(
        f"{project_base(account, project)}/cases",
        json={"name": name, "request": request, "assertions": assertions or []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def publish_case(
    client: TestClient,
    account: dict,
    project: dict,
    case_id: str,
    side_effect: str = "read",
    draft_rev: int | None = None,
) -> dict:
    """发布用例。默认先读一次草稿拿修订号，模拟“发布我看到的那一版”。

    发布要求声明草稿修订号，正是为了防止“用户看到 v3、另一个保存改成 v4”的窗口
    把 v4 悄悄固化。因此这里不硬编码修订号，而是照页面的做法先读再发。
    """
    if draft_rev is None:
        current = client.get(f"{project_base(account, project)}/cases/{case_id}")
        assert current.status_code == 200, current.text
        draft_rev = current.json()["rev"]
    response = client.post(
        f"{project_base(account, project)}/cases/{case_id}/publish",
        json={"side_effect": side_effect, "draft_rev": draft_rev},
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, account: dict, project: dict, payload: dict,
              idempotency_key: str | None = None) -> dict:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    response = client.post(f"{project_base(account, project)}/runs", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def get_report(client: TestClient, account: dict, project: dict, run_id: str) -> dict:
    response = client.get(f"{project_base(account, project)}/runs/{run_id}/report")
    assert response.status_code == 200, response.text
    return response.json()
