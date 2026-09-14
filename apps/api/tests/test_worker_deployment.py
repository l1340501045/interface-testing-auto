"""执行器必须由部署声明：运行它，就必须把它算进禁区。

执行器（`worker`）是**部署可选**的服务：`make up` 只起平台本体，它由显式命令启动，干净
克隆的验证流程在进程内调用同一份执行内核（`claim_job`／`execute_claim`），不依赖它运行。
守卫因此只要求**本部署实际启用**的名字解析成功，执行器必须由部署声明
（`PLATFORM_REQUIRED_HOSTS`）而不是被无条件要求——否则一个当前部署没有的名字会让所有
发送在地址复核阶段被拒，干净克隆的 `make up → make check` 直接失败。

放宽不能变成缺口，所以守护点放在进程启动处：**只要执行器在跑，声明就必须在**。缺声明时
明确失败并给出改法，不静默降级；比对走 `TargetGuard.requires`，与发送前那份名单同源，
不会出现“自查通过、发送前却按另一份名单判断”。

同一处还有第二道部署级前置条件：**地址层禁区此刻是否完整**（`require_host_boundary_ready`）。
宿主入口（`host.docker.internal`，由部署模板的 `extra_hosts: host-gateway` 提供）是必需
来源，解析不出来就拒绝发送；把这件事提前到启动时，是为了让“本部署提供不出宿主入口”
表现为启动失败，而不是让一台空转的执行器等第一条运行被拒。

前一半只看配置形态、不解析；后一半必须真的看解析结果——两者不混在一个函数里。

纯方法验证：前一半不连接数据库、不解析 DNS；后一半只用注入的假解析，不接触真实网络。
"""
from __future__ import annotations

import socket

import pytest

from app.config import Settings
from app.worker import WorkerConfigError, require_declared_executor, require_host_boundary_ready


def test_a_deployment_without_the_executor_is_not_required_to_declare_it() -> None:
    """不运行执行器的部署（干净安装）不该被要求声明一个自己没有的名字。"""
    settings = Settings(platform_required_hosts="api,db,web")

    with pytest.raises(WorkerConfigError, match="PLATFORM_REQUIRED_HOSTS"):
        require_declared_executor(settings)


def test_a_declared_executor_names_itself() -> None:
    """声明了就通过，并返回它在网络中的名字（启动日志用同一个值）。"""
    settings = Settings(platform_required_hosts="api,db,web,worker")

    assert require_declared_executor(settings) == "worker"


def test_the_declaration_is_matched_by_the_hostname_rules() -> None:
    """`WORKER.`、大小写与前后空格是同一个主机名，不能因为写法不同而拒绝启动。"""
    settings = Settings(platform_required_hosts=" web , WORKER. ")

    assert require_declared_executor(settings) == "worker"


def test_declaring_a_different_name_does_not_cover_this_process() -> None:
    """比对的是**本进程**在网络中的名字：声明了别的名字等于没声明。"""
    settings = Settings(worker_host="executor-1", platform_required_hosts="api,db,worker")

    with pytest.raises(WorkerConfigError, match="executor-1"):
        require_declared_executor(settings)


def test_a_renamed_service_declares_its_own_name() -> None:
    """部署给自己的执行器改名字时，声明同一个新名字即可通过。"""
    settings = Settings(worker_host="executor-1", platform_required_hosts="executor-1")

    assert require_declared_executor(settings) == "executor-1"


def test_a_malformed_declaration_is_a_configuration_error() -> None:
    """声明写错（写成 URL 或“名字:端口”）按配置错误报出，而不是启动后才失败。"""
    settings = Settings(platform_required_hosts="intranet-api:8000,worker")

    with pytest.raises(WorkerConfigError, match="来源配置无效"):
        require_declared_executor(settings)


def test_the_check_never_resolves_or_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    """自查只看部署配置：既不解析 DNS，也不向任何来源发请求。"""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("启动自查不应触发 DNS 解析或建立连接")

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", explode)
    monkeypatch.setattr("app.kernel.target_policy.socket.create_connection", explode)
    settings = Settings(platform_required_hosts="worker")

    assert require_declared_executor(settings) == "worker"


def _pin_sources(mapping: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """只让指定主机解析成功，其余按“解析不了”处理；地址用 TEST-NET-1，不碰真实网络。"""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        addresses = mapping.get(host)
        if not addresses:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", fake_getaddrinfo)


_FULL_SOURCES = {
    "api": ["192.0.2.11"],
    "db": ["192.0.2.12"],
    "host.docker.internal": ["192.0.2.10"],
    # 跑着执行器的部署会声明自己（`PLATFORM_REQUIRED_HOSTS`），声明了就必须能确认。
    "worker": ["192.0.2.13"],
}


def test_a_deployment_that_can_confirm_the_host_boundary_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署模板提供的宿主入口能解析时，地址层禁区是完整的，正常启动。"""
    _pin_sources(_FULL_SOURCES, monkeypatch)

    require_host_boundary_ready(Settings(platform_required_hosts="worker"))


def test_a_deployment_that_cannot_confirm_the_host_boundary_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主入口确认不了就不启动：宁可起不来，也不要一台持续空转、连不上宿主的执行器。

    这正是“实际宿主不能按需追加而默认无保护”的落点。若这里改成放行，一台**没有**等价
    宿主入口的部署会照常领工作项；宿主自己的地址既不在地址层禁区里，主机名层又只认
    名字（换个域名或直接写 IP 就绕过去了），指向宿主的用例会被真实发出去。

    错误信息必须给出改法，而不是只说“失败了”——操作者要能从启动日志直接知道是模板的
    ``extra_hosts`` 没生效，还是自定义部署需要自己声明宿主地址范围。

    这里只让宿主入口解析不出来（其余来源都给结果），拒绝原因就唯一指向它。
    """
    _pin_sources(
        {"api": ["192.0.2.11"], "db": ["192.0.2.12"], "worker": ["192.0.2.13"]}, monkeypatch
    )

    with pytest.raises(WorkerConfigError, match="宿主边界保护未就绪") as failure:
        require_host_boundary_ready(Settings(platform_required_hosts="worker"))

    message = str(failure.value)
    assert "host.docker.internal" in message, "错误里应点名确认不了的宿主入口"
    assert "extra_hosts" in message and "PLATFORM_FORBIDDEN_ADDRESSES" in message, (
        "错误里应给出两条改法：让模板的宿主入口生效，或显式声明宿主地址范围"
    )


def test_the_boundary_check_actually_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """与“只看部署配置”的声明自查不同：这一项必须真的看解析结果。

    它防的缺口恰恰是“名字在配置里、但这台机器上解析不出来”。若改成只看配置形态，
    上面那条拒绝用例会因为“名字在配置里”而通过，宿主的地址就重新没有地址层拦截。
    """
    calls: list[str] = []

    def record(host, port, *args, **kwargs):
        calls.append(host)
        if host not in _FULL_SOURCES:
            raise socket.gaierror(-2, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in _FULL_SOURCES[host]
        ]

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", record)

    require_host_boundary_ready(Settings(platform_required_hosts="worker"))

    assert "host.docker.internal" in calls, "启动自查必须解析宿主入口，而不是只看配置"
