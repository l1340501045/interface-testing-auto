"""真实数据库上的 API 行为集成：登录会话、项目配置、用例版本、导入与入队。

对应 SC-01（登录/退出与角色）、SC-02（导入不发送）、SC-03（环境绑定执行池）、
SC-04（断言试算与草稿隔离）、SC-05（长整数无损）与 SC-06 的“创建运行立即返回
标识、尚未发送 HTTP”。测试通过 ASGI 客户端直连真实应用与真实 PostgreSQL，
不依赖外部 uvicorn 进程，因此验证的是真实路由、鉴权、事务与 RLS。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from harness import (
    PASSWORD,
    create_case,
    migrator_connection,
    publish_case,
    purge_user,
    purge_workspace,
    seed_account,
)
from harness import create_environment as _environment
from harness import project_base as _base

pytestmark = pytest.mark.integration


# —— SC-01：登录、退出与会话撤销 ——


def test_unauthenticated_request_is_rejected() -> None:
    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/me").status_code == 401


def test_health_endpoint_still_available() -> None:
    from app.main import app

    with TestClient(app) as anonymous:
        response = anonymous.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_login_binds_session_to_principals_workspaces(client: TestClient, account: dict) -> None:
    me = client.get("/api/v1/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user"]["username"] == account["username"]
    # 工作空间管理员不等于平台引导管理员：把前者当成后者，等于让一个项目的
    # 管理员拿到跨租户数据绕过，正是 RLS 明确要避免的。
    assert body["user"]["is_admin"] is False
    assert [item["id"] for item in body["workspaces"]] == [str(account["workspace_id"])]
    assert body["workspaces"][0]["role"] == "admin"


def test_platform_admin_flag_is_independent_of_workspace_role(client: TestClient, account: dict) -> None:
    """平台引导管理员标志来自账号本身，不因加入某个工作空间而获得。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.users SET is_admin = true WHERE id = %s", (account["user_id"],)
            )
        connection.commit()
        me = client.get("/api/v1/me")
        assert me.status_code == 200, me.text
        assert me.json()["user"]["is_admin"] is True
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.users SET is_admin = false WHERE id = %s", (account["user_id"],)
            )
        connection.commit()
        connection.close()


