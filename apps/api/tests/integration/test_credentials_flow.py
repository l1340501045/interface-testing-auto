"""身份凭证闭环集成：秘密加密入库、按授权注入、受保护值不可被断言。

本模块补齐 PRD 第 8 项与 SC-09 的可观察证据：此前 `services/credentials.py` 的
加密、集合切换与授权解析虽已实现，却没有对外入口，因此受保护值保护无法经真实
产品路径验证。这里全部走真实 HTTP 管理 API + 真实 worker 执行 + compose 网络内
的受控目标服务。

对应 SC-09（拒绝跨项目凭证、受保护值断言；日志／快照／结果无明文凭证副本）
与 PRD 第 8 项（秘密加密、值不回显、限定用例版本与允许来源）。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from harness import (
    CONTROLLED_TARGET_BASE_URL,
    PASSWORD,
    create_case,
    create_environment,
    get_report,
    migrator_connection,
    project_base,
    publish_case,
    seed_account,
    start_run,
)

pytestmark = pytest.mark.integration

# 测试用秘密值：只在请求体中出现一次，之后任何接口与报告都不得再见到它。
# 必须限定 ASCII：认证槽位注入的是 HTTP 请求头，中文等非 ASCII 值无法按 HTTP
# 头传输——这条边界本身由 tests/test_request_wire_headers.py 单独覆盖。
PLAIN_SECRET = "demo-token-7f3a91c0-it-fixture"
DEMO_SLOT = "header.X-Demo-Token"


def _settings(worker_id: str = "it-cred-worker"):
    from dataclasses import replace

    from app.config import get_settings

    return replace(get_settings(), worker_id=worker_id)


def _run_once(pool_id: str, worker_id: str = "it-cred-worker", settings=None) -> str:
    """与 `python -m app.worker` 完全同一份领取与执行内核。

    `settings` 只在需要改变执行参数（例如把读超时压到毫秒级以复现真实发送失败）时
    传入；默认仍是进程当前配置，避免各处各带一套参数。
    """
    import uuid as _uuid

    from app.db import get_session_factory
    from app.services.executor import claim_job, execute_claim

    session = get_session_factory()()
    try:
        claim = claim_job(session, worker_id, [_uuid.UUID(pool_id)], 30)
        session.commit()
    finally:
        session.close()
    assert claim is not None, "执行池中应有可领取的工作项"
    return execute_claim(get_session_factory(), settings or _settings(worker_id), claim)


def _clear_grant_input_digest(grant_id: str) -> None:
    """把某条授权的输入摘要退回空值，复现迁移之前签发的历史授权。

    迁移不回填历史行，因此“未绑定输入”是真实存在的状态，必须有确定的回归覆盖。
    """
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.credential_use_grants SET input_digest = NULL WHERE id = %s",
                (grant_id,),
            )
        connection.commit()
    finally:
        connection.close()


def _grant_input_digest(grant_id: str) -> str | None:
    """读取授权冻结的输入摘要；用于确认签发时确实写入了绑定。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT input_digest FROM app.credential_use_grants WHERE id = %s",
                (grant_id,),
            )
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _claim_job(pool_id: str, worker_id: str = "it-cred-worker"):
    """单独领取一个工作项，供“领取之后租约失效”这类交错测试使用。"""
    from app.db import get_session_factory
    from app.services.executor import claim_job

    session = get_session_factory()()
    try:
        claim = claim_job(session, worker_id, [uuid.UUID(pool_id)], 30)
        session.commit()
        return claim
    finally:
        session.close()


def _execute_claim(claim, worker_id: str = "it-cred-worker") -> str:
    from app.db import get_session_factory
    from app.services.executor import execute_claim

    return execute_claim(get_session_factory(), _settings(worker_id), claim)


def _expire_lease(run_id: str) -> None:
    """把租约回拨到过去，复现“租约已过期但尚未被重新领取”，不需要真实等待。"""
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


def _grant_used_at(grant_id: str):
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT used_at FROM app.credential_use_grants WHERE id = %s", (grant_id,)
            )
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _step_send_intent_at(run_id: str):
    """本次运行是否已经持久化发送意图；用于确认拦截发生在准入点而不是更早。"""
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT max(send_intent_at) FROM app.run_step_attempts WHERE run_id = %s",
                (run_id,),
            )
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _run_rows_as_text(run_id: str) -> str:
    """把本次运行涉及的全部数据库行拼成文本，供“不得出现明文”的统一断言。

    只看接口返回的报告是不够的：真正落库的内容才是秘密泄露的长期载体，而报告可能
    只回传了其中一部分字段。这里整行取出，不挑字段。
    """
    connection = migrator_connection()
    try:
        chunks: list[str] = []
        with connection.cursor() as cursor:
            cursor.execute("SELECT row_to_json(t)::text FROM app.runs t WHERE id = %s", (run_id,))
            chunks.extend(row[0] for row in cursor.fetchall())
            for table in ("run_step_attempts", "assertion_results"):
                cursor.execute(
                    f"SELECT row_to_json(t)::text FROM app.{table} t WHERE run_id = %s",
                    (run_id,),
                )
                chunks.extend(row[0] for row in cursor.fetchall())
        return "\n".join(chunks)
    finally:
        connection.close()


def _derived_rows_as_text(run_id: str) -> str:
    """只取运行里**由平台派生**的证据行：断言结果与步骤请求／响应证据。

    与 `_run_rows_as_text` 的区别是排除了运行行自身保存的用例／环境快照：那里面是
    用户自己写下的配置（他完全可以在某个字段路径上写出与秘密相同的名字或值），按原样
    保存属于“用户输入”，不是平台根据响应生成的副本。这两处要查的是平台自己写出来的
    那一份——定位信息、期望／实际、错误文本。
    """
    connection = migrator_connection()
    try:
        chunks: list[str] = []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT row_to_json(t)::text FROM app.assertion_results t WHERE run_id = %s"
                " ORDER BY assertion_id",
                (run_id,),
            )
            chunks.extend(row[0] for row in cursor.fetchall())
            cursor.execute(
                "SELECT row_to_json(t)::text FROM app.run_step_attempts t WHERE run_id = %s",
                (run_id,),
            )
            chunks.extend(row[0] for row in cursor.fetchall())
        return "\n".join(chunks)
    finally:
        connection.close()


def _captured_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """把捕获到的日志连同格式化参数拼成文本：参数里同样可能夹带明文。"""
    return "\n".join(
        f"{record.getMessage()} | {record.args!r}" for record in caplog.records
    )


def _decoded_forms(text: str) -> list[str]:
    """文本的常规解码形态：百分号解码与表单解码（`+` 还原成空格）。

    查询串按 `quote_plus` 编码，凭据里的空格上线后是一个 `+`。只做 `unquote`
    的话，`a%2Bb%2Fc%3D+d+e` 解出的是 `a+b/c=+d+e`——与原文 `a+b/c= d e` 只差
    一个空格，于是一份真实的明文泄露能整段躲过“解码能否还原”的断言。两种形态
    都要试：判别说的是“解不解得回来”，不是“哪一种解码器认得出来”。
    """
    from urllib.parse import unquote, unquote_plus

    return [unquote(text), unquote_plus(text)]


