"""目标准入、连接地址固定与实际发送必须是同一个目标。

发送前的准入要按**实际将要发送的 URL**复核。若改成“按当前环境重新拼一个目标再看
白名单”，复核的是另一个地址：环境在“请求已经准备好、还没发出”的窗口里被改到 B，
检查会按 B 通过，而请求仍然发往 A——撤销 A 就成了一次假动作。更糟的是，按 B 解析
出的固定连接地址套不到 A 的主机上，缺地址时若静默退回普通 DNS，地址校验被整段绕过。

后三条用例盯住另一条边界：白名单内的域名解析到环回或平台基础设施地址时，请求必须
在发送前被拦下，且**一个请求都不发**。解析结果由注入的假解析器提供（只替换被测的那
一台主机），不查真实 DNS，也不访问任何真实生产、元数据或管理基础设施地址。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import uuid
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from harness import (
    CONTROLLED_TARGET_ALT_BASE_URL,
    CONTROLLED_TARGET_BASE_URL,
    create_case,
    create_environment,
    get_report,
    project_base,
    publish_case,
    start_run,
)

pytestmark = pytest.mark.integration


def _run_once(pool_id: str, worker_id: str = "it-pin-worker") -> str:
    """与 `python -m app.worker` 完全同一份领取与执行内核。"""
    from app.config import get_settings
    from app.db import get_session_factory
    from app.services.executor import claim_job, execute_claim

    session = get_session_factory()()
    try:
        claim = claim_job(session, worker_id, [uuid.UUID(pool_id)], 30)
        session.commit()
    finally:
        session.close()
    assert claim is not None, "执行池中应有可领取的工作项"
    settings = replace(get_settings(), worker_id=worker_id)
    return execute_claim(get_session_factory(), settings, claim)


def _pools_base(account: dict, project: dict) -> str:
    return f"{project_base(account, project)}/pools"


def _echo_request() -> dict:
    return {"method": "GET", "path": "/echo", "body_type": "none", "body": ""}


def _status_assertions() -> list[dict]:
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


def _prepare_run(
    client: TestClient, account: dict, project: dict, name: str
) -> tuple[dict, dict]:
    """建一条指向受控目标 A 的完整可执行链路，返回（环境, 运行）。

    不配置身份凭证：本模块验证的是目标与连接地址，凭据注入不参与，避免把两件事
    混在一条用例里。
    """
    environment = create_environment(client, account, project, name=name)
    case = create_case(
        client,
        account,
        project,
        name=f"{name}用例",
        request=_echo_request(),
        assertions=_status_assertions(),
    )
    version = publish_case(client, account, project, case["id"])
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )
    return environment, run


def _counting_send(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记录实际发出的 URL；返回空列表即证明一个请求都没发出。"""
    from app.services import executor

    sent: list[str] = []
    real_send = executor._send

    def counting_send(settings, pin, prepared):
        sent.append(prepared.url)
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", counting_send)
    return sent