def test_login_sets_httponly_session_cookie_and_readable_csrf(
    account: dict,
) -> None:
    from app.main import app

    with TestClient(app) as fresh:
        response = fresh.post(
            "/api/v1/auth/login",
            json={"username": account["username"], "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(item for item in cookies if item.startswith("interface_session="))
        csrf_cookie = next(item for item in cookies if item.startswith("interface_csrf="))
        # 会话令牌不可被脚本读取；CSRF 令牌需要前端回填请求头，因此可读。
        assert "HttpOnly" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "SameSite=lax" in session_cookie


def test_logout_revokes_session_immediately(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 200
    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204, logout.text
    # 会话在数据库中被撤销，原 Cookie 立即失效。
    assert client.get("/api/v1/me").status_code == 401


def test_logout_clears_session_and_csrf_cookies(account: dict) -> None:
    """退出登录必须真正下发删除 Cookie，而不是只撤销服务端会话。

    只撤销会话会让浏览器继续带着已经失效的令牌，下一个页面加载仍被当作“已登录”，
    直到某个请求返回 401 才回到登录页。这里同时检查响应头与客户端 Cookie 罐，
    避免只测数据库状态而漏掉响应构造问题。
    """
    from app.main import app

    with TestClient(app) as fresh:
        login = fresh.post(
            "/api/v1/auth/login",
            json={"username": account["username"], "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        assert fresh.cookies.get("interface_session") is not None
        assert fresh.cookies.get("interface_csrf") is not None
        # 退出是写操作，同样要带 CSRF 头（与页面行为一致）。
        fresh.headers["X-CSRF-Token"] = fresh.cookies["interface_csrf"]

        logout = fresh.post("/api/v1/auth/logout")
        assert logout.status_code == 204, logout.text

        cleared = logout.headers.get_list("set-cookie")
        session_cleared = next(item for item in cleared if item.startswith("interface_session="))
        csrf_cleared = next(item for item in cleared if item.startswith("interface_csrf="))
        # 删除靠把值置空并让 Max-Age 归零；两者缺一，浏览器都会保留旧值。
        assert session_cleared.split("=", 1)[1].split(";")[0] in ('""', "")
        assert csrf_cleared.split("=", 1)[1].split(";")[0] in ('""', "")
        assert "Max-Age=0" in session_cleared
        assert "Max-Age=0" in csrf_cleared
        # 客户端 Cookie 罐也必须不再持有令牌（Cookie 的删除属性要与写入时一致）。
        assert fresh.cookies.get("interface_session") is None
        assert fresh.cookies.get("interface_csrf") is None
        fresh.headers.pop("X-CSRF-Token", None)
        assert fresh.get("/api/v1/me").status_code == 401


def test_write_requires_csrf_header(client: TestClient, account: dict) -> None:
    saved = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            f"/api/v1/workspaces/{account['workspace_id']}/projects",
            json={"key": f"nocsrf{uuid.uuid4().hex[:6]}", "name": "无 CSRF"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "csrf_invalid"
    finally:
        client.headers["X-CSRF-Token"] = saved


def test_wrong_password_does_not_reveal_account_existence() -> None:
    from app.main import app

    connection = migrator_connection()
    username = f"it-known-{uuid.uuid4().hex[:8]}"
    user_id, workspace_id = seed_account(connection, username=username, password=PASSWORD)
    try:
        with TestClient(app) as anonymous:
            unknown = anonymous.post(
                "/api/v1/auth/login", json={"username": "不存在的账号", "password": "whatever-1234"}
            )
            wrong = anonymous.post(
                "/api/v1/auth/login", json={"username": username, "password": "错误口令-0000"}
            )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["message"] == wrong.json()["message"]
    finally:
        purge_workspace(connection, workspace_id)
        purge_user(connection, user_id)
        connection.close()


def test_viewer_cannot_write_project() -> None:
    """最低角色验证：查看者可以读，不能写。"""
    from app.main import app

    connection = migrator_connection()
    username = f"it-viewer-{uuid.uuid4().hex[:8]}"
    user_id, workspace_id = seed_account(
        connection, username=username, password=PASSWORD, workspace_role="viewer"
    )
    try:
        with TestClient(app) as viewer:
            login = viewer.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            viewer.headers["X-CSRF-Token"] = viewer.cookies["interface_csrf"]

            assert viewer.get(f"/api/v1/workspaces/{workspace_id}/projects").status_code == 200
            denied = viewer.post(
                f"/api/v1/workspaces/{workspace_id}/projects",
                json={"key": "viewer-key", "name": "查看者创建"},
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["code"] == "insufficient_role"
    finally:
        purge_workspace(connection, workspace_id)
        purge_user(connection, user_id)
        connection.close()


def test_cross_workspace_access_is_hidden(client: TestClient) -> None:
    """非成员访问其他工作空间按 404 处理，不泄露其存在。"""
    connection = migrator_connection()
    username = f"it-outsider-{uuid.uuid4().hex[:8]}"
    _, foreign_workspace = seed_account(connection, username=username, password=PASSWORD)
    try:
        response = client.get(f"/api/v1/workspaces/{foreign_workspace}/projects")
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
    finally:
        purge_workspace(connection, foreign_workspace)
        connection.close()


# —— SC-03：项目、环境与执行池绑定 ——


def test_project_creates_default_pool_and_grant(
    client: TestClient, account: dict, project: dict
) -> None:
    assert project["pool_id"], "创建项目应同时建立默认执行池并显式授权"
    listed = client.get(f"/api/v1/workspaces/{account['workspace_id']}/projects")
    assert listed.status_code == 200
    assert any(item["id"] == project["id"] for item in listed.json())

    connection = migrator_connection()
    try:
        row = connection.execute(
            "SELECT status FROM app.runner_pool_project_grants WHERE project_id = %s",
            (project["id"],),
        ).fetchone()
        assert row is not None and row[0] == "active", "默认执行池必须带项目授权记录"
    finally:
        connection.close()


def test_environment_is_bound_to_server_side_pool(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = _environment(client, account, project)
    # 执行池由服务端绑定，不接受调用方指定。
    assert environment["pool_id"] == project["pool_id"]


def test_production_environment_is_rejected(client: TestClient, account: dict, project: dict) -> None:
    response = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}/environments",
        json={"name": "生产环境", "kind": "production", "base_url": "http://echo:8080"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "production_not_enabled"


def test_environment_duplicate_name_conflicts(client: TestClient, account: dict, project: dict) -> None:
    _environment(client, account, project, name="同名环境")
    duplicate = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}/environments",
        json={"name": "同名环境", "kind": "test", "base_url": "http://echo:8080"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "environment_name_exists"


def test_variables_are_versioned_and_reject_secrets(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    first = client.put(
        f"{base}/variables",
        json={"variables": [{"name": "base", "value": {"type": "string", "text": "v1"}}]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1

    second = client.put(
        f"{base}/variables",
        json={"variables": [{"name": "base", "value": {"type": "string", "text": "v2"}}]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2, "普通变量按不可变版本新增"

    secret = client.put(
        f"{base}/variables",
        json={"variables": [{"name": "token", "value": {"type": "secret", "text": "s"}}]},
    )
    assert secret.status_code == 400, secret.text
    assert secret.json()["code"] == "secret_not_allowed"


# —— 执行池授权与目标白名单的管理入口 ——


def _pools_base(account: dict, project: dict) -> str:
    return f"{_base(account, project)}/pools"


def test_pool_list_shows_granted_pool_targets_and_bound_environments(
    client: TestClient, account: dict, project: dict
) -> None:
    environment = _environment(client, account, project, name="绑定池环境")
    listed = client.get(_pools_base(account, project))
    assert listed.status_code == 200, listed.text
    pools = listed.json()
    assert [item["id"] for item in pools] == [project["pool_id"]], "只列出本项目已授权的池"
    pool = pools[0]
    assert pool["grant_status"] == "active"
    # 默认白名单来自受控目标配置，是“能访问哪些目标”的当前事实。
    assert "http://echo:8080" in pool["allowed_targets"]
    assert pool["environment_ids"] == [environment["id"]], "要能看出改这一条会影响哪些环境"


def test_pool_targets_are_replaced_normalized_and_deduplicated(
    client: TestClient, account: dict, project: dict
) -> None:
    """白名单整份替换：同一来源的不同写法归成一条，避免重复条目掩盖真实范围。"""
    response = client.put(
        f"{_pools_base(account, project)}/{project['pool_id']}/targets",
        json={"allowed_targets": ["http://echo:8080", "echo:8080", " https://api.example.test "]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed_targets"] == [
        "http://echo:8080",
        "https://api.example.test:443",
    ]
    again = client.get(_pools_base(account, project)).json()[0]
    assert again["allowed_targets"] == response.json()["allowed_targets"], "改动必须落库"


def test_saving_targets_never_resolves_or_contacts_them(
    client: TestClient, account: dict, project: dict
) -> None:
    """保存配置不访问目标：域名解析不了也必须能保存，否则等于用配置动作试探目标。"""
    response = client.put(
        f"{_pools_base(account, project)}/{project['pool_id']}/targets",
        json={"allowed_targets": ["http://host-does-not-exist.invalid:8080"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed_targets"] == ["http://host-does-not-exist.invalid:8080"]


def test_invalid_target_entry_is_rejected(client: TestClient, account: dict, project: dict) -> None:
    base = f"{_pools_base(account, project)}/{project['pool_id']}/targets"
    for payload in (
        {"allowed_targets": ["ftp://echo:8080"]},
        {"allowed_targets": [""]},
        # 只有路径、没有主机：不是来源，不能作为白名单条目。
        {"allowed_targets": ["/just/a/path"]},
    ):
        response = client.put(base, json=payload)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "target_invalid"
    # 空列表在请求模型层就被拒：允许清空白名单等于允许“没有任何目标可访问”，
    # 那不是收窄而是配置错误，没必要进到业务处理再判断。
    empty = client.put(base, json={"allowed_targets": []})
    assert empty.status_code == 422, empty.text
    assert empty.json()["code"] == "invalid_request"
    unchanged = client.get(_pools_base(account, project)).json()[0]
    assert "http://echo:8080" in unchanged["allowed_targets"], "被拒绝的请求不得改动白名单"


def test_pool_targets_require_admin_role(client: TestClient, account: dict, project: dict) -> None:
    """白名单是出网边界，编辑者也不能自行扩大。"""
    from app.main import app

    connection = migrator_connection()
    username = f"it-editor-{uuid.uuid4().hex[:8]}"
    user_id, workspace_id = seed_account(
        connection, username=username, password=PASSWORD, workspace_role="editor"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.workspace_memberships (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'editor')",
                (account["workspace_id"], user_id),
            )
        connection.commit()
        with TestClient(app) as editor:
            login = editor.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            editor.headers["X-CSRF-Token"] = editor.cookies["interface_csrf"]
            listed = editor.get(_pools_base(account, project))
            assert listed.status_code == 403, listed.text
            assert listed.json()["code"] == "insufficient_role"
            denied = editor.put(
                f"{_pools_base(account, project)}/{project['pool_id']}/targets",
                json={"allowed_targets": ["http://10.9.9.9:9999"]},
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["code"] == "insufficient_role"
    finally:
        purge_workspace(connection, workspace_id)
        purge_user(connection, user_id)
        connection.close()
    # 越权请求没有生效：白名单仍是受控目标默认值。
    assert "http://echo:8080" in client.get(_pools_base(account, project)).json()[0]["allowed_targets"]


def test_ungranted_pool_is_not_editable(client: TestClient, account: dict, project: dict) -> None:
    """不属于本项目的池按不存在处理，不泄露其是否存在。"""
    response = client.put(
        f"{_pools_base(account, project)}/{uuid.uuid4()}/targets",
        json={"allowed_targets": ["http://echo:8080"]},
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


def test_folder_create_and_duplicate(client: TestClient, account: dict, project: dict) -> None:
    base = _base(account, project)
    created = client.post(f"{base}/folders", json={"name": "订单模块"})
    assert created.status_code == 201, created.text
    duplicate = client.post(f"{base}/folders", json={"name": "订单模块"})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "folder_name_exists"


def test_case_folder_membership_is_explicit_and_filterable(
    client: TestClient, account: dict, project: dict
) -> None:
    """用例的目录归属：新建时落进指定目录、按字段三态更新、并能按目录过滤。

    「不改目录」与「移到未分组」**都**序列化成 `folder_id: null`，只能靠字段是否出现
    区分。按值判断（`is not None`）会把“移到未分组”做成一次静默无效的保存：响应看着
    像成功，重新打开却回到原目录——而重新分组正是目录功能的主要用途之一。
    """
    base = _base(account, project)
    folder_a = client.post(f"{base}/folders", json={"name": "A 模块"}).json()
    folder_b = client.post(f"{base}/folders", json={"name": "B 模块"}).json()

    created = client.post(f"{base}/cases", json={**_case_payload(), "folder_id": folder_a["id"]})
    assert created.status_code == 201, created.text
    case = created.json()
    case_id = case["id"]
    assert case["folder_id"] == folder_a["id"], "在 A 目录里新建的用例必须落进 A"

    def listed(folder_id: str | None) -> list[str]:
        suffix = f"?folder_id={folder_id}" if folder_id is not None else ""
        response = client.get(f"{base}/cases{suffix}")
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()]

    assert case_id in listed(folder_a["id"])
    assert case_id not in listed(folder_b["id"])
    # 不带 folder_id 是“全部草稿”，它必须仍然包含这条用例。
    assert case_id in listed(None)

    etag = created.headers["ETag"]

    # ① 字段缺席：本次不改目录。只改名字后归属必须原样保留。
    renamed = client.patch(
        f"{base}/cases/{case_id}", json={"name": "查询订单（改名）"}, headers={"If-Match": etag}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["folder_id"] == folder_a["id"], "字段缺席时必须保留原目录"
    etag = renamed.headers["ETag"]

    # ② 显式 null：移到未分组。这一步是判别性的——旧实现会静默忽略它。
    unfiled = client.patch(
        f"{base}/cases/{case_id}", json={"folder_id": None}, headers={"If-Match": etag}
    )
    assert unfiled.status_code == 200, unfiled.text
    assert unfiled.json()["folder_id"] is None, "显式 null 必须真的移到未分组"
    etag = unfiled.headers["ETag"]
    assert case_id not in listed(folder_a["id"]), "移出 A 之后不能还留在 A 的过滤列表里"
    assert case_id in listed(None)

    # ③ 给定目录 id：移到 B；两个目录的过滤列表同时换边。
    moved = client.patch(
        f"{base}/cases/{case_id}", json={"folder_id": folder_b["id"]}, headers={"If-Match": etag}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_id"] == folder_b["id"]
    etag = moved.headers["ETag"]
    assert case_id not in listed(folder_a["id"])
    assert case_id in listed(folder_b["id"])
    # 只改目录也真的推进了修订号：否则界面的 If-Match 会一直通过，冲突保护形同虚设。
    assert moved.json()["rev"] == unfiled.json()["rev"] + 1

    # ④ 跨项目目录按“不存在”拒绝，且被拒的这次不改变归属（事务未提交）。
    other = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects",
        json={"key": f"fc1{uuid.uuid4().hex[:8]}", "name": "另一个项目"},
    )
    assert other.status_code == 201, other.text
    other_base = f"/api/v1/workspaces/{account['workspace_id']}/projects/{other.json()['id']}"
    foreign = client.post(f"{other_base}/folders", json={"name": "别的项目的目录"})
    assert foreign.status_code == 201, foreign.text
    rejected = client.patch(
        f"{base}/cases/{case_id}",
        json={"folder_id": foreign.json()["id"]},
        headers={"If-Match": etag},
    )
    assert rejected.status_code == 404, rejected.text
    assert rejected.json()["code"] == "not_found"
    still_in_b = client.get(f"{base}/cases/{case_id}").json()
    assert still_in_b["folder_id"] == folder_b["id"], "被拒的跨项目目录不能改动归属"

    # ⑤ 已归档目录明确拒绝，不再接受新的用例；已归档目录里的用例不受影响。
    # S2 后旧 DELETE 明确停写；这里直接构造旧无批次归档，继续验证兼容读取/写拒绝。
    connection = migrator_connection()
    try:
        connection.execute(
            "UPDATE app.folders SET archived_at=now(), rev=rev+1 WHERE id=%s",
            (folder_b["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    assert folder_b["id"] not in [item["id"] for item in client.get(f"{base}/folders").json()]
    archived_etag = f'"{still_in_b["rev"]}"'
    into_archived = client.patch(
        f"{base}/cases/{case_id}",
        json={"folder_id": folder_b["id"]},
        headers={"If-Match": archived_etag},
    )
    assert into_archived.status_code == 409, into_archived.text
    assert into_archived.json()["code"] == "asset_unavailable"
    # S2 后旧阻塞用例只能通过显式资产 move 救回，普通 PATCH 不再顺带编辑。
    kept = client.patch(
        f"{base}/cases/{case_id}",
        json={"name": "归档后改名"},
        headers={"If-Match": archived_etag},
    )
    assert kept.status_code == 409, kept.text
    assert kept.json()["code"] == "asset_unavailable"
    # 在失效目录里新建也不允许：那会立刻得到一条挂在失效目录上的用例。
    created_in_archived = client.post(
        f"{base}/cases", json={**_case_payload(), "folder_id": folder_b["id"]}
    )
    assert created_in_archived.status_code == 400, created_in_archived.text
    assert created_in_archived.json()["code"] == "folder_archived"


# —— SC-02 / SC-04 / SC-05：导入、断言与用例版本 ——


def test_curl_import_previews_multiline_browser_command(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """浏览器复制的多行 cURL 经真实预览接口返回可发送草稿，且预览一个请求都不发。

    用户从浏览器复制出来的命令每行以反斜杠续接：LF 与从 Windows 粘贴的 CRLF 两种
    形状都要与等价单行得到同一份请求定义。认证值（Authorization 与 -b 的 Cookie）
    随运行生成、与命令文本分开构造，只用于证明它们不会落进完整响应。

    探针按对象属性安装，不用字符串路径：字符串形式若在 executor 尚未导入时被解析，
    会先触发它的首次导入，把当时装好的替身冻结成 `executor.build_client` 别名；撤销
    只还原模块属性，这个别名会留到后续用例，把真实发送一起拦掉。先导入模块再替换
    发送入口（`_send` 是唯一调用 build_client 的地方），撤销时才能正常恢复。
    """
    from app.services import executor

    def forbid_send(*_args, **_kwargs) -> None:
        raise AssertionError("cURL 预览属于导入步骤，不得发出被测 HTTP 请求")

    monkeypatch.setattr(executor, "_send", forbid_send)

    base = _base(account, project)
    token = f"it-curlimp-{uuid.uuid4().hex}"
    session = uuid.uuid4().hex
    url = "https://example.test/api/orders?page=1&size=20&sort=desc&filter=open"
    # 十一个普通浏览器请求头，加一个 Authorization 与一个 -b Cookie（用户样例的形状）。
    ordinary_headers = [
        "Accept: application/json",
        "Accept-Encoding: gzip, deflate, br, zstd",
        "Accept-Language: zh-CN,zh;q=0.9",
        "Connection: keep-alive",
        "Origin: https://example.test",
        "Referer: https://example.test/orders/list",
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        'sec-ch-ua: "Chromium";v="140", "Not=A?Brand";v="24"',
        "sec-ch-ua-mobile: ?0",
        'sec-ch-ua-platform: "macOS"',
        "sec-fetch-site: same-origin",
    ]
    options = [*(f"-H '{header}'" for header in ordinary_headers)]
    options.append(f"-H 'Authorization: Bearer {token}'")
    options.append(f"-b 'it_session={session}; it_locale=zh-CN'")
    single = " ".join([f"curl '{url}'", *options])

    def continued(newline: str) -> str:
        lines = [f"curl '{url}'", *(f"      {option}" for option in options)]
        return (" \\" + newline).join(lines)

    def preview(text: str):
        response = client.post(f"{base}/imports/curl/preview", json={"text": text})
        assert response.status_code == 200, response.text
        return response

    shapes = {
        "单行": preview(single),
        "LF": preview(continued("\n")),
        "CRLF": preview(continued("\r\n")),
    }
    baseline = shapes["单行"].json()

    for name, response in shapes.items():
        body = response.json()
        assert body["draft"] == baseline["draft"], f"{name} 应与等价单行得到同一份请求定义"
        assert body["sendable"] is True, name
        assert token not in response.text, f"{name}：Authorization 值不得出现在预览响应中"
        assert session not in response.text, f"{name}：Cookie 值不得出现在预览响应中"
        assert not any(
            header["name"].lower() in ("authorization", "cookie")
            for header in body["draft"]["headers"]
        ), f"{name}：认证头不得作为普通请求头落进草稿"

    assert baseline["draft"]["method"] == "GET"
    assert baseline["draft"]["path"] == "/api/orders"
    assert [(p["name"], p["value"]) for p in baseline["draft"]["query_params"]] == [
        ("page", "1"),
        ("size", "20"),
        ("sort", "desc"),
        ("filter", "open"),
    ]
    assert [h["name"] for h in baseline["draft"]["headers"]] == [
        "Accept",
        "Accept-Encoding",
        "Accept-Language",
        "Connection",
        "Origin",
        "Referer",
        "User-Agent",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-site",
    ]
    assert baseline["draft"]["headers"][7]["value"] == '"Chromium";v="140", "Not=A?Brand";v="24"'
    assert baseline["auth_hint"]["pending"] is True


def test_curl_import_preview_never_sends_and_preserves_values(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    response = client.post(
        f"{base}/imports/curl/preview",
        json={
            "text": (
                "curl -X POST 'https://example.test/api/items?a=1&a=2' "
                "-H 'Content-Type: application/json' "
                "--data-raw '{\"id\":9007199254740993}'"
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"]["method"] == "POST"
    # 重复查询参数与长整数都按原文保留，未经 JavaScript Number 往返。
    assert [p["value"] for p in body["draft"]["query_params"] if p["name"] == "a"] == ["1", "2"]
    assert "9007199254740993" in body["draft"]["body"]
    assert body["sendable"] is True


def test_curl_import_keeps_secrets_out_of_draft(client: TestClient, account: dict, project: dict) -> None:
    """认证值只在本次运行生成；秘密不得进预览响应，也不得作为普通请求头落草稿。

    合成值随运行生成、认证头与该命令文本分开构造，源码里不留固定 token，也不出现
    “命令字面量 + 明文认证头”这种可被静态抓取的形状；拒绝泄露的语义不变。
    """
    base = _base(account, project)
    secret = f"it-curlimp-{uuid.uuid4().hex}"
    # 认证头单独构造：值来自上面的运行时合成值，源码中不写死任何凭据。
    auth_header = f"Authorization: Bearer {secret}"
    curl_text = f"curl https://example.test/x -H '{auth_header}'"

    response = client.post(f"{base}/imports/curl/preview", json={"text": curl_text})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_hint"]["pending"] is True
    assert secret not in response.text, "秘密不得出现在预览响应中"
    assert all(
        secret not in header["value"] for header in body["draft"]["headers"]
    ), "认证值不得作为普通请求头的取值落进草稿"
    assert not any(
        header["name"].lower() == "authorization" for header in body["draft"]["headers"]
    ), "认证头不能作为普通请求头落草稿"


def test_curl_import_flags_unsupported_multipart(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    response = client.post(
        f"{base}/imports/curl/preview",
        json={"text": "curl https://example.test/upload -F 'file=@payload.json'"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sendable"] is False
    assert body["unsupported"], "multipart 文件上传必须明确标记为不支持"


def test_curl_import_rejects_unmodellable_option(
    client: TestClient, account: dict, project: dict
) -> None:
    """会改变发送行为但无法等价建模的选项必须拒绝，不静默生成错误草稿。"""
    base = _base(account, project)
    response = client.post(
        f"{base}/imports/curl/preview",
        json={"text": "curl https://example.test/x --resolve example.test:443:127.0.0.1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sendable"] is False


def test_curl_import_preserves_explicit_get_with_data(
    client: TestClient, account: dict, project: dict
) -> None:
    """显式 -X GET 搭配 -d 时不能改成 POST，否则篡改用户请求语义。"""
    base = _base(account, project)
    response = client.post(
        f"{base}/imports/curl/preview",
        json={"text": "curl -X GET -d 'a=1' https://example.test/path"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["method"] == "GET"


def test_assertion_catalog_only_exposes_implemented_types(client: TestClient) -> None:
    response = client.get("/api/v1/assertion-types")
    assert response.status_code == 200, response.text
    items = response.json()
    ids = {item["id"] for item in items}
    assert {"equals", "range", "exists", "not_null", "starts_with", "length_range"} <= ids
    # 阶段 2 的能力不能以可用项出现。
    assert not ({"regex", "json_schema", "all_of", "any_of"} & ids)
    for item in items:
        assert item["summary"].strip(), "每个断言类型都要有中文说明"
        assert item["operator_version"] >= 1


def _preview(client: TestClient, account: dict, project: dict, payload: dict) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}/assertion-previews",
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_range_default_is_open_interval(client: TestClient, account: dict, project: dict) -> None:
    params = {
        "min": {"type": "number", "text": "0"},
        "max": {"type": "number", "text": "100"},
        "include_min": False,
        "include_max": False,
    }
    assert (
        _preview(client, account, project, {"type": "range", "parameters": params,
                                            "value": {"type": "number", "text": "0"}})["status"]
        == "failed"
    )
    assert (
        _preview(client, account, project, {"type": "range", "parameters": params,
                                            "value": {"type": "number", "text": "50"}})["status"]
        == "passed"
    )
    assert (
        _preview(client, account, project, {"type": "range", "parameters": params,
                                            "value": {"type": "number", "text": "100"}})["status"]
        == "failed"
    )

    inclusive = {**params, "include_min": True, "include_max": True}
    assert (
        _preview(client, account, project, {"type": "range", "parameters": inclusive,
                                            "value": {"type": "number", "text": "100"}})["status"]
        == "passed"
    )
    assert (
        _preview(client, account, project, {"type": "range", "parameters": inclusive,
                                            "value": {"type": "number", "text": "0"}})["status"]
        == "passed"
    )


def test_big_integer_is_not_rounded(client: TestClient, account: dict, project: dict) -> None:
    def equals(expected: str, actual: str) -> str:
        return _preview(
            client,
            account,
            project,
            {
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": expected}},
                "value": {"type": "number", "text": actual},
            },
        )["status"]

    assert equals("9007199254740993", "9007199254740993") == "passed"
    assert equals("9007199254740992", "9007199254740993") == "failed"
    assert equals("9007199254740994", "9007199254740993") == "failed"


def test_decimal_compare_is_exact(client: TestClient, account: dict, project: dict) -> None:
    def equals(expected: str, actual: str) -> str:
        return _preview(
            client,
            account,
            project,
            {
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": expected}},
                "value": {"type": "number", "text": actual},
            },
        )["status"]

    assert equals("0.3", "0.1") == "failed"
    assert equals("0.10", "0.1") == "passed", "十进制数值按数值相等，不按文本"


def test_string_and_number_are_not_confused(client: TestClient, account: dict, project: dict) -> None:
    outcome = _preview(
        client,
        account,
        project,
        {
            "type": "equals",
            "parameters": {"expected": {"type": "number", "text": "0"}},
            "value": {"type": "string", "text": "0"},
        },
    )
    assert outcome["status"] == "failed"


def test_json_body_big_integer_stays_lossless(client: TestClient, account: dict, project: dict) -> None:
    """JSON 字面量内的长整数经 locator 与断言仍精确。"""
    outcome = _preview(
        client,
        account,
        project,
        {
            "type": "equals",
            "parameters": {"expected": {"type": "number", "text": "9007199254740993"}},
            "value": {"type": "json", "text": '{"id": 9007199254740993}'},
        },
    )
    # 值本身是对象，与数字期望不等，但必须给出可读的失败而不是精度相关错误。
    assert outcome["status"] == "failed"
    assert outcome["reason_code"] == "not_equal"


BIG_INT = "9007199254740993"
LONG_DECIMAL = "0.10000000000000000000000000001"


def test_big_numbers_survive_draft_and_published_round_trip(
    client: TestClient, account: dict, project: dict
) -> None:
    """SC-05 的“往返保存无损”：草稿保存后再读、发布快照落库都不改数值词法。

    前面几个用例分别覆盖了试算内核与真实 HTTP 的单点精度；这里覆盖剩下的一半——
    **存储往返**。两者失败方式不同：内核出错是算错，存储出错是“用户看到的原文被
    静默改写”，而后者在页面上不可见，只能靠读回比对发现。断言同时覆盖整数字面量
    与超长小数，因为它们在二进制浮点下会各自走到不同的舍入路径。
    """
    body = (
        '{"id": ' + BIG_INT + ', "next": 9007199254740994, "tiny": ' + LONG_DECIMAL + "}"
    )
    base = _base(account, project)
    case = create_case(
        client,
        account,
        project,
        name="往返无损用例",
        request={
            "method": "GET",
            "path": "/numbers",
            "query_params": [{"name": "amount", "value": BIG_INT}],
            "headers": [],
            "body_type": "json",
            "body": body,
        },
        assertions=[
            {
                "id": "big-int",
                "target_source": "response.body",
                "selector": [{"kind": "key", "key": "big_integer"}],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": BIG_INT}},
                "severity": "error",
            }
        ],
    )

    # 1) 草稿读回：原文与断言参数都不许被改写。
    draft = client.get(f"{base}/cases/{case['id']}")
    assert draft.status_code == 200, draft.text
    assert BIG_INT in draft.json()["request"]["body"]
    assert "9007199254740994" in draft.json()["request"]["body"]
    assert LONG_DECIMAL in draft.json()["request"]["body"]
    assert draft.json()["assertions"][0]["parameters"]["expected"]["text"] == BIG_INT

    # 2) 发布快照落库：从数据库直读，排除响应模型掩盖了存储差异的可能。
    version = publish_case(client, account, project, case["id"])
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT request::text FROM app.case_versions WHERE id = %s", (version["id"],)
            )
            stored_request = cursor.fetchone()[0]
            cursor.execute(
                "SELECT parameters::text FROM app.case_assertions WHERE case_version_id = %s",
                (version["id"],),
            )
            stored_params = cursor.fetchone()[0]
    finally:
        connection.close()

    assert BIG_INT in stored_request, "发布快照不得把长整数写成浮点近似值"
    assert "9007199254740994" in stored_request
    assert LONG_DECIMAL in stored_request, "超长小数不得被舍入"
    assert BIG_INT in stored_params
    assert "9007199254740993.0" not in stored_request, "数值不得被改写为浮点或字符串形式"


def test_missing_and_null_are_distinct(client: TestClient, account: dict, project: dict) -> None:
    exists = _preview(
        client, account, project, {"type": "exists", "parameters": {}, "value": None, "found": False}
    )
    assert exists["status"] == "failed", "存在性断言在字段缺失时应失败"

    is_null = _preview(
        client,
        account,
        project,
        {"type": "is_null", "parameters": {}, "value": {"type": "null"}, "found": True},
    )
    assert is_null["status"] == "passed"

    not_null_missing = _preview(
        client, account, project, {"type": "not_null", "parameters": {}, "found": False}
    )
    assert not_null_missing["status"] == "failed", "字段缺失时非空断言失败，不能当作通过"


def test_wrong_type_reports_error_not_pass(client: TestClient, account: dict, project: dict) -> None:
    outcome = _preview(
        client,
        account,
        project,
        {
            "type": "range",
            "parameters": {
                "min": {"type": "number", "text": "0"},
                "max": {"type": "number", "text": "10"},
                "include_min": False,
                "include_max": False,
            },
            "value": {"type": "string", "text": "abc"},
        },
    )
    assert outcome["status"] == "error", "类型错误必须是 error，不能静默通过"


def test_invalid_assertion_parameters_are_rejected(
    client: TestClient, account: dict, project: dict
) -> None:
    response = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}/assertion-previews",
        json={"type": "range", "parameters": {"unknown": 1}, "value": {"type": "number", "text": "5"}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_assertion_config"


def test_unsupported_assertion_type_is_rejected(
    client: TestClient, account: dict, project: dict
) -> None:
    response = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects/{project['id']}/assertion-previews",
        json={"type": "regex", "parameters": {}, "value": {"type": "string", "text": "abc"}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "unknown_assertion_type"


def _case_payload() -> dict:
    return {
        "name": "查询订单",
        "request": {
            "method": "GET",
            "path": "/echo",
            "query_params": [{"name": "amount", "value": "50"}],
            "headers": [],
            "body_type": "none",
            "body": "",
        },
        "assertions": [
            {
                "target_source": "request.query",
                "selector": [{"kind": "key", "key": "amount"}],
                "type": "range",
                "parameters": {
                    "min": {"type": "number", "text": "0"},
                    "max": {"type": "number", "text": "100"},
                    "include_min": False,
                    "include_max": False,
                },
            },
            {
                "target_source": "response.status",
                "type": "in_set",
                "parameters": {"values": [{"type": "number", "text": "200"}]},
            },
        ],
    }


def test_case_draft_etag_conflict_and_publish(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    assert created.status_code == 201, created.text
    case = created.json()
    assert case["rev"] == 1
    assert len(case["assertions"]) == 2

    fetched = client.get(f"{base}/cases/{case['id']}")
    assert fetched.status_code == 200
    etag = fetched.headers["ETag"]
    assert etag

    # 无前置条件拒绝写入，避免覆盖他人修改。
    missing = client.patch(f"{base}/cases/{case['id']}", json={"name": "改名"})
    assert missing.status_code == 428, missing.text
    assert missing.json()["code"] == "precondition_required"

    stale = client.patch(
        f"{base}/cases/{case['id']}", json={"name": "改名"}, headers={"If-Match": '"999"'}
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "revision_conflict"

    updated = client.patch(
        f"{base}/cases/{case['id']}", json={"name": "查询订单（改名）"}, headers={"If-Match": etag}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rev"] == 2

    # 发布声明固化的是刚才这一版（rev=2）。修订号过期的发布必须被拒绝。
    published = client.post(
        f"{base}/cases/{case['id']}/publish", json={"side_effect": "read", "draft_rev": 2}
    )
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 1
    assert published.json()["snapshot_hash"]

    versions = client.get(f"{base}/cases/{case['id']}/versions")
    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()] == [1]


def test_publishing_does_not_freeze_other_cases_draft(
    client: TestClient, account: dict, project: dict
) -> None:
    """修改一个用例不影响另一个：断言归属各自的草稿。"""
    base = _base(account, project)
    first = client.post(f"{base}/cases", json=_case_payload())
    assert first.status_code == 201
    second = client.post(
        f"{base}/cases",
        json={"name": "另一用例", "request": {"method": "GET", "path": "/echo"}},
    )
    assert second.status_code == 201, second.text
    assert second.json()["assertions"] == []

    # 修改 A 并发布，不改变 B 的修订与断言。
    etag = client.get(f"{base}/cases/{first.json()['id']}").headers["ETag"]
    updated = client.patch(
        f"{base}/cases/{first.json()['id']}",
        json={"assertions": []},
        headers={"If-Match": etag},
    )
    assert updated.status_code == 200
    assert updated.json()["assertions"] == []
    published = client.post(
        f"{base}/cases/{first.json()['id']}/publish",
        json={"side_effect": "read", "draft_rev": 2},
    )
    assert published.status_code == 201

    untouched = client.get(f"{base}/cases/{second.json()['id']}")
    assert untouched.json()["rev"] == 1
    assert untouched.json()["assertions"] == []


def test_case_default_side_effect_is_unknown(client: TestClient, account: dict, project: dict) -> None:
    """副作用未知时不能默认按只读处理，否则结果不明时会被错误重放。"""
    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    # 不传 side_effect 时默认 unknown；但草稿修订号必须显式声明。
    published = client.post(
        f"{base}/cases/{created.json()['id']}/publish", json={"draft_rev": created.json()["rev"]}
    )
    assert published.status_code == 201, published.text
    assert published.json()["side_effect"] == "unknown"


def test_case_rejects_absolute_path_and_unknown_field(
    client: TestClient, account: dict, project: dict
) -> None:
    base = _base(account, project)
    absolute = client.post(
        f"{base}/cases",
        json={"name": "绝对地址", "request": {"method": "GET", "path": "https://elsewhere.test/x"}},
    )
    assert absolute.status_code == 400, absolute.text
    assert absolute.json()["code"] == "invalid_case"

    extra = client.post(
        f"{base}/cases",
        json={"name": "多余字段", "request": {"method": "GET", "path": "/x", "url": "http://a"}},
    )
    assert extra.status_code == 400
    assert extra.json()["code"] == "invalid_case"


# —— SC-06 / SC-09：运行入队与目标管控 ——


def test_run_is_queued_without_sending_http(client: TestClient, account: dict, project: dict) -> None:
    base = _base(account, project)
    environment = _environment(client, account, project, name="执行环境")

    response = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
        },
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["state"] == "queued", "202 表示已受理入队，不代表已经发出 HTTP"
    assert run["outcome"] is None
    assert run["pool_id"] == project["pool_id"]
    assert response.headers["Location"].endswith(run["id"])

    steps = client.get(f"{base}/runs/{run['id']}/steps")
    assert steps.status_code == 200
    assert steps.json() == [], "排队阶段不应有工作项尝试记录"

    report = client.get(f"{base}/runs/{run['id']}/report")
    assert report.status_code == 200
    assert report.json()["request"] is None, "尚未执行时没有请求证据"

    listed = client.get(f"{base}/runs")
    assert listed.status_code == 200
    assert any(item["id"] == run["id"] for item in listed.json())


def test_run_rejects_client_chosen_pool(client: TestClient, account: dict, project: dict) -> None:
    """执行池由环境在服务端绑定，调用方不能自选网络出口。"""
    base = _base(account, project)
    environment = _environment(client, account, project, name="池校验环境")
    forged = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "runner_pool_id": str(uuid.uuid4()),
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
        },
    )
    assert forged.status_code == 422, forged.text


def test_run_requires_exactly_one_target(client: TestClient, account: dict, project: dict) -> None:
    base = _base(account, project)
    environment = _environment(client, account, project, name="目标校验环境")
    both = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "case_version_id": str(uuid.uuid4()),
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
        },
    )
    assert both.status_code == 400, both.text
    assert both.json()["code"] == "target_required"

    neither = client.post(f"{base}/runs", json={"environment_id": environment["id"]})
    assert neither.status_code == 400
    assert neither.json()["code"] == "target_required"


def test_cross_project_environment_is_not_executable(
    client: TestClient, account: dict, project: dict
) -> None:
    """跨项目的环境不能被借用执行。"""
    environment = _environment(client, account, project, name="范围环境")

    other = client.post(
        f"/api/v1/workspaces/{account['workspace_id']}/projects",
        json={"key": f"it2{uuid.uuid4().hex[:8]}", "name": "另一项目"},
    )
    assert other.status_code == 201, other.text
    other_base = f"/api/v1/workspaces/{account['workspace_id']}/projects/{other.json()['id']}"

    borrowed = client.post(
        f"{other_base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
        },
    )
    assert borrowed.status_code == 404, borrowed.text
    assert borrowed.json()["code"] == "not_found", "越权与不存在都按不可见处理"


def test_cancel_prevents_new_send(client: TestClient, account: dict, project: dict) -> None:
    base = _base(account, project)
    environment = _environment(client, account, project, name="取消环境")
    created = client.post(
        f"{base}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    canceled = client.post(f"{base}/runs/{run_id}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["state"] == "finished"
    assert canceled.json()["outcome"] == "canceled"

    again = client.post(f"{base}/runs/{run_id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "run_already_finished"


def test_run_idempotency_key_replays_same_run(
    client: TestClient, account: dict, project: dict
) -> None:
    """同一幂等键与相同内容返回同一运行，不重复创建。"""
    base = _base(account, project)
    environment = _environment(client, account, project, name="幂等环境")
    payload = {
        "environment_id": environment["id"],
        "debug_snapshot": {"request": {"method": "GET", "path": "/echo"}, "assertions": []},
    }
    key = uuid.uuid4().hex
    first = client.post(f"{base}/runs", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 202, first.text
    second = client.post(f"{base}/runs", json=payload, headers={"Idempotency-Key": key})
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]


def test_selector_on_direct_source_rejected_at_the_api_boundary(
    client: TestClient, account: dict, project: dict
) -> None:
    """对状态码等直接来源配置定位步骤，保存时必须明确报错。

    这类配置能通过旧的校验，却执行不了：取值层对“直接来源 + 定位步骤”抛错，而该
    取值不在断言的异常处理之内，错误会冒到 worker 兜底捕获——那里只记日志、不写
    终态，运行于是永远停在可被重新领取。因此在 API 边界就拒绝，不留悬挂运行。
    """
    base = _base(account, project)
    response = client.post(
        f"{base}/cases",
        json={
            "name": "直接来源配定位步骤",
            "request": {"method": "GET", "path": "/echo", "query_params": [], "headers": [],
                        "body_type": "none", "body": ""},
            "assertions": [
                {
                    "id": "status-with-selector",
                    "target_source": "response.status",
                    "selector": [{"kind": "key", "key": "code"}],
                    "type": "equals",
                    "parameters": {"expected": {"type": "number", "text": "200"}},
                    "severity": "error",
                }
            ],
        },
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "invalid_case", body
    assert "不接受字段定位" in body["message"]


def test_selector_on_structured_source_still_saves(
    client: TestClient, account: dict, project: dict
) -> None:
    """收紧不能误伤真正需要定位的来源。"""
    base = _base(account, project)
    case = create_case(
        client, account, project, name="结构化来源定位",
        request={"method": "GET", "path": "/echo", "query_params": [], "headers": [],
                 "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "body-field",
                "target_source": "response.body",
                "selector": [{"kind": "key", "key": "service"}],
                "type": "exists",
                "parameters": {},
                "severity": "error",
            }
        ],
    )
    detail = client.get(f"{base}/cases/{case['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["assertions"][0]["selector"] == [{"kind": "key", "key": "service"}]


# —— F1：发布必须固化“用户看到的那一版”草稿 ——
#
# 发布把草稿固化成不可变快照，执行从此固定在该快照上。若发布不声明草稿修订号，
# “用户看到草稿 rev=3、点发布”与“另一个保存把草稿推进到 rev=4”之间的窗口会让
# rev=4 的内容被悄悄固化，而页面上显示的仍是 rev=3。下面覆盖窗口两侧与行锁。


def test_publish_rejects_stale_draft_revisions(client: TestClient, account: dict, project: dict) -> None:
    """读到的修订号已过期时，发布必须被拒绝，且不产生任何版本。"""
    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    assert created.status_code == 201, created.text
    case = created.json()
    assert case["rev"] == 1

    # 用户读到 rev=1；随后另一处保存把草稿推进到 rev=2。
    etag = client.get(f"{base}/cases/{case['id']}").headers["ETag"]
    updated = client.patch(
        f"{base}/cases/{case['id']}", json={"name": "被他人改名"}, headers={"If-Match": etag}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rev"] == 2

    # 仍按 rev=1 发布：必须冲突，不能把 rev=2 的内容当成 rev=1 固化。
    stale = client.post(
        f"{base}/cases/{case['id']}/publish",
        json={"side_effect": "read", "draft_rev": 1},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "draft_rev_conflict"

    versions = client.get(f"{base}/cases/{case['id']}/versions")
    assert versions.status_code == 200
    assert versions.json() == []


def test_publish_requires_explicit_draft_revision(client: TestClient, account: dict, project: dict) -> None:
    """不声明修订号就不能发布，否则等于“固化当前随便哪一版”。"""
    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    assert created.status_code == 201, created.text
    case = created.json()

    missing = client.post(f"{base}/cases/{case['id']}/publish", json={"side_effect": "read"})
    assert missing.status_code == 422, missing.text


def test_publish_freezes_the_declared_revision_content(
    client: TestClient, account: dict, project: dict
) -> None:
    """按最新修订号发布时成功，快照内容与声明的这一版一致。"""
    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    case = created.json()

    etag = client.get(f"{base}/cases/{case['id']}").headers["ETag"]
    client.patch(f"{base}/cases/{case['id']}", json={"name": "最终名"}, headers={"If-Match": etag})

    current = client.get(f"{base}/cases/{case['id']}")
    assert current.json()["rev"] == 2
    published = client.post(
        f"{base}/cases/{case['id']}/publish",
        json={"side_effect": "read", "draft_rev": current.json()["rev"]},
    )
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 1


def test_publish_locks_the_draft_row_before_checking_revision(
    client: TestClient, account: dict, project: dict
) -> None:
    """发布必须在行锁内核对修订号，不能被“尚未提交的另一次保存”骗过。

    构造确定性交错：另一个事务已把草稿推进到 rev=2 但尚未提交，用户仍按 rev=1 发布。

    - 加锁读：发布阻塞在行锁上，等对方提交后重新读到 rev=2，于是收到冲突、不产生版本。
    - 不加锁读：普通 SELECT 看不到未提交的 rev=2，于是校验通过并返回 201，
      把一个已被并发修改的草稿固化出去——正是要防的发布竞态。

    因此这条测试的判别依据是结束后的 409 而不是 201；不是靠 sleep 猜时序，而是
    先断言发布确实等在该事务的行锁上（pg_stat_activity 的真实等待事件）。
    """
    import threading
    import time

    base = _base(account, project)
    created = client.post(f"{base}/cases", json=_case_payload())
    assert created.status_code == 201, created.text
    case_id = created.json()["id"]
    stale_rev = created.json()["rev"]

    holder = migrator_connection()
    holder.autocommit = False
    with holder.cursor() as cursor:
        # 业务表是 FORCE ROW LEVEL SECURITY，即使表所有者也要有租户上下文才看得到行；
        # 没有上下文时 UPDATE 匹配不到行，构造不出“他人正在改这一行”的状态。
        cursor.execute(
            "SELECT set_config('app.workspace_id', %s, true)", (str(account["workspace_id"]),)
        )
        cursor.execute(
            "UPDATE app.cases SET rev = rev + 1, name = %s WHERE id = %s RETURNING rev",
            ("另一个保存提交的名字", case_id),
        )
        row = cursor.fetchone()
        assert row is not None, "测试未取到用例行"
    # 事务故意不提交：这一行现在被未提交的写入锁住。

    outcome: dict[str, object] = {}

    def do_publish() -> None:
        try:
            outcome["response"] = client.post(
                f"{base}/cases/{case_id}/publish",
                json={"side_effect": "read", "draft_rev": stale_rev},
            )
        except BaseException as error:  # noqa: BLE001 - 线程内异常必须带回主线程
            outcome["error"] = error
            import traceback

            outcome["traceback"] = traceback.format_exc()

    worker = threading.Thread(target=do_publish)
    worker.start()

    # 行级锁在 pg_locks 里是 transactionid 条目，等待方表现为 wait_event='transactionid'。
    # 不能按 pg_locks.database 过滤：事务级锁的 database 列为 NULL，JOIN pg_database
    # 会把要观察的那一行滤掉，查询恒为空、断言永远看不到阻塞。
    watcher = migrator_connection()
    waited = False
    seen: list[tuple] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with watcher.cursor() as cursor:
            cursor.execute(
                "SELECT a.pid, a.state, a.wait_event_type, a.wait_event"
                " FROM pg_stat_activity a"
                " WHERE a.wait_event = 'transactionid'"
                " AND a.datname = current_database()"
            )
            rows = cursor.fetchall()
            if rows:
                seen.extend(rows)
                waited = True
                break
        if not worker.is_alive():
            break
        time.sleep(0.05)
    blocked = worker.is_alive()
    watcher.close()

    assert waited, f"发布没有等待持有该行写锁的事务，说明未在锁内核对修订号（观察 {seen}）"
    assert blocked, "发布在行锁被他人持有期间就已返回，说明未加锁读"

    holder.commit()
    holder.close()
    worker.join(timeout=30)
    assert not worker.is_alive(), "发布请求在释放行锁后仍未返回"
    assert "error" not in outcome, f"发布线程异常：{outcome.get('traceback')}"
    response = outcome["response"]
    # 锁释放后按 READ COMMITTED 重新读取，看到的已是 rev=2，声明的 rev=1 不再成立。
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "draft_rev_conflict"

    versions = client.get(f"{base}/cases/{case_id}/versions")
    assert versions.status_code == 200
    assert versions.json() == []