def _create_secret(client: TestClient, base: str, name: str, value: str) -> dict:
    response = client.post(
        f"{base}/credentials/secrets", json={"name": name, "value": value}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_profile(client: TestClient, base: str, environment_id: str, *, name: str) -> dict:
    response = client.post(
        f"{base}/credentials/profiles",
        json={
            "environment_id": environment_id,
            "name": name,
            "allowed_targets": [CONTROLLED_TARGET_BASE_URL],
            "allowed_auth_slots": [DEMO_SLOT],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _configure_credential(
    client: TestClient,
    base: str,
    *,
    environment_id: str,
    secret_version_id: str,
    name: str = "受控目标身份",
) -> dict:
    """一步到位：建立身份、写入配置版本、按槽位绑定秘密版本并激活集合。"""
    profile = _create_profile(client, base, environment_id, name=name)
    version = client.post(
        f"{base}/credentials/profiles/{profile['id']}/versions",
        json={"config": {"auth_slot": DEMO_SLOT, "failure_criteria": {"status": [401]}}},
    )
    assert version.status_code == 201, version.text
    activated = client.put(
        f"{base}/credentials/profiles/{profile['id']}/set",
        json={"slots": {DEMO_SLOT: secret_version_id}},
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def _grant_case_version(
    client: TestClient, base: str, *, profile_id: str, principal_id: str, case_version_id: str
) -> dict:
    response = client.post(
        f"{base}/credentials/grants",
        json={
            "profile_id": profile_id,
            "grant_type": "case_version",
            "principal_id": principal_id,
            "case_version_id": case_version_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# —— 正向：秘密加密入库并按授权注入受控目标 ——


def test_secret_is_encrypted_and_injected_into_controlled_target(
    client: TestClient, account: dict, project: dict
) -> None:
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="凭证注入环境")

    secret = _create_secret(client, base, "虚拟令牌", PLAIN_SECRET)
    assert secret["latest_version"] == 1
    # 写入响应只回元数据（含可绑定的版本 id），不回值。
    assert PLAIN_SECRET not in json.dumps(secret, ensure_ascii=False)

    rotated = client.post(
        f"{base}/credentials/secrets/{secret['id']}/versions", json={"value": PLAIN_SECRET}
    )
    assert rotated.status_code == 201, rotated.text
    assert rotated.json()["version"] == 2
    secret_version_id = rotated.json()["version_id"]

    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret_version_id
    )
    assert profile["status"] == "available"
    assert profile["slot_count"] == 1
    assert PLAIN_SECRET not in json.dumps(profile, ensure_ascii=False)

    case = create_case(
        client,
        account,
        project,
        name="需要认证头的用例",
        request={"method": "GET", "path": "/require-header", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "status-ok",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "passed"

    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200, "认证头确实被注入，受控目标放行"
    # 受控目标会把收到的令牌回显到正文；脱敏必须覆盖响应证据，而不只是请求。
    dumped = json.dumps(report, ensure_ascii=False)
    assert PLAIN_SECRET not in dumped, "报告任何位置都不得出现明文凭证"
    header_names = {item["name"].lower() for item in report["request"]["headers"]}
    assert "x-demo-token" in header_names, "注入的认证头应体现在请求证据中（值已遮蔽）"


def test_declared_value_prefix_is_applied_to_the_injected_value(
    client: TestClient, account: dict, project: dict
) -> None:
    """身份声明里的值前缀必须真的作用到注入值上。

    界面把 Bearer Token 表达成「放在 Authorization、值前加 `Bearer `」，配置快照里也
    如实存了 `auth_locations[].prefix`。曾经执行器只注入秘密原文：表单上写着 Bearer，
    发出去却是裸值，真实服务收到的 Authorization 已经不是声明的那个方案——配了却不
    生效，要等被目标拒绝才暴露。

    这里不比较秘密本身：只看受控目标回显的、已脱敏的值里，方案前缀是否还在秘密之前。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="值前缀环境")
    secret = _create_secret(client, base, "值前缀令牌", PLAIN_SECRET)

    created = client.post(
        f"{base}/credentials/profiles",
        json={
            "environment_id": environment["id"],
            "name": "Bearer 身份",
            "allowed_targets": [CONTROLLED_TARGET_BASE_URL],
            "allowed_auth_slots": ["header.Authorization"],
            "config": {
                "auth_locations": [
                    {"slot": "header.Authorization", "scheme": "bearer", "prefix": "Bearer "}
                ],
                "invalidation": {"status_codes": [401], "redirect_to_login": False},
            },
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    activated = client.put(
        f"{base}/credentials/profiles/{profile['id']}/set",
        json={"slots": {"header.Authorization": secret["latest_version_id"]}},
    )
    assert activated.status_code == 200, activated.text

    case = create_case(
        client,
        account,
        project,
        name="值前缀用例",
        request={"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "status-ok",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "passed"

    report = get_report(client, account, project, run["id"])
    echoed = json.loads(report["response"]["body"])
    authorization = next(
        item for item in echoed["headers"] if item["name"].lower() == "authorization"
    )
    # 前缀和秘密一起发出去了：脱敏只吃掉秘密那一段，方案前缀仍在线上。
    assert authorization["value"] == "Bearer ***", authorization
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


QUERY_SLOT = "query.api_key"


def _run_query_auth_case(
    client: TestClient,
    account: dict,
    project: dict,
    base: str,
    *,
    tag: str,
    extra_assertions: list[dict] | None = None,
    query_params: list[dict[str, str]] | None = None,
    secret_value: str = PLAIN_SECRET,
    path: str = "/echo",
    settings=None,
) -> tuple[str, dict]:
    """建一套“查询参数认证”的完整链路并执行一次，返回（执行结论, 运行报告）。

    查询参数认证是界面上可选的一种认证方式（“认证方式：查询参数”）。既然接受这种
    配置，注入值就必须和请求头一样真正写进 URL：只在断言取值上下文里出现、请求里
    没有，等于让用户在一个虚构值上核对结果。

    `secret_value`／`path`／`settings` 供“必须编码才能上线的凭据”这类用例换值、
    换目标路径与换执行参数；默认与既有用例完全一致。
    """
    environment = create_environment(client, account, project, name=f"查询认证环境-{tag}")
    secret = _create_secret(client, base, f"查询认证令牌-{tag}", secret_value)
    created = client.post(
        f"{base}/credentials/profiles",
        json={
            "environment_id": environment["id"],
            "name": f"查询参数身份-{tag}",
            "allowed_targets": [CONTROLLED_TARGET_BASE_URL],
            "allowed_auth_slots": [QUERY_SLOT],
            "config": {
                "auth_locations": [{"slot": QUERY_SLOT, "scheme": "query", "prefix": ""}],
                "invalidation": {"status_codes": [401], "redirect_to_login": False},
            },
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    activated = client.put(
        f"{base}/credentials/profiles/{profile['id']}/set",
        json={"slots": {QUERY_SLOT: secret["latest_version_id"]}},
    )
    assert activated.status_code == 200, activated.text

    case = create_case(
        client,
        account,
        project,
        name=f"查询认证用例-{tag}",
        request={
            "method": "GET",
            "path": path,
            "query_params": query_params or [],
            "body_type": "none",
            "body": "",
        },
        assertions=[
            {
                "id": "status-ok",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            },
            *(extra_assertions or []),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    outcome = _run_once(project["pool_id"], settings=settings)
    return outcome, get_report(client, account, project, run["id"])


def test_query_auth_injection_reaches_the_wire(client: TestClient, account: dict, project: dict) -> None:
    """查询参数认证注入必须真的出现在发出去的 URL 上。

    此前 `Injection.query` 只并入断言取值来源、不随 `prepare()` 上线：界面接受
    “查询参数”这种认证方式，断言却在一个根本没发出去的虚构值上核对。这里要求它
    与请求头走同一条组装路径——受控目标回显的查询参数就是线上下来的证据。
    """
    outcome, report = _run_query_auth_case(
        client, account, project, project_base(account, project), tag="wire"
    )
    assert outcome == "passed", outcome

    echoed = json.loads(report["response"]["body"])
    sent = [item for item in echoed["query"] if item["name"] == "api_key"]
    assert sent, f"目标没有收到注入的查询参数：{echoed['query']}"
    assert PLAIN_SECRET not in sent[0]["value"], "回显的注入值必须已脱敏"
    # 请求证据里的 URL 同样只保留脱敏后的形态，秘密不因改走查询参数而失去保护。
    assert "api_key=***" in report["request"]["url"], report["request"]["url"]
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_query_auth_injection_keeps_repeated_and_encoded_parameters(
    client: TestClient, account: dict, project: dict
) -> None:
    """注入不能挤掉普通查询参数：重复项、顺序与编码都要原样保留。

    注入值与用户配置的参数走同一条组装路径，最容易踩的是“为了塞进注入值而把
    查询参数折叠成字典”——那样同名的第二个参数会被吃掉、顺序也会变。这里让受控
    目标回显它实际收到的解码后序列：值里带 `&`、`=`、空格与中文，只要线上编码有
    一处不对，回显还原出来的就不是原值。
    """
    configured = [
        {"name": "tag", "value": "a b&c=d"},
        {"name": "tag", "value": "中文/值"},
        {"name": "empty", "value": ""},
    ]
    outcome, report = _run_query_auth_case(
        client, account, project, project_base(account, project), tag="repeats", query_params=configured
    )
    assert outcome == "passed", outcome

    echoed = json.loads(report["response"]["body"])["query"]
    # 顺序按配置原样，重复的 tag 两次都在，注入项追加在用户参数之后。
    assert [item["name"] for item in echoed] == ["tag", "tag", "empty", "api_key"], echoed
    assert [item["value"] for item in echoed[:3]] == ["a b&c=d", "中文/值", ""], echoed
    # 注入值仍然脱敏，秘密不因同行的普通参数而失去保护。
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    assert "api_key=***" in report["request"]["url"], report["request"]["url"]


@pytest.mark.parametrize("alias", ["name", "index"])
def test_injected_query_parameter_cannot_be_probed(
    client: TestClient, account: dict, project: dict, alias: str
) -> None:
    """注入的查询参数是受保护值：按名字或按下标访问都不得用它下结论。

    断言取值来源与线上同源之后，按下标访问拿到的是同一个坐标；两条别名都必须在
    求值前被拦下，而不是让布尔结果回答“秘密是否以某前缀开头”。
    """
    base = project_base(account, project)
    # 先实测一次，量出注入参数在回显数组里的真实下标——不写死，也不靠猜。
    outcome, probe_report = _run_query_auth_case(
        client, account, project, base, tag=f"probe-{alias}"
    )
    assert outcome == "passed", outcome
    echoed = json.loads(probe_report["response"]["body"])
    index = next(
        position for position, item in enumerate(echoed["query"]) if item["name"] == "api_key"
    )

    # repeat_key 这一步直接命中该重复项的值；index 这一步命中的是整项，需再取 value。
    selector = (
        [{"kind": "repeat_key", "key": "api_key", "occurrence": 0}]
        if alias == "name"
        else [{"kind": "index", "index": index}, {"kind": "key", "key": "value"}]
    )
    outcome, report = _run_query_auth_case(
        client,
        account,
        project,
        base,
        tag=f"assert-{alias}",
        extra_assertions=[
            {
                "id": "probe-query",
                "target_source": "request.query",
                "selector": selector,
                "type": "contains",
                "parameters": {"expected": {"type": "string", "text": PLAIN_SECRET[:6]}},
                "severity": "error",
            }
        ],
    )
    # 认证值在发送前就已并入请求，请求会真实发出；被拦下的是“拿这个值下结论”。
    assert outcome == "error", outcome
    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe-query")
    assert probe["reason_code"] == "policy_rejected", probe
    assert probe["actual"] is None and probe["expected"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_secret_plaintext_never_stored_in_database(
    client: TestClient, account: dict, project: dict
) -> None:
    """密文验证：库里只有密文，明文不可从 `secret_versions` 读出。"""
    base = project_base(account, project)
    _create_secret(client, base, "落库校验秘密", PLAIN_SECRET)

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT encrypted_value FROM app.secret_versions WHERE workspace_id = %s",
                (account["workspace_id"],),
            )
            rows = [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()
    assert rows, "秘密版本应已落库"
    for stored in rows:
        assert isinstance(stored, bytes)
        assert PLAIN_SECRET.encode() not in stored, "数据库中不得出现明文凭证"


# —— 反向：受保护值不能被普通断言当作猜测目标 ——


def test_assertion_on_injected_header_is_rejected_without_sending(
    client: TestClient, account: dict, project: dict
) -> None:
    """对注入后的认证头发起断言时统一策略拒绝，且不发出 HTTP。

    定位用 `repeat_key`（字段树对请求头这类 {name,value} 数组发出的就是这种步骤），
    它与按下标访问指向同一个值，因此都必须落在同一条保护规则里。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="受保护值环境")
    secret = _create_secret(client, base, "受保护令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    assert profile["status"] == "available"

    case = create_case(
        client,
        account,
        project,
        name="对认证头断言",
        request={"method": "GET", "path": "/require-header", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "probe-secret",
                "target_source": "request.header",
                "selector": [{"kind": "repeat_key", "key": "X-Demo-Token", "occurrence": 0}],
                "type": "starts_with",
                "parameters": {"expected": {"type": "string", "text": "demo"}},
                "severity": "error",
            },
            {
                "id": "status-ok",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            },
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    outcome = _run_once(project["pool_id"])
    assert outcome == "error", outcome

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "configuration"
    # 未发出请求：没有响应证据，响应断言记为未执行。
    assert report["response"] is None
    statuses = {item["assertion_id"]: item["status"] for item in report["assertions"]}
    assert statuses["probe-secret"] == "error"
    assert statuses["status-ok"] == "skipped"
    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe-secret")
    assert probe["reason_code"] == "policy_rejected"
    # 拒绝信息不得携带秘密本身，也不返回可用于猜测的期望／实际值。
    assert PLAIN_SECRET not in json.dumps(probe, ensure_ascii=False)
    assert probe["actual"] is None and probe["expected"] is None


@pytest.mark.parametrize(
    ("case_id", "selector"),
    [
        # 同一个认证头：按名字（字段树对请求头发出的就是 repeat_key）与按下标是
        # 两次合法取值，必须落在同一个受保护位置。
        ("repeat_key", [{"kind": "repeat_key", "key": "X-Demo-Token", "occurrence": 0}]),
        ("index_value", [{"kind": "index", "index": 0}, {"kind": "key", "key": "value"}]),
        ("index_item", [{"kind": "index", "index": 0}]),
    ],
)
def test_secret_cannot_be_probed_through_any_addressing_alias(
    client: TestClient, account: dict, project: dict, case_id: str, selector: list[dict]
) -> None:
    """定位别名必须与敏感标记落在同一坐标，否则布尔结果本身就成为猜秘密的通道。

    同一个回答有两种合法写法——重复键按名字、或按数组下标。标记只认其中一种时，
    另一种就能读到受保护的值，并借“通过／不通过”反推它的内容。这里逐个别名验证
    它们指向同一个受保护位置。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name=f"别名环境-{case_id}")
    secret = _create_secret(client, base, f"别名令牌-{case_id}", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    assert profile["status"] == "available"

    case = create_case(
        client,
        account,
        project,
        name=f"别名用例-{case_id}",
        # 不配置请求头：注入的认证头是最终头列表里的第 0 项，两种定位别名
        # （按名字、按下标）指向的是同一个位置。
        request={"method": "GET", "path": "/require-header", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "probe",
                "target_source": "request.header",
                "selector": selector,
                "type": "contains",
                "parameters": {"expected": {"type": "string", "text": PLAIN_SECRET[:6]}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    outcome = _run_once(project["pool_id"])
    assert outcome == "error", outcome

    report = get_report(client, account, project, run["id"])
    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe")
    assert probe["reason_code"] == "policy_rejected", probe
    assert probe["actual"] is None and probe["expected"] is None
    # 入参断言在发送前求值：被拒绝时没有任何响应证据，即未发出请求。
    assert report["response"] is None, report["response"]
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_repeated_header_occurrences_are_addressed_individually(
    client: TestClient, account: dict, project: dict
) -> None:
    """重复头与安全兄弟：保护按出现位置成立，且不外溢到同一列表里的普通头。

    同名头出现两次时，两次是各自独立的取值坐标；本用例让普通头重复，同时环境里
    还注入着一个认证头。若保护用的是“整行都敏感”的粗粒度判定、或不区分出现位置，
    这两条针对普通头的正常断言会被一并拒绝。这里验证它们仍能正常求值——保护只
    覆盖真正含秘密的坐标（认证头那一项交给上面的别名用例逐项验证被拒绝）。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="重复头环境")
    secret = _create_secret(client, base, "重复头令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    assert profile["status"] == "available"

    case = create_case(
        client,
        account,
        project,
        name="重复头用例",
        request={
            "method": "GET",
            "path": "/require-header",
            "body_type": "none",
            "body": "",
            # 同名普通头出现两次：两个 occurrence 各是一个坐标。
            "headers": [
                {"name": "X-Trace-Id", "value": "trace-first"},
                {"name": "X-Trace-Id", "value": "trace-second"},
            ],
        },
        assertions=[
            {
                "id": "trace-first",
                "target_source": "request.header",
                "selector": [{"kind": "repeat_key", "key": "X-Trace-Id", "occurrence": 0}],
                "type": "equals",
                "parameters": {"expected": {"type": "string", "text": "trace-first"}},
                "severity": "error",
            },
            {
                "id": "trace-second",
                "target_source": "request.header",
                "selector": [{"kind": "repeat_key", "key": "X-Trace-Id", "occurrence": 1}],
                "type": "equals",
                "parameters": {"expected": {"type": "string", "text": "trace-second"}},
                "severity": "error",
            },
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    # 两条都是入参断言，响应侧没有可校验的断言，因此终态是“未校验通过”
    # （completed_unchecked）而不是 passed——这里要证明的是两条都正常求值通过，
    # 没有被敏感保护误伤。
    assert _run_once(project["pool_id"]) == "completed_unchecked"

    report = get_report(client, account, project, run["id"])
    statuses = {item["assertion_id"]: item["status"] for item in report["assertions"]}
    assert statuses == {"trace-first": "passed", "trace-second": "passed"}, report["assertions"]
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def _echo_header_index(
    client: TestClient, account: dict, project: dict, base: str, tag: str
) -> tuple[int, str]:
    """在受控目标上实测一次回显，取得认证头在正文 `headers` 数组里的下标与名字。

    下标与名字都取决于客户端实际发出去的那一份头，不是可以写死的常量：下标前有
    host／accept 等客户端自带的头，名字在线上是小写形态。先用一次结构探针把两者
    量出来，再拿它们构造按名字／按下标访问的断言——不靠猜，也不靠 sleep。
    """
    environment = create_environment(client, account, project, name=f"回显结构探针环境-{tag}")
    secret = _create_secret(client, base, f"回显结构探针令牌-{tag}", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    case = create_case(
        client,
        account,
        project,
        name=f"回显结构探针用例-{tag}",
        request={"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "probe-structure",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "passed"

    report = get_report(client, account, project, run["id"])
    echoed = json.loads(report["response"]["body"])
    for index, item in enumerate(echoed["headers"]):
        if item["name"].lower() == "x-demo-token":
            # 值已被脱敏成 ***，但位置与名称不受影响，足以定位真实坐标。
            assert PLAIN_SECRET not in item["value"], "证据里的秘密必须已脱敏"
            return index, item["name"]
    raise AssertionError(f"回显正文里没有认证头，结构探针失效：{echoed['headers']}")


@pytest.mark.parametrize("case_id", ["echo_repeat_key", "echo_index"])
def test_echoed_secret_in_a_name_value_array_cannot_be_probed(
    client: TestClient, account: dict, project: dict, case_id: str
) -> None:
    """目标把注入的认证头原样回显到正文：在回显结构上取它同样属于受保护值。

    回显位置不是注入位置，只按“注入到哪里”标注就保护不到它。回显出来的 `headers`
    是普通 JSON 数组，不是请求头映射，因此不能整体当成头部映射来判敏感；但按真实
    坐标（对象字段名／数组下标）标注后，同一个回显项无论按名字还是按下标取，都要
    落在同一个受保护坐标上。
    """
    base = project_base(account, project)
    index, echoed_name = _echo_header_index(client, account, project, base, case_id)

    environment = create_environment(client, account, project, name=f"回显数组环境-{case_id}")
    secret = _create_secret(client, base, f"回显令牌-{case_id}", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    assert profile["status"] == "available"

    if case_id == "echo_repeat_key":
        selector = [
            {"kind": "key", "key": "headers"},
            {"kind": "repeat_key", "key": echoed_name, "occurrence": 0},
        ]
    else:
        selector = [
            {"kind": "key", "key": "headers"},
            {"kind": "index", "index": index},
            {"kind": "key", "key": "value"},
        ]

    case = create_case(
        client,
        account,
        project,
        name=f"回显数组用例-{case_id}",
        request={"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        assertions=[
            {
                "id": "probe-echo",
                "target_source": "response.body",
                "selector": selector,
                "type": "contains",
                "parameters": {"expected": {"type": "string", "text": PLAIN_SECRET[:6]}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    # 出参断言在响应之后求值，请求会真实发出——被拦下的是“拿这个值下结论”。
    outcome = _run_once(project["pool_id"])
    assert outcome == "error", outcome

    report = get_report(client, account, project, run["id"])
    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe-echo")
    assert probe["reason_code"] == "policy_rejected", probe
    assert probe["actual"] is None and probe["expected"] is None
    # 报告与证据里都不允许出现秘密明文：脱敏发生在写入证据与结果之前。
    dumped = json.dumps(report, ensure_ascii=False)
    assert PLAIN_SECRET not in dumped
    assert "***" in dumped, "回显的秘密应当以脱敏形态出现在证据里"


# —— 反向：凭证的使用受授权范围约束 ——


def test_run_without_grant_is_rejected_before_send(
    client: TestClient, account: dict, project: dict
) -> None:
    """没有用途授权时不得使用凭证执行，运行以认证错误终止且不发请求。"""
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="未授权环境")
    secret = _create_secret(client, base, "未授权令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client, base, environment_id=environment["id"], secret_version_id=secret["latest_version_id"]
    )
    assert profile["status"] == "available"

    case = create_case(
        client,
        account,
        project,
        name="未授权用例",
        request={"method": "GET", "path": "/require-header", "body_type": "none", "body": ""},
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "authentication"
    # 稳定错误码落在步骤尝试上；运行只带中文可读的原因分类。
    assert [step["error_code"] for step in report["steps"]] == ["credential_not_granted"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_grant_scope_cannot_exceed_profile(
    client: TestClient, account: dict, project: dict
) -> None:
    """授权只能收窄：声明外的认证槽位必须被拒绝，而不是被静默接受。"""
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="越权环境")
    profile = _create_profile(client, base, environment["id"], name="范围受限身份")
    case = create_case(
        client,
        account,
        project,
        name="占位用例",
        request={"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])

    response = client.post(
        f"{base}/credentials/grants",
        json={
            "profile_id": profile["id"],
            "grant_type": "case_version",
            "principal_id": str(account["user_id"]),
            "case_version_id": version["id"],
            "allowed_auth_slots": ["header.X-Not-Declared"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "grant_scope_wider_than_profile"


def _set_environment_variables(
    client: TestClient, account: dict, project: dict, environment_id: str, variables: dict
) -> None:
    """按界面同样的入口写入环境普通变量；这是请求模板 `{{...}}` 的真正输入。"""
    response = client.patch(
        f"{project_base(account, project)}/environments/{environment_id}",
        json={"variables": variables},
    )
    assert response.status_code == 200, response.text


def test_grant_is_not_reusable_after_request_inputs_change(
    client: TestClient, account: dict, project: dict
) -> None:
    """模板一个字符都没改，只改普通变量：原授权不得再用于另一条请求。

    授权的定位符只有用例版本 id，而请求里的 `{{...}}` 由项目／环境普通变量在运行时
    解析。授权若不绑定这些输入，管理员为 `/{{operation}}` 签发的凭证在变量被改成
    别的路径后仍会被注入——同一份授权就此用在一条从未被批准的请求上。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="变量绑定环境")
    secret = _create_secret(client, base, "变量绑定令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="变量绑定身份",
    )
    _set_environment_variables(
        client, account, project, environment["id"],
        {"operation": {"type": "string", "text": "require-header"}},
    )
    case = create_case(
        client,
        account,
        project,
        name="变量路径用例",
        request={"method": "GET", "path": "/{{operation}}", "body_type": "none", "body": ""},
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    # 同一份输入照常执行：绑定输入不等于禁用授权。
    start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) != "error"

    # 只把变量指向另一条路径，模板与授权行都没有变。
    _set_environment_variables(
        client, account, project, environment["id"],
        {"operation": {"type": "string", "text": "echo"}},
    )
    second = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, second["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_inputs_changed"]
    assert report["response"] is None, "输入变化之后不得产生任何目标副作用"
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def _set_operation(client: TestClient, account: dict, project: dict, environment_id: str, text: str) -> None:
    """把路径模板变量指向某条受控路径；这是授权摘要绑定里的“输入 A／B”。"""
    _set_environment_variables(
        client,
        account,
        project,
        environment_id,
        {"operation": {"type": "string", "text": text}},
    )


def test_grant_is_bound_to_the_inputs_frozen_for_this_run(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 A 授权 → 改成 B 入队 → 执行前恢复 A：必须拒绝，且一次请求都不发。

    授权摘要若只跟“当前环境变量”比，这条路径就会漏过去：签发时是 A、当前又变回
    A，检查全部通过，而本次运行真正要用的输入是入队那一刻冻结进快照的 B。于是
    管理员批准的是 `/require-header`，发出去的却是 `/echo`。
    判定必须落在“本次运行实际冻结的这批变量”上；当前值一致不足以放行。
    """
    from app.services import executor

    base = project_base(account, project)
    environment = create_environment(client, account, project, name="冻结输入环境")
    secret = _create_secret(client, base, "冻结输入令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="冻结输入身份",
    )
    _set_operation(client, account, project, environment["id"], "require-header")
    case = create_case(
        client,
        account,
        project,
        name="冻结输入用例",
        request={"method": "GET", "path": "/{{operation}}", "body_type": "none", "body": ""},
        assertions=_debug_assertions(),
    )
    version = publish_case(client, account, project, case["id"])
    grant = _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    # 正向对照：授权时是 A、本次运行冻结的也是 A，照常执行。
    matching = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "passed"
    assert [url.rsplit("/", 1)[-1] for url in sent] == ["require-header"]
    assert get_report(client, account, project, matching["id"])["response"]["status"] == 200

    # 故障点：按 A 授权之后改成 B 并入队，执行前再把当前值恢复成 A。
    _set_operation(client, account, project, environment["id"], "echo")
    mismatched = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    _set_operation(client, account, project, environment["id"], "require-header")

    assert _run_once(project["pool_id"]) == "error"
    assert [url.rsplit("/", 1)[-1] for url in sent] == ["require-header"], (
        "只应发出正向对照的那一次请求：冻结输入与授权不一致时不得发送"
    )

    report = get_report(client, account, project, mismatched["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_inputs_changed"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    # 拦截发生在发送意图之前，且一次性授权没有被这次失败消费掉。
    assert _step_send_intent_at(mismatched["id"]) is None
    assert _grant_used_at(grant["id"]) is None


def test_debug_grant_is_bound_to_the_inputs_frozen_for_this_run(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一条不变量对调试授权同样成立：摘要按冻结输入判定，不看当前值。"""
    from app.services import executor

    base = project_base(account, project)
    environment = create_environment(client, account, project, name="调试冻结输入环境")
    secret = _create_secret(client, base, "调试冻结令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="调试冻结身份",
    )
    _set_operation(client, account, project, environment["id"], "require-header")
    snapshot = {
        "request": {"method": "GET", "path": "/{{operation}}", "body_type": "none", "body": ""},
        "assertions": _debug_assertions(),
    }
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=_snapshot_digest(client, base, snapshot),
    )

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    # 故障点：授权之后把变量改成 B 并入队，执行前再恢复成 A。
    _set_operation(client, account, project, environment["id"], "echo")
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": snapshot},
    )
    _set_operation(client, account, project, environment["id"], "require-header")

    assert _run_once(project["pool_id"]) == "error"
    assert sent == [], "冻结输入与调试授权不一致时不得发送任何请求"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_inputs_changed"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_grant_without_bound_inputs_is_rejected(
    client: TestClient, account: dict, project: dict
) -> None:
    """未绑定输入的旧授权（列可为空）必须按未授权处理，而不是照常注入。

    迁移不回填历史行：按当前变量回填等于替一份从未存在过的绑定伪造证据。这些行
    因此只能拒绝，让管理员重新签发。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="未绑定输入环境")
    secret = _create_secret(client, base, "未绑定令牌", PLAIN_SECRET)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="未绑定身份",
    )
    case = create_case(
        client,
        account,
        project,
        name="未绑定用例",
        request={"method": "GET", "path": "/require-header", "body_type": "none", "body": ""},
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])
    grant = _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )

    _clear_grant_input_digest(grant["id"])

    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert [step["error_code"] for step in report["steps"]] == ["credential_inputs_unbound"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


def test_unsupported_allowed_inputs_is_rejected_at_creation(
    client: TestClient, account: dict, project: dict
) -> None:
    """未实现的输入白名单必须明确拒绝，而不是收下再忽略。

    收下不校验等于向管理员承诺了一个不存在的保护：界面显示“已限制输入”，执行时
    却完全不看这份约束。
    """
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="输入白名单环境")
    profile = _create_profile(client, base, environment["id"], name="输入白名单身份")
    case = create_case(
        client,
        account,
        project,
        name="输入白名单用例",
        request={"method": "GET", "path": "/echo", "body_type": "none", "body": ""},
        assertions=[],
    )
    version = publish_case(client, account, project, case["id"])

    response = client.post(
        f"{base}/credentials/grants",
        json={
            "profile_id": profile["id"],
            "grant_type": "case_version",
            "principal_id": str(account["user_id"]),
            "case_version_id": version["id"],
            "allowed_inputs": {"operation": ["echo"]},
        },
    )
    # 按凭证服务的错误边界，未支持字段走 400（bad_request），错误码保持稳定。
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "grant_input_contract_unsupported"

    # 拒绝之后不得留下任何授权：不能“报了错但已经签发”。
    listing = client.get(f"{base}/credentials/grants")
    assert listing.status_code == 200, listing.text
    assert listing.json() == []


# —— 反向：调试运行不得借用为已发布版本签发的凭证 ——

def _debug_request(path: str = "/require-header") -> dict:
    return {"method": "GET", "path": path, "body_type": "none", "body": ""}


def _debug_assertions() -> list[dict]:
    return [
        {
            "id": "status-ok",
            "target_source": "response.status",
            "selector": [],
            "type": "equals",
            "parameters": {"expected": {"type": "number", "text": "200"}},
            "severity": "error",
        }
    ]


def _snapshot_digest(client: TestClient, base: str, snapshot: dict) -> str:
    response = client.post(f"{base}/debug-snapshot-digest", json=snapshot)
    assert response.status_code == 200, response.text
    return response.json()["hash"]


def _grant_debug_snapshot(
    client: TestClient, base: str, *, profile_id: str, principal_id: str, digest: str
) -> dict:
    response = client.post(
        f"{base}/credentials/grants",
        json={
            "profile_id": profile_id,
            "grant_type": "debug_snapshot",
            "principal_id": principal_id,
            "debug_snapshot_hash": digest,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _prepare_authenticated_environment(
    client: TestClient, account: dict, project: dict, name: str
) -> tuple[str, dict, dict]:
    """建立“需要认证头才放行”的环境与可用身份，返回（base, 环境, 身份）。"""
    base = project_base(account, project)
    environment = create_environment(client, account, project, name=name)
    secret = _create_secret(client, base, f"令牌-{name}", PLAIN_SECRET)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name=f"身份-{name}",
    )
    assert profile["status"] == "available"
    return base, environment, profile


def test_debug_run_cannot_borrow_published_version_grant(
    client: TestClient, account: dict, project: dict
) -> None:
    """只有为固定版本签发的授权时，调试运行必须被拒绝，而不是借用该凭证。

    复现的缺陷是：调试路径把空摘要传给授权匹配，而匹配只比较“等于传入值”且不
    过滤 grant_type，于是 `debug_snapshot_hash IS NULL` 的已发布版本授权被命中。
    受控目标只在收到认证头时才放行，因此“调试运行变成 200”就是凭证被借用的可
    观察证据。
    """
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "版本授权环境"
    )
    case = create_case(
        client,
        account,
        project,
        name="固定版本用例",
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

    run = start_run(
        client,
        account,
        project,
        {
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _debug_request(), "assertions": _debug_assertions()},
        },
    )
    assert _run_once(project["pool_id"]) == "error", "调试运行不得借用固定版本授权"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_not_granted"]
    assert report["response"] is None, "被拒绝的运行不得发出请求"
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)

    # 同一个身份对固定版本本身仍然可用：拒绝的是越界的借用，不是把凭证一并禁用。
    versioned_run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    assert _run_once(project["pool_id"]) == "passed"
    assert get_report(client, account, project, versioned_run["id"])["response"]["status"] == 200


def test_debug_grant_is_bound_to_its_own_snapshot(
    client: TestClient, account: dict, project: dict
) -> None:
    """一次性授权绑定具体快照摘要：换一份调试请求不得复用同一授权。"""
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "快照绑定环境"
    )
    allowed = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, allowed)
    _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )

    other = {
        "request": _debug_request("/require-header?probe=other"),
        "assertions": _debug_assertions(),
    }
    other_run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": other},
    )
    assert _run_once(project["pool_id"]) == "error"
    other_report = get_report(client, account, project, other_run["id"])
    assert [step["error_code"] for step in other_report["steps"]] == ["credential_not_granted"]
    assert other_report["response"] is None

    # 摘要一致的那一份仍可执行，且凭证确实注入到位。
    allowed_run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": allowed},
    )
    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, allowed_run["id"])
    assert report["response"]["status"] == 200
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)

    # 一次性授权用掉即止，重复提交同一快照不再放行。
    replay = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "debug_snapshot": allowed},
    )
    assert _run_once(project["pool_id"]) == "error"
    replay_report = get_report(client, account, project, replay["id"])
    assert [step["error_code"] for step in replay_report["steps"]] == ["credential_grant_used"]
    assert replay_report["response"] is None


def test_concurrent_debug_grant_consumption_succeeds_at_most_once(
    client: TestClient, account: dict, project: dict
) -> None:
    """两个执行者同时消费同一份一次性授权时，只能有一个成功。

    真实现场是两个 worker 并发执行：各自读到 `used_at` 为空，再各自写回，授权就
    被用了两次。这里用两个独立会话精确复现该交错：两边都先读，再依次消费。条件
    更新在第二次会因为 `used_at IS NULL` 不再成立而命中零行。
    """
    from app.db import bind_tenant, get_session_factory
    from app.models import CredentialUseGrant
    from app.services.credentials import CredentialError, _consume_debug_grant

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "并发消费环境"
    )
    digest = _snapshot_digest(
        client, base, {"request": _debug_request(), "assertions": _debug_assertions()}
    )
    grant = _grant_debug_snapshot(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        digest=digest,
    )

    factory = get_session_factory()
    first, second = factory(), factory()
    try:
        bind_tenant(first, account["workspace_id"], account["user_id"])
        bind_tenant(second, account["workspace_id"], account["user_id"])
        grant_a = first.get(CredentialUseGrant, uuid.UUID(grant["id"]))
        grant_b = second.get(CredentialUseGrant, uuid.UUID(grant["id"]))
        assert grant_a.used_at is None and grant_b.used_at is None, "消费前双方都读到未使用"

        _consume_debug_grant(first, grant_a)
        first.commit()

        with pytest.raises(CredentialError) as raised:
            _consume_debug_grant(second, grant_b)
        assert raised.value.code == "credential_grant_used"
        second.rollback()
    finally:
        first.close()
        second.close()

    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT used_at FROM app.credential_use_grants WHERE id = %s", (grant["id"],)
            )
            assert cursor.fetchone()[0] is not None
    finally:
        connection.close()


def test_lost_lease_does_not_consume_one_time_grant(
    client: TestClient, account: dict, project: dict
) -> None:
    """丢掉租约的旧执行者不得把一次性授权烧掉。

    授权消费在最终发送校验之前。只判断“这行授权还没用过”的话，一个已经失去租约、
    工作项被别人接管的旧执行者仍会把它标记为已用，真正持有工作项的新执行者拿到的
    是一份“已使用”的授权。这里用一次真实领取复现该状态：领取后把租约回拨到过去，
    再让同一次领取去解析注入——租约条件写在消费语句里，零行即判失效且不消费。
    随后重新领取并完整执行，证明授权留给了真正持有工作项的那一次执行。
    """
    from app.db import bind_tenant, get_session_factory
    from app.kernel.target_policy import normalize_origin
    from app.models import Environment
    from app.services.credentials import CredentialError, LeaseGuard, resolve_injection

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "租约消费环境"
    )
    snapshot = {"request": _debug_request(), "assertions": _debug_assertions()}
    digest = _snapshot_digest(client, base, snapshot)
    grant = _grant_debug_snapshot(
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

    stale_claim = _claim_job(project["pool_id"])
    assert stale_claim is not None, "刚入队的工作项应当可以领取"
    _expire_lease(run["id"])

    session = get_session_factory()()
    try:
        bind_tenant(session, account["workspace_id"], account["user_id"])
        environment_row = session.get(Environment, uuid.UUID(environment["id"]))
        with pytest.raises(CredentialError) as raised:
            resolve_injection(
                session,
                _settings(),
                environment=environment_row,
                principal_id=uuid.UUID(str(account["user_id"])),
                case_version_id=None,
                debug_snapshot_hash=digest,
                target_origin=normalize_origin(CONTROLLED_TARGET_BASE_URL),
                lease_guard=LeaseGuard(
                    job_id=stale_claim.job_id,
                    worker_id="it-cred-worker",
                    fencing_token=stale_claim.fencing_token,
                ),
            )
        assert raised.value.code == "credential_lease_lost"
        session.rollback()
    finally:
        session.close()

    assert _grant_used_at(grant["id"]) is None, "失去租约的领取不得消费一次性授权"

    # 重新领取（token 递增）后完整执行：授权仍在，凭证照常注入。
    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    assert _grant_used_at(grant["id"]) is not None, "真正持有工作项的执行才消费授权"


def test_revocation_between_intent_and_send_blocks_the_request(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """授权在“发送意图已存、请求尚未发出”之间被撤销：必须拦住这次发送。

    发送意图与真正发送之间还隔着凭证解析、请求准备与入参断言。只在流程开头校验
    一次，等于把“校验那一刻的授权”当成“发送那一刻的授权”，撤销要等下一次运行才
    生效——而这一次的请求已经带着凭证发出去了。准入点必须在意图之后按当前状态
    重读一遍，撤销立即生效。
    """
    from app.services import executor

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "撤销准入环境"
    )
    case = create_case(
        client,
        account,
        project,
        name="撤销准入用例",
        request=_debug_request(),
        assertions=_debug_assertions(),
    )
    version = publish_case(client, account, project, case["id"])
    grant = _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    real_persist = executor._persist_assertion_results
    revoked: list[int] = []

    def revoke_then_persist(session, job_claim, attempt_id, records, secrets=None):
        # 故障点：意图写入之后、准入点重读之前，管理员撤销授权。
        response = client.delete(f"{base}/credentials/grants/{grant['id']}")
        revoked.append(response.status_code)
        assert response.status_code == 204, response.text
        return real_persist(session, job_claim, attempt_id, records, secrets)

    monkeypatch.setattr(executor, "_persist_assertion_results", revoke_then_persist)

    assert _run_once(project["pool_id"]) == "error"
    assert revoked == [204], "撤销本身应当成功"
    assert sent == [], "授权已撤销，不得发出任何请求"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_not_granted"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    # 发送意图确实已落库，说明拦截来自准入点重读，而不是流程更早的某次校验。
    assert _step_send_intent_at(run["id"]) is not None


def test_variable_change_between_intent_and_send_blocks_the_request(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """变量输入在“发送意图已存、请求尚未发出”之间被改掉：必须拦住这次发送。

    与撤销授权是同一准入点的两个面：一个看授权还在不在，一个看要发的请求还是不是
    被批准的那一份。变量在最后一步被改掉，请求模板会解析成另一条路径，授权却仍然
    有效，只靠“授权存在”拦不住。
    """
    from app.services import executor

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "改变量准入环境"
    )
    _set_environment_variables(
        client, account, project, environment["id"],
        {"operation": {"type": "string", "text": "require-header"}},
    )
    case = create_case(
        client,
        account,
        project,
        name="改变量准入用例",
        request={"method": "GET", "path": "/{{operation}}", "body_type": "none", "body": ""},
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
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    real_persist = executor._persist_assertion_results

    def rewire_then_persist(session, job_claim, attempt_id, records, secrets=None):
        # 故障点：意图写入之后，把请求要解析的变量改成另一条路径。
        _set_environment_variables(
            client, account, project, environment["id"],
            {"operation": {"type": "string", "text": "echo"}},
        )
        return real_persist(session, job_claim, attempt_id, records, secrets)

    monkeypatch.setattr(executor, "_persist_assertion_results", rewire_then_persist)

    assert _run_once(project["pool_id"]) == "error"
    assert sent == [], "变量已改，不得再发出按旧输入准备的请求"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "authentication"
    assert [step["error_code"] for step in report["steps"]] == ["credential_inputs_changed"]
    assert report["response"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    assert _step_send_intent_at(run["id"]) is not None


def test_assertion_on_echoed_secret_in_response_is_rejected(
    client: TestClient, account: dict, project: dict
) -> None:
    """目标把认证头回显到正文时，回显字段同样受保护，断言不得成为猜测入口。

    受控目标在 `/require-header` 把收到的令牌原样放进 `token_echo`。只标注“注入到
    request.header”会漏掉这个可读副本：对 `response.body.token_echo` 配一条“以
    demo 开头”，无论返回通过还是失败，都在回答“秘密是不是这样开头”。因此必须按
    实际内容反查坐标，在求值前就按策略拒绝；报告与数据库都不得留下明文。
    """
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "回显保护环境"
    )
    case = create_case(
        client,
        account,
        project,
        name="对回显字段断言",
        request=_debug_request(),
        assertions=[
            {
                "id": "probe-echo",
                "target_source": "response.body",
                "selector": [{"kind": "key", "key": "token_echo"}],
                "type": "starts_with",
                "parameters": {"expected": {"type": "string", "text": "demo"}},
                "severity": "error",
            },
            {
                "id": "status-ok",
                "target_source": "response.status",
                "selector": [],
                "type": "equals",
                "parameters": {"expected": {"type": "number", "text": "200"}},
                "severity": "error",
            },
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    # 请求确实发出并被目标放行：拒绝的是对回显值的求值，不是发送本身。
    assert report["response"]["status"] == 200

    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe-echo")
    assert probe["status"] == "error"
    assert probe["reason_code"] == "policy_rejected"
    assert probe["actual"] is None and probe["expected"] is None

    # 全文与落库两处都不得出现明文：正文证据、断言结果、步骤证据一起检查。
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    connection = migrator_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT expected, actual, status, reason_code FROM app.assertion_results"
                " WHERE run_id = %s ORDER BY assertion_id",
                (run["id"],),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT request, response FROM app.run_step_attempts WHERE run_id = %s",
                (run["id"],),
            )
            evidence = cursor.fetchall()
    finally:
        connection.close()
    assert rows, "断言结果应已落库"
    assert PLAIN_SECRET not in json.dumps([list(row) for row in rows], ensure_ascii=False, default=str)
    assert PLAIN_SECRET not in json.dumps(
        [list(row) for row in evidence], ensure_ascii=False, default=str
    )


def test_assertion_on_whole_response_text_containing_secret_is_rejected(
    client: TestClient, account: dict, project: dict
) -> None:
    """整段正文包含秘密时，对 `response.text` 的普通断言同样必须被拒绝。

    这是“祖先容器”那一类：用户没有精确点到 `token_echo`，而是对整段正文配条件。
    实际值里就含有秘密，返回通过与否都会泄露信息，不能因为坐标不同就绕过。
    """
    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "整段正文环境"
    )
    case = create_case(
        client,
        account,
        project,
        name="对整段正文断言",
        request=_debug_request(),
        assertions=[
            {
                "id": "probe-whole-body",
                "target_source": "response.text",
                "selector": [],
                "type": "contains",
                "parameters": {"expected": {"type": "string", "text": "demo"}},
                "severity": "error",
            }
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200
    probe = next(
        item for item in report["assertions"] if item["assertion_id"] == "probe-whole-body"
    )
    assert probe["status"] == "error"
    assert probe["reason_code"] == "policy_rejected"
    assert probe["actual"] is None
    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)


# —— 反向：非管理员不能配置身份凭证 ——


def test_editor_cannot_manage_credentials(account: dict) -> None:
    """编辑者即使能编辑用例，也不得创建秘密或身份（权限边界按角色）。"""
    from app.main import app

    connection = migrator_connection()
    username = f"it-cred-editor-{uuid.uuid4().hex[:8]}"
    editor_id, workspace_id = seed_account(
        connection, username=username, password=PASSWORD, workspace_role="editor"
    )
    # 编辑者需要工作空间下的项目才能命中端点；项目由管理员在同工作空间创建。
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.projects (id, workspace_id, key, name) VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), workspace_id, f"cred{uuid.uuid4().hex[:6]}", "编辑者可见项目"),
        )
        cursor.execute(
            "SELECT id FROM app.projects WHERE workspace_id = %s", (workspace_id,)
        )
        project_id = cursor.fetchone()[0]
    connection.commit()

    try:
        with TestClient(app) as editor:
            login = editor.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            editor.headers["X-CSRF-Token"] = editor.cookies["interface_csrf"]
            base = f"/api/v1/workspaces/{workspace_id}/projects/{project_id}"
            denied = editor.post(
                f"{base}/credentials/secrets", json={"name": "越权秘密", "value": PLAIN_SECRET}
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["code"] == "insufficient_role"
            assert PLAIN_SECRET not in denied.text
    finally:
        connection.execute("DELETE FROM app.projects WHERE workspace_id = %s", (workspace_id,))
        connection.execute("DELETE FROM app.workspace_memberships WHERE workspace_id = %s", (workspace_id,))
        connection.execute("DELETE FROM app.workspaces WHERE id = %s", (workspace_id,))
        connection.commit()
        connection.execute("DELETE FROM app.sessions WHERE user_id = %s", (editor_id,))
        connection.execute("DELETE FROM app.users WHERE id = %s", (editor_id,))
        connection.commit()
        connection.close()


# —— 反向：失败出口不得把认证值写进报告、数据库或日志 ——


def test_untransmittable_secret_never_reaches_report_database_or_logs(
    client: TestClient,
    account: dict,
    project: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """注入值无法按 HTTP 头传输时的失败出口：报告、数据库、日志都不得有明文。

    秘密值在创建时不限字符集，中文完全合法；而认证槽位注入的是请求头，非 ASCII
    值要到运行时才撞上传输边界。这条失败路径发生在成功路径的脱敏之外：异常文本
    原样落库就等于把秘密写进了报告与数据库。错误码与可操作说明仍要清楚。
    """
    import logging

    from app.services import executor

    secret_value = "壹贰叁-待注入令牌-0f3a"
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="不可传输值环境")
    secret = _create_secret(client, base, "不可传输令牌", secret_value)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="不可传输身份",
    )
    assert profile["status"] == "available"
    case = create_case(
        client,
        account,
        project,
        name="不可传输用例",
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
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)

    with caplog.at_level(logging.DEBUG):
        assert _run_once(project["pool_id"]) == "error"
    assert sent == [], "传输校验失败必须在发送之前拦住"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "configuration"
    assert [step["error_code"] for step in report["steps"]] == ["request_invalid"]
    assert report["response"] is None
    # 仍然可操作：说清是哪个位置、为什么发不出去，但不回显值本身。
    message = report["request"]["message"]
    assert "X-Demo-Token" in message
    assert "无法按 HTTP 头传输" in message
    assert "不回显" in message

    for text in (json.dumps(report, ensure_ascii=False), _run_rows_as_text(run["id"])):
        assert secret_value not in text, "报告与数据库都不得出现秘密明文"
        assert secret_value[:4] not in text, "开头片段同样不得出现"
    logged = _captured_log_text(caplog)
    assert secret_value not in logged, "日志不得出现秘密明文"
    assert secret_value[:4] not in logged


# —— 反向：入库正文必须先脱敏，再截断 ——


def test_json_escaped_echo_of_the_secret_cannot_be_recovered_from_the_report(
    client: TestClient, account: dict, project: dict
) -> None:
    """目标把含引号／反斜线的秘密按 JSON 转义回显：报告里解不出明文。

    秘密原文在这段正文里根本不存在（`"` 被写成 `\\"`），按原文串替换的脱敏匹配
    不到任何东西，留下的就是一份可以解码还原的副本。
    """
    from app.kernel.redaction import MASK

    # 纯 ASCII：能按 HTTP 头传输，但在 JSON 里必须转义。
    secret_value = 'ab"cd\\ef-0f3a'
    base = project_base(account, project)
    environment = create_environment(client, account, project, name="转义回显环境")
    secret = _create_secret(client, base, "转义回显令牌", secret_value)
    profile = _configure_credential(
        client,
        base,
        environment_id=environment["id"],
        secret_version_id=secret["latest_version_id"],
        name="转义回显身份",
    )
    case = create_case(
        client,
        account,
        project,
        name="转义回显用例",
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
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    assert _run_once(project["pool_id"]) == "passed"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200
    assert report["response"]["body_truncated"] is False
    # 目标回显的是凭据本身；报告里同一个字段必须是掩码，且掩码不遮挡其他内容。
    assert json.loads(report["response"]["body"]) == {"token_echo": MASK}

    fragments = {secret_value[i : i + 4] for i in range(len(secret_value) - 3)}
    for text in (json.dumps(report, ensure_ascii=False), _run_rows_as_text(run["id"])):
        leaked = sorted(fragment for fragment in fragments if fragment in text)
        assert leaked == [], f"报告或数据库中残留秘密片段：{leaked}"


def test_secret_straddling_the_stored_body_boundary_leaves_no_fragment(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """秘密横跨入库正文的截断边界时，报告里不得留下它的任何片段。

    “先截断、再替换”在这里必然漏：截断按字节切，秘密的后半段被切掉，替换再也匹配
    不到完整秘密，前半段就原样留在了报告里。这里把入库上限调小，让同一条真实链路
    （真实目标、真实响应、真实入库）落在确定的小边界上跑一遍，不依赖 64 KiB 的巨型
    请求——边界取多大与“先脱敏、再截断”这一顺序无关。
    """
    from app.kernel.redaction import MASK
    from app.services import executor

    boundary = 4096
    monkeypatch.setattr(executor, "_MAX_STORED_BODY_BYTES", boundary)
    base = project_base(account, project)

    # 先用同一套模板、把填充留空量一次：秘密在响应正文里的偏移只与填充长度有关，
    # 量出来即可算出任意填充下它落在哪里，不需要猜测。
    outcome, measured = _run_query_auth_case(
        client, account, project, base, tag="截断基准", query_params=[{"name": "pad", "value": ""}]
    )
    assert outcome == "passed", outcome
    assert measured["response"]["body_truncated"] is False, "基准运行的正文不应被截断"
    marker = '{"name":"api_key","value":"'
    secret_start = measured["response"]["body"].index(marker) + len(marker)

    size = len(PLAIN_SECRET.encode())
    for shift in (-1, 0, 1):
        offset = boundary - size // 2 + shift
        pad = "a" * (offset - secret_start)
        assert pad, "填充长度必须为正：基准偏移应远小于截断边界"

        outcome, report = _run_query_auth_case(
            client,
            account,
            project,
            base,
            tag=f"截断-{shift}",
            query_params=[{"name": "pad", "value": pad}],
        )
        assert outcome == "passed", outcome
        evidence = report["response"]
        assert evidence["body_truncated"] is True, f"shift={shift} 的正文必须真的被截断"
        stored = evidence["body"]
        fragments = {PLAIN_SECRET[i : i + 4] for i in range(size - 3)}
        leaked = sorted(fragment for fragment in fragments if fragment in stored)
        assert leaked == [], f"shift={shift} 的报告正文残留秘密片段：{leaked}"
        assert MASK in stored, f"shift={shift} 的掩码应落在被保留的那一段里"
        assert PLAIN_SECRET not in _run_rows_as_text(report["run"]["id"])


# —— 反向：以凭据为对象键的响应同样受保护 ——


def _run_keyed_case(
    client: TestClient, account: dict, project: dict, name: str, assertions: list[dict]
) -> tuple[str, dict, dict]:
    """建一条命中“凭据被当作对象键回显”的链路并执行一次。"""
    base, environment, profile = _prepare_authenticated_environment(client, account, project, name)
    case = create_case(
        client,
        account,
        project,
        name=f"{name}用例",
        request=_debug_request("/secure/keyed"),
        assertions=assertions,
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    outcome = _run_once(project["pool_id"])
    return outcome, run, get_report(client, account, project, run["id"])


def test_credential_echoed_as_object_key_is_redacted_before_storage(
    client: TestClient, account: dict, project: dict
) -> None:
    """凭据出现在对象**键**上时同样要遮蔽：报告与数据库都不得留明文键名。

    只按值判断敏感的实现在这里什么都看不出来：这个对象没有任何字段的值含秘密，
    于是掩码不生效、明文键名直接落库，报告里的这一份就是可读副本。
    """
    from app.kernel.redaction import MASK

    outcome, run, report = _run_keyed_case(
        client, account, project, "键名回显环境", _debug_assertions()
    )
    assert outcome == "passed", outcome
    assert report["response"]["status"] == 200
    assert json.loads(report["response"]["body"]) == {MASK: "ok"}

    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    assert PLAIN_SECRET not in _run_rows_as_text(run["id"])


def test_assertion_on_a_secret_keyed_object_is_refused(
    client: TestClient, account: dict, project: dict
) -> None:
    """对以凭据为键的整对象配条件必须被拒绝：通过与否本身就是猜秘密的通道。

    同一份用例里与秘密无关的节点（状态码）照常求值并通过——保护要精确到坐标，
    不能因为响应里有个秘密键就把整份报告变成不可用。
    """
    outcome, run, report = _run_keyed_case(
        client,
        account,
        project,
        "键名探测环境",
        [
            {
                "id": "probe-keyed-object",
                "target_source": "response.body",
                "selector": [],
                "type": "not_empty",
                "parameters": {},
                "severity": "error",
            },
            *_debug_assertions(),
        ],
    )
    assert outcome == "error", outcome
    # 请求确实发出并被目标放行：被拒绝的是对这个对象的求值，不是发送本身。
    assert report["response"]["status"] == 200

    probe = next(
        item for item in report["assertions"] if item["assertion_id"] == "probe-keyed-object"
    )
    assert probe["status"] == "error"
    assert probe["reason_code"] == "policy_rejected"
    assert probe["actual"] is None and probe["expected"] is None
    status = next(item for item in report["assertions"] if item["assertion_id"] == "status-ok")
    assert status["status"] == "passed"

    assert PLAIN_SECRET not in json.dumps(report, ensure_ascii=False)
    assert PLAIN_SECRET not in _run_rows_as_text(run["id"])


# —— 反向：必须编码才能上线的凭据（含 + / = 与空格）在每个出口都不可还原 ——
#
# 查询参数认证的凭据到运行时就在 URL 里，而 URL 必须编码才能上线：`a+b/c= d e` 发
# 出去是 `a%2Bb%2Fc%3D+d+e`。对**已编码**的那份 URL 做原文串替换一个字符都匹配不
# 到，报告里留下的是一份 `unquote` 就能还原的副本。修复不是再补一条“猜秘密会怎么
# 编码”的替换分支，而是从真实查询项重建：先按槽位遮蔽，再用发送用的同一个编码入口
# 编码，普通参数与顺序因此与线上逐字一致。

# 四个字符各自撞上不同的编码规则：`+` 与空格在查询串里编码不同，`/` 与 `=` 是分隔符。
ENCODED_SECRET = "a+b/c= d e"


def test_credential_with_encoding_characters_arrives_intact_and_stays_hidden(
    client: TestClient,
    account: dict,
    project: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """必须编码的凭据：到目标手里仍逐字符正确，且报告／数据库／日志里都不可还原。

    “到目标手里仍正确”既不能靠断言比秘密（那正是被策略拒绝的事），也不能靠读报告里
    的值（那正是被遮蔽的事）。可判别的证据是**目标解码后的回声是不是整段掩码**：掩
    码的前提是目标收到的值里含有这份凭据；只要线上编码有一处走样（例如多编一层），
    目标解出来的就不是凭据，掩码不成立，报告里会直接留下走样的那串值。

    日志是第三条出口：传输库在 INFO 级别逐条记录请求行，凭据就在那条 URL 里，而它不
    经过引擎的脱敏。这条同样按“解码也还原不出来”检查。
    """
    import logging
    from urllib.parse import parse_qsl, urlsplit

    from app.kernel.redaction import MASK

    configured = [
        {"name": "tag", "value": "a b&c=d"},
        {"name": "tag", "value": "第二个"},
    ]
    with caplog.at_level(logging.DEBUG):
        outcome, report = _run_query_auth_case(
            client,
            account,
            project,
            project_base(account, project),
            tag="encoded",
            query_params=configured,
            secret_value=ENCODED_SECRET,
        )
    assert outcome == "passed", outcome
    assert report["response"]["status"] == 200

    echoed = json.loads(report["response"]["body"])["query"]
    keyed = [item for item in echoed if item["name"] == "api_key"]
    assert [item["name"] for item in echoed] == ["tag", "tag", "api_key"], echoed
    assert len(keyed) == 1
    assert keyed[0]["value"] == MASK, (
        "目标解码后的整段值应当就是这份凭据（因此整段被遮蔽）；"
        f"出现别的取值说明编码已经走样：{keyed[0]['value']!r}"
    )

    url = report["request"]["url"]
    assert "api_key=***" in url, url
    assert ENCODED_SECRET not in url
    for decoded in _decoded_forms(url):
        assert ENCODED_SECRET not in decoded, "URL 解码不得还原出凭据"
    assert parse_qsl(urlsplit(url).query, keep_blank_values=True) == [
        ("tag", "a b&c=d"),
        ("tag", "第二个"),
        ("api_key", MASK),
    ], "普通参数（含重复项与顺序）不得因重建而走样"

    for name, text in (
        ("报告", json.dumps(report, ensure_ascii=False)),
        ("数据库", _run_rows_as_text(report["run"]["id"])),
        ("日志", _captured_log_text(caplog)),
    ):
        assert ENCODED_SECRET not in text, f"{name}不得出现凭据"
        for decoded in _decoded_forms(text):
            assert ENCODED_SECRET not in decoded, f"{name}不得留下可解码还原的凭据"


def test_encoded_credential_cannot_be_recovered_from_a_failed_send(
    client: TestClient,
    account: dict,
    project: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """发送失败的出口、以及运行时日志，都不得留下可解码还原的凭据。

    失败路径最容易漏的是把**线上那份 URL** 写进错误证据——原文串替换挡不住编码形态。
    这里让一次真实发送读超时（受控目标的 `/delay/1500` 对 0.2 秒读超时，请求确实已经
    发出），把报告、数据库与日志三个出口一起检查：三者都不允许在百分号解码或表单解码
    （`+` 还原成空格）之后出现凭据——只试 `unquote` 会漏掉空格编成 `+` 的那一段。
    """
    import logging
    from dataclasses import replace

    from app.config import get_settings

    settings = replace(get_settings(), worker_id="it-cred-worker", http_read_timeout=0.2)
    with caplog.at_level(logging.DEBUG):
        outcome, report = _run_query_auth_case(
            client,
            account,
            project,
            project_base(account, project),
            tag="sendfail",
            path="/delay/1500",
            secret_value=ENCODED_SECRET,
            settings=settings,
        )
    assert outcome == "error", outcome
    assert report["run"]["reason_category"] == "network"
    assert [step["error_code"] for step in report["steps"]] == ["read_timeout"]

    # 请求证据仍然完整可用，只是凭据那一项按槽位遮蔽；解码也还原不回来。
    url = report["request"]["url"]
    assert "api_key=***" in url, url
    for decoded in _decoded_forms(url):
        assert ENCODED_SECRET not in decoded, "URL 解码不得还原出凭据"

    exits = {
        "报告": json.dumps(report, ensure_ascii=False),
        "数据库": _run_rows_as_text(report["run"]["id"]),
        "日志": _captured_log_text(caplog),
    }
    for name, text in exits.items():
        assert ENCODED_SECRET not in text, f"{name}不得出现凭据"
        for decoded in _decoded_forms(text):
            assert ENCODED_SECRET not in decoded, f"{name}不得留下可解码还原的凭据"


# —— 反向：秘密的转义写法（判定的原文预筛在这里是瞎的） ——


def test_escaped_credential_echo_cannot_be_recovered_from_the_report(
    client: TestClient, account: dict, project: dict
) -> None:
    """秘密被回显成 `\\uXXXX` 写法时：判定要看解码后的内容，遮蔽要与判定同源。

    此前“是否要脱敏”的预筛只比两种形态：秘密原文，以及 `json.dumps` 的转义写法。秘密
    为纯 ASCII 时两者都等于原文，于是 `{"token":"\\uXXXX…"}` 这样的合法 JSON 一进来，
    预筛直接放行——最终报告里的这段文本 `json.loads` 一解就是凭据原文。

    同时它也是断言侧的口子：同一个值若不被标为受保护坐标，对它的断言就会既求出布尔
    结果、又把明文写进 `actual`。这里两条一起验：目标回声整段被遮蔽，且对这段转义文本
    配条件必须被策略拒绝。
    """
    from app.kernel.redaction import MASK

    base, environment, profile = _prepare_authenticated_environment(
        client, account, project, "转义回显环境"
    )
    # `d` 就是 `d`：解出来正好是 PLAIN_SECRET，而原文一个字符都不在这段正文里。
    escaped_body = '{"token":"\\u0064emo-token-7f3a91c0-it-fixture"}'
    assert PLAIN_SECRET not in escaped_body, "前提：原文串确实不在请求正文里"

    case = create_case(
        client,
        account,
        project,
        name="转义回显用例",
        request={
            "method": "POST",
            "path": "/echo",
            "query_params": [],
            "body_type": "text",
            "body": escaped_body,
        },
        assertions=[
            {
                "id": "probe-escaped-echo",
                "target_source": "response.body",
                "selector": [{"kind": "key", "key": "body_text"}],
                "type": "contains",
                "parameters": {"expected": {"type": "string", "text": PLAIN_SECRET}},
                "severity": "error",
            },
            *_debug_assertions(),
        ],
    )
    version = publish_case(client, account, project, case["id"])
    _grant_case_version(
        client,
        base,
        profile_id=profile["id"],
        principal_id=str(account["user_id"]),
        case_version_id=version["id"],
    )
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    assert _run_once(project["pool_id"]) == "error"
    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200

    probe = next(
        item for item in report["assertions"] if item["assertion_id"] == "probe-escaped-echo"
    )
    assert probe["status"] == "error"
    assert probe["reason_code"] == "policy_rejected", probe
    assert probe["actual"] is None and probe["expected"] is None
    status = next(item for item in report["assertions"] if item["assertion_id"] == "status-ok")
    assert status["status"] == "passed", "安全兄弟字段仍要可用"

    stored = json.loads(report["response"]["body"])
    # 目标把请求正文原样回显进 `body_text`：报告里这一份必须连“转义写法”都不留，
    # 解码一次也解不出秘密；请求证据里的正文同样如此。
    assert "u0064emo-token" not in report["response"]["body"], report["response"]["body"]
    assert json.loads(stored["body_text"]) == {"token": MASK}
    assert "u0064emo-token" not in report["request"]["body"], report["request"]["body"]

    for text in (json.dumps(report, ensure_ascii=False), _derived_rows_as_text(run["id"])):
        assert PLAIN_SECRET not in text
        for decoded in _decoded_forms(text):
            assert PLAIN_SECRET not in decoded, "解码一次也不得还原出凭据"


# —— 反向：断言结果的动态定位信息同样受保护 ——


def test_sub_path_assertion_on_the_secret_key_does_not_leak_the_key_name(
    client: TestClient, account: dict, project: dict
) -> None:
    """按子路径点到秘密键的断言：除了拒绝，定位信息本身也不得把键名写进报告。

    整对象被拒绝时 `selector` 是空的，看不出定位信息这条出口；按子路径定位时
    `selector` 与渲染出的可读路径里都带着那个键名——`expected`／`actual` 已置空，定位
    信息却原样落库，遮一个出口留一个等于没遮。固定的 API 信封字段（断言标识、类型、
    阶段、状态、原因码）必须保持原样语义，无关的兄弟断言照常通过。
    """
    from app.kernel.redaction import MASK

    outcome, run, report = _run_keyed_case(
        client,
        account,
        project,
        "键名子路径环境",
        [
            {
                "id": "probe-sub-key",
                "target_source": "response.body",
                "selector": [{"kind": "key", "key": PLAIN_SECRET}],
                "type": "equals",
                "parameters": {"expected": {"type": "string", "text": "ok"}},
                "severity": "error",
            },
            *_debug_assertions(),
        ],
    )
    assert outcome == "error", outcome
    assert report["response"]["status"] == 200

    probe = next(item for item in report["assertions"] if item["assertion_id"] == "probe-sub-key")
    # 状态枚举与固定信封字段保持原样语义。
    assert probe["status"] == "error"
    assert probe["reason_code"] == "policy_rejected"
    assert probe["type"] == "equals"
    assert probe["phase"] == "post_response"
    assert probe["expected"] is None and probe["actual"] is None
    # 定位信息保留结构（键名换成掩码），报告与数据库都不得出现明文键名。
    assert probe["target"]["source"] == "response.body"
    assert probe["target"]["selector"] == [{"kind": "key", "key": MASK}], probe["target"]
    assert probe["target"]["path"] == MASK, probe["target"]
    status = next(item for item in report["assertions"] if item["assertion_id"] == "status-ok")
    assert status["status"] == "passed", "安全兄弟字段仍要可用"

    for text in (json.dumps(report, ensure_ascii=False), _derived_rows_as_text(run["id"])):
        assert PLAIN_SECRET not in text