def test_target_change_between_prepare_and_send_blocks_the_request(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """准备成 A、发送前环境改到 B 并把 A 移出白名单：必须拒绝，一个请求都不发。

    这是“准入按重读环境重算目标”的失效路径：重算得到 B，B 在修订后的白名单里，
    检查于是通过；而真正要发的是先前准备好的 A。撤销 A 之后请求照发，白名单形同
    虚设，连接地址也仍然指向 A。
    """
    from app.services import executor

    environment, run = _prepare_run(client, account, project, "准入改目标环境")
    sent = _counting_send(monkeypatch)

    real_persist = executor._persist_assertion_results
    changed: list[int] = []

    def change_environment_then_persist(session, job_claim, attempt_id, records, secrets=None):
        # 故障点：发送意图已写入、准入点还没重读，此时管理员把环境改到另一个受控
        # 目标，并把原目标从执行池白名单里删掉。
        patched = client.patch(
            f"{project_base(account, project)}/environments/{environment['id']}",
            json={"base_url": CONTROLLED_TARGET_ALT_BASE_URL},
            headers={"If-Match": str(environment["rev"])},
        )
        narrowed = client.put(
            f"{_pools_base(account, project)}/{project['pool_id']}/targets",
            json={"allowed_targets": [CONTROLLED_TARGET_ALT_BASE_URL]},
        )
        changed.extend([patched.status_code, narrowed.status_code])
        return real_persist(session, job_claim, attempt_id, records, secrets)

    monkeypatch.setattr(executor, "_persist_assertion_results", change_environment_then_persist)

    assert _run_once(project["pool_id"]) == "error"
    assert changed == [200, 200], "改环境与收窄白名单本身都应当成功"
    assert sent == [], "目标已被移出白名单，不得发出任何请求"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "policy"
    assert [step["error_code"] for step in report["steps"]] == ["target_not_allowed"]
    assert report["response"] is None, "被拒绝的请求不得产生目标证据"


def test_request_goes_to_the_prepared_host_with_its_pinned_address(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常路径：发往准备时那个目标，连接地址固定为已校验的 IP。"""
    from app.services import executor

    _environment, run = _prepare_run(client, account, project, "地址固定环境")
    seen: list[dict] = []
    real_send = executor._send

    def recording_send(settings, pin, prepared):
        seen.append({"url": prepared.url, "pin": dict(pin)})
        return real_send(settings, pin, prepared)

    monkeypatch.setattr(executor, "_send", recording_send)

    assert _run_once(project["pool_id"]) == "passed"
    assert len(seen) == 1, "一条用例只发一次请求"

    parts = urlsplit(seen[0]["url"])
    assert f"{parts.scheme}://{parts.netloc}" == CONTROLLED_TARGET_BASE_URL
    # 固定地址按“实际发送的主机名”取用，取值是一个已校验的 IP，而不是把主机名
    # 交给运行时再解析一次。
    assert set(seen[0]["pin"]) == {parts.hostname}
    assert ipaddress.ip_address(seen[0]["pin"][parts.hostname])

    report = get_report(client, account, project, run["id"])
    assert report["response"]["status"] == 200
    # 响应来自受控目标本体，说明请求确实发到了线上目标，而不是被就地伪造。
    assert json.loads(report["response"]["body"])["service"] == "controlled-target"


def test_missing_pinned_address_blocks_the_request(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标主机解析不出地址时必须直接拒绝，不得退回普通 DNS 连接。

    只让被测目标这一台主机的解析失败；其余主机（数据库等）仍走真实解析，本用例
    不会因此进入未知状态。
    """
    _environment, run = _prepare_run(client, account, project, "无固定地址环境")
    sent = _counting_send(monkeypatch)

    host = urlsplit(CONTROLLED_TARGET_BASE_URL).hostname
    real_getaddrinfo = socket.getaddrinfo

    def failing_getaddrinfo(name, *args, **kwargs):
        if name == host:
            raise socket.gaierror(-2, "Name or service not known")
        return real_getaddrinfo(name, *args, **kwargs)

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", failing_getaddrinfo)

    assert _run_once(project["pool_id"]) == "error"
    assert sent == [], "没有可用的固定连接地址时不得尝试发送"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "policy"
    assert [step["error_code"] for step in report["steps"]] == ["target_not_allowed"]
    assert report["response"] is None


# —— BQ7：解析到环回／平台基础设施地址时不得发出请求 ——


def _redirect_host(host: str, addresses: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """只替换一台主机的解析结果，其余主机仍走真实解析。

    只替换这一台：守卫在地址层还会解析平台基础设施的名字来封掉 IP 直连，把全体主机
    的解析都换掉会让那些名字也指到被测地址上，拒绝的原因就不再是本次要验证的规则。
    """
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(name, *args, **kwargs):
        if name == host:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses
            ]
        return real_getaddrinfo(name, *args, **kwargs)

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", fake_getaddrinfo)


def _rejected_without_send(
    client: TestClient,
    account: dict,
    project: dict,
    run: dict,
    sent: list[str],
    loopback: str = "",
) -> None:
    """断言“策略拒绝 + 一个请求都没发出 + 没有响应证据”。"""
    assert _run_once(project["pool_id"]) == "error"
    assert sent == [], "解析到禁区时不得发出任何请求"
    report = get_report(client, account, project, run["id"])
    assert report["run"]["reason_category"] == "policy"
    assert [step["error_code"] for step in report["steps"]] == ["target_not_allowed"]
    assert report["response"] is None, "被拒绝的请求不得产生目标证据"


@pytest.mark.parametrize("loopback", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_allowed_domain_resolving_to_loopback_blocks_the_send(
    client: TestClient,
    account: dict,
    project: dict,
    monkeypatch: pytest.MonkeyPatch,
    loopback: str,
) -> None:
    """白名单内的域名在发送前解析到环回（三种写法）：拒绝，且一个请求都不发。

    域名在白名单里只说明来源被允许；解析结果指向执行器自己的回环接口时必须拒绝。
    三种写法是同一台主机，结论必须一致，否则改一处写法就能把用例打到平台自身。
    """
    _environment, run = _prepare_run(client, account, project, f"环回解析环境-{loopback}")
    sent = _counting_send(monkeypatch)
    _redirect_host(urlsplit(CONTROLLED_TARGET_BASE_URL).hostname or "", [loopback], monkeypatch)

    _rejected_without_send(client, account, project, run, sent)


def test_target_resolving_to_platform_address_blocks_the_send(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """允许域名在发送前解析到平台基础设施的地址（这里用当前 db 的地址）：拒绝且不发送。

    这与“名字叫 api”是两条不同的路径：域名本身合法、也在白名单里，只有解析结果指到了
    平台自己。改成 IP 直连同样绕不过主机名检查——别名当前指向的地址就是禁区地址。
    """
    _environment, run = _prepare_run(client, account, project, "基础设施地址环境")
    sent = _counting_send(monkeypatch)

    db_addresses = sorted(
        {info[4][0] for info in socket.getaddrinfo("db", None, proto=socket.IPPROTO_TCP)}
    )
    assert db_addresses, "集成测试环境里应当能解析出数据库地址"
    _redirect_host(urlsplit(CONTROLLED_TARGET_BASE_URL).hostname or "", [db_addresses[0]], monkeypatch)

    _rejected_without_send(client, account, project, run, sent)


@pytest.mark.parametrize("alias", ["api", "db", "host.docker.internal"])
def test_platform_alias_cannot_be_whitelisted_into_use(
    client: TestClient, account: dict, project: dict, alias: str
) -> None:
    """管理 API／数据库／宿主机入口的名字填进白名单也不会被放行。

    白名单保存本身照旧成功（保存只落配置、不试探目标），但运行创建时就按主机名层
    拒绝：这类名字由可信部署配置定义，普通项目的目标白名单覆盖不了它。
    """
    origin = f"http://{alias}:8080"
    environment = create_environment(
        client, account, project, name=f"基础设施目标环境-{alias}", base_url=origin
    )
    updated = client.put(
        f"{_pools_base(account, project)}/{project['pool_id']}/targets",
        json={"allowed_targets": [origin]},
    )
    assert updated.status_code == 200, updated.text
    assert origin in updated.json()["allowed_targets"], "白名单里确实写着它"

    response = client.post(
        f"{project_base(account, project)}/runs",
        json={
            "environment_id": environment["id"],
            "debug_snapshot": {"request": _echo_request(), "assertions": []},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "target_not_allowed"


# —— CF2：真实宿主机入口必须由部署模板提供，且它的地址真的进禁区 ——


def _real_addresses(host: str) -> list[str]:
    from app.kernel.target_policy import resolve_addresses

    return resolve_addresses(host)


def test_the_deployment_really_provides_the_host_entry() -> None:
    """部署模板必须真的提供容器到宿主机的入口（这里是**真实解析**，不加替身）。

    守卫把 `host.docker.internal` 当成必需来源，所以这个入口解不出来时所有发送都会被拒。
    也就是说：模板里的 `extra_hosts: host-gateway` 一旦被删掉或被改坏，不是“少一层保护”，
    而是整个平台发不出请求——这条用例把这个前提固定在真实部署上，改模板必须在这里红。
    """
    addresses = _real_addresses("host.docker.internal")

    assert addresses, "部署模板应当提供宿主入口并解析出地址"
    assert all(ipaddress.ip_address(address) for address in addresses)


def test_the_real_host_address_is_blocked_for_an_allowed_domain(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单域名在发送前解析到**真实宿主机入口地址**：拒绝，且一个请求都不发。

    这条用的不是测试造的假地址，而是本部署里宿主入口当前解析到的地址。宿主机既跑着
    平台本体、又常常是公司内网的入口，指向它的域名与直接写这个 IP 都必须被拦下；地址
    随容器重建变化，所以它只能由“每次发送前解析必需来源”得到，不能写成固定名单。
    """
    host_addresses = _real_addresses("host.docker.internal")

    _environment, run = _prepare_run(client, account, project, "宿主地址环境")
    sent = _counting_send(monkeypatch)
    _redirect_host(urlsplit(CONTROLLED_TARGET_BASE_URL).hostname or "", host_addresses, monkeypatch)

    _rejected_without_send(client, account, project, run, sent)


def test_an_unconfirmable_host_entry_sends_nothing(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主入口确认不了（例如自定义部署没有等价入口）：整次运行零发送。

    与上一条的区别在缺口方向：上一条是“入口在、但目标指向它”，这条是“入口不在”。
    只让宿主入口解析失败，其余主机走真实解析——数据库仍要连得上，运行才能走到发送前
    的地址复核；若这里改成跳过无法确认的来源，宿主的地址就重新没有地址层拦截。
    """
    _environment, run = _prepare_run(client, account, project, "宿主入口缺失环境")
    sent = _counting_send(monkeypatch)

    real_getaddrinfo = socket.getaddrinfo

    def failing_getaddrinfo(name, *args, **kwargs):
        if name == "host.docker.internal":
            raise socket.gaierror(-2, "Name or service not known")
        return real_getaddrinfo(name, *args, **kwargs)

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", failing_getaddrinfo)

    _rejected_without_send(client, account, project, run, sent)
