"""目标访问控制：禁用地址段、DNS 解析结果、平台基础设施禁区与生产阻断。

判别重点都是“换个写法就能绕过”的等价形式：

- IPv4 映射 IPv6 地址（``::ffff:127.0.0.1`` 与 ``127.0.0.1`` 是同一台主机，在 Python
  里却是版本不同的两个对象）；
- 主机名与解析地址两条路径（域名在白名单里只说明来源被允许，不代表它这次解析到的
  地址可以连接）；
- 平台基础设施的别名与它当前指向的地址（写别名还是写 IP 直连必须得到同一结论）。

解析结果由注入的假 `getaddrinfo` 提供：不查真实 DNS，也不访问任何元数据、
管理 API、数据库或宿主机入口。
"""
from __future__ import annotations

import socket
from urllib.parse import urlsplit

import pytest

from app.kernel.target_policy import (
    TargetGuard,
    TargetPolicyError,
    _forbidden_address,
    _parse_networks,
)

# 基线必需来源（管理 API、数据库、容器到宿主机的入口）在测试里的固定地址。守卫要求这些
# 来源必须解析得到，所以每个用例都要给它们结果——给的是互不相干的 TEST-NET-1 地址，既让
# “部署完整确认”成立，又不会撞上被测目标的地址而改变拒绝原因。宿主入口与 api／db 一样是
# 基线必需项：部署模板用 `extra_hosts: host-gateway` 提供它，这里按同样方式“提供”一个地址。
# 执行器（worker）不在基线里：它是部署可选的服务，只在部署声明时才被要求，见
# `test_declared_executor_...` 与 `test_a_deployment_without_the_executor_can_still_send`。
_HOST_ENTRY = "host.docker.internal"
_CORE_SOURCES = {_HOST_ENTRY: "192.0.2.10", "api": "192.0.2.11", "db": "192.0.2.12"}


def _pin(
    mapping: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch, *, core: bool = True
) -> None:
    """只让指定主机解析成功，其余主机按“解析不了”处理。

    必须按主机给结果：守卫在地址层还会解析平台基础设施的名字来封掉 IP 直连，若所有
    主机都返回同一个地址，那些名字也会被指到被测地址上——测出来的拒绝与本次要验证
    的规则无关。

    默认同时把基线的三个必需来源（api／db／宿主入口）指到 TEST-NET-1：它们在真实部署里
    必须是可确认的，缺一个就是“禁区证明不了完整”，会让所有用例都变成解析失败。要专门
    验证某个来源解析失败的用例传 ``core=False``（或用 ``mapping`` 覆盖其中一个名字）。
    """

    pinned = {**_CORE_SOURCES, **mapping} if core else dict(mapping)

    def fake_getaddrinfo(host, port, *args, **kwargs):
        addresses = pinned.get(host)
        if not addresses:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "::ffff:a9fe:a9fe",
        "fe80::1",
        # 带 scope id 的链路本地地址是同一个地址的另一种写法，不能因解析不了就放行。
        "fe80::1%eth0",
        "fd00:ec2::254",
        "127.0.0.1",
        "::ffff:127.0.0.1",
        "::1",
        # Linux 上连 0.0.0.0/:: 就是连本机回环。
        "0.0.0.0",
        "::",
    ],
)
def test_forbidden_addresses_including_mapped_forms(address: str) -> None:
    assert _forbidden_address(address) is True


@pytest.mark.parametrize(
    ("plain", "mapped"),
    [
        ("127.0.0.1", "::ffff:127.0.0.1"),
        ("10.0.0.5", "::ffff:10.0.0.5"),
        ("192.168.1.10", "::ffff:192.168.1.10"),
    ],
)
def test_mapped_form_matches_plain_form(plain: str, mapped: str) -> None:
    """映射形式与普通形式必须得到同一结论，不能因写法不同而放宽。"""
    assert _forbidden_address(mapped) == _forbidden_address(plain)


def test_unparsable_address_is_treated_as_forbidden() -> None:
    """解析不出来的地址不能证明它安全，按拒绝处理，而不是放行。"""
    assert _forbidden_address("not-an-address") is True


@pytest.mark.parametrize(
    "loopback",
    ["127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "::"],
)
def test_allowed_host_resolving_to_loopback_is_rejected(
    loopback: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单内的域名解析到环回：三种写法都必须拒绝。

    域名在白名单里只说明来源被允许，不代表这次解析出的地址可以连接；DNS 改绑正是
    发生在两者之间。
    """
    guard = TargetGuard(["http://echo.internal:8080"])
    target = guard.authorize_url("http://echo.internal:8080/echo")
    _pin({"echo.internal": [loopback]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="不允许的地址段"):
        guard.pinned_address(target)


def test_guard_rejects_host_resolving_to_mapped_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单内的主机若解析到映射后的元数据地址，仍必须被拦下。"""
    guard = TargetGuard(["http://echo.internal:8080"])
    target = guard.authorize_url("http://echo.internal:8080/echo")
    _pin({"echo.internal": ["::ffff:169.254.169.254"]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="不允许的地址段"):
        guard.pinned_address(target)


def test_guard_rejects_any_pointing_to_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析出多个地址时，只要有一个落在禁区就必须整体拒绝。"""
    guard = TargetGuard(["http://echo.internal:8080"])
    target = guard.authorize_url("http://echo.internal:8080/echo")
    _pin({"echo.internal": ["10.0.0.5", "::ffff:169.254.169.254"]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="不允许的地址段"):
        guard.pinned_address(target)


def test_guard_allows_mapped_form_of_permitted_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通允许地址的映射写法不应被误伤：修复的是等价判断，不是一律拒绝 IPv6。"""
    guard = TargetGuard(["http://echo.internal:8080"])
    target = guard.authorize_url("http://echo.internal:8080/echo")
    _pin({"echo.internal": ["::ffff:10.0.0.5"]}, monkeypatch)

    assert guard.pinned_address(target) == "::ffff:10.0.0.5"


@pytest.mark.parametrize(
    "address", ["10.0.0.5", "192.168.1.10", "172.16.9.9", "::ffff:10.0.0.5"]
)
def test_private_targets_remain_available(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式授权的私网目标必须仍然可用：不能为了挡环回把公司内网一起禁掉。"""
    guard = TargetGuard(["http://intranet.test:8080"])
    target = guard.authorize_url("http://intranet.test:8080/echo")
    _pin({"intranet.test": [address]}, monkeypatch)

    assert guard.pinned_address(target) == address


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1:8080/echo", "不允许的地址段"),
        ("http://[::1]:8080/echo", "不允许的地址段"),
        ("http://0.0.0.0:8080/echo", "不允许的地址段"),
        # 平台基础设施的名字：连白名单都写成它也不行，主机名层直接拒绝。
        ("http://api:8080/echo", "平台基础设施"),
        ("http://db:8080/echo", "平台基础设施"),
        ("http://web:8080/echo", "平台基础设施"),
        ("http://worker:8080/echo", "平台基础设施"),
        ("http://host.docker.internal:8080/echo", "平台基础设施"),
        ("http://gateway.docker.internal:8080/echo", "平台基础设施"),
        ("http://localhost:8080/echo", "平台基础设施"),
        # 尾点写法是同一个主机名，不能靠加个点绕过去。
        ("http://API.:8080/echo", "平台基础设施"),
        ("http://metadata.google.internal/computeMetadata/v1/", "元数据"),
    ],
)
def test_forbidden_hosts_are_rejected_at_the_hostname_stage(url: str, reason: str) -> None:
    """把禁区填进白名单也不会被放行：判断发生在白名单之前，且不依赖 DNS。"""
    parts = urlsplit(url)
    guard = TargetGuard([f"{parts.scheme}://{parts.netloc}"])

    with pytest.raises(TargetPolicyError, match=reason):
        guard.authorize_url(url)


def test_declared_platform_hosts_extend_the_baseline() -> None:
    """部署可以用配置追加自己环境里的基础设施名字，而不是改代码。"""
    guard = TargetGuard(["http://intranet-api:8000"], platform_hosts=["intranet-api"])

    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.authorize_url("http://intranet-api:8000/echo")


def test_platform_alias_address_cannot_be_reached_by_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改用平台别名当前指向的 IP 直连同样被拒：别名与地址两条路径结论一致。"""
    guard = TargetGuard(["http://alias.test:8080"])
    target = guard.authorize_url("http://alias.test:8080/echo")
    _pin({"api": ["10.9.9.9"], "alias.test": ["10.9.9.9"]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(target)


def test_declared_platform_subnet_blocks_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署声明的禁区网段同样按地址层生效，且多结果里命中一个就整体拒绝。"""
    guard = TargetGuard(["http://alias.test:8080"], platform_addresses=["10.9.9.0/24"])
    target = guard.authorize_url("http://alias.test:8080/echo")
    _pin({"alias.test": ["10.0.0.5", "10.9.9.9"]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(target)


def test_invalid_platform_address_entry_is_a_config_error() -> None:
    """配置里的禁区条目写错了必须明确报出，不能静默忽略成“没有禁区”。"""
    with pytest.raises(TargetPolicyError, match="平台禁区条目无效"):
        TargetGuard(["http://echo:8080"], platform_addresses=["10.9.9.0/33"])


def test_authorize_url_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建运行不解析 DNS：解析随网络波动，不该让合法的运行创建失败。"""

    def explode(*args, **kwargs):
        raise AssertionError("authorize_url 不应触发 DNS 解析")

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", explode)
    guard = TargetGuard(["http://echo:8080"])

    assert guard.authorize_url("http://echo:8080/echo").origin == "http://echo:8080"


def test_metadata_hostnames_are_rejected_before_resolution() -> None:
    guard = TargetGuard(["http://metadata.google.internal:80"])
    with pytest.raises(TargetPolicyError, match="元数据"):
        guard.authorize_url("http://metadata.google.internal/computeMetadata/v1/")


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        # 声明的两种写法必须展开成同一批网络对象：等价语义只发生在这一处。
        ("::ffff:10.9.9.0/120", {"10.9.9.0/24", "::ffff:10.9.9.0/120"}),
        ("10.9.9.0/24", {"10.9.9.0/24", "::ffff:10.9.9.0/120"}),
        ("::ffff:10.9.9.9/128", {"10.9.9.9/32", "::ffff:10.9.9.9/128"}),
        ("10.9.9.9", {"10.9.9.9/32", "::ffff:10.9.9.9/128"}),
        # 非映射的 IPv6 段没有 IPv4 等价形式，保持原样，不误伤其他地址空间。
        ("2001:db8::/32", {"2001:db8::/32"}),
    ],
)
def test_declared_network_forms_are_normalized_in_one_place(
    declared: str, expected: set[str]
) -> None:
    """规范化责任层自身的契约：两种等价写法产出同一批网段。

    这一层的展开在行为上被地址侧的等价展开覆盖（地址侧是完备的），所以只靠“应拒绝／
    应允许”看不出它被删掉。但两侧共用同一份等价语义、各自独立展开，是为了让任何一侧
    将来被单独改动时另一侧仍能兜住——这里把这个契约固定住，声明侧被改回只保留原样时
    必须报错，而不是悄悄退化成“只有目标侧在兜”。

    断言的是**两种写法得到同一批结果**，而不是拿两个同样错误的返回值互相比较：期望值
    写成了明确的网段集合，少一个、多一个都会失败。
    """
    assert {str(network) for network in _parse_networks([declared])} == expected


def test_production_environment_is_blocked() -> None:
    guard = TargetGuard(["http://echo:8080"])
    with pytest.raises(TargetPolicyError, match="生产环境"):
        guard.check_environment("production")
    guard.check_environment("test")


def test_core_sources_are_required_even_when_configuration_lists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api／db（平台本体）不能被配置当成“可选名字”摘掉。

    目标本身完全合法（在白名单内、解析到的私网地址也不在禁段），但只要部署的基础设施
    确认不了，这次发送就必须被拦下：解析失败时拼不出完整的禁区，而“解析不了就跳过”
    正好让指向同一台主机的别名合法漏过去。

    执行器（worker）不在这里：它是部署可选的服务，`make up` 不启动它，未声明的部署
    不该因为“没有这个进程”而无法发送——这正是干净克隆的 make up → make check 能跑通
    的前提。声明了它的部署照旧必须确认，见下一个用例。
    """
    guard = TargetGuard(["http://echo.internal:8080"])
    target = guard.authorize_url("http://echo.internal:8080/echo")
    assert guard.required_hosts == ["api", "db", _HOST_ENTRY]
    for host in ("web", "worker"):
        assert host not in guard.required_hosts, f"未声明的可选服务不该被无条件要求：{host}"

    _pin({"echo.internal": ["10.0.0.5"]}, monkeypatch, core=False)

    with pytest.raises(TargetPolicyError) as failure:
        guard.pinned_address(target)

    message = str(failure.value)
    assert "无法确认" in message
    for host in ("api", "db", _HOST_ENTRY):
        assert host in message, f"错误里应点名确认不了的来源：{host}"


def test_a_deployment_without_the_executor_can_still_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有执行器的部署必须能真实发送：干净克隆里就只有平台本体在跑。

    执行器是**部署可选**的进程（`make up` 不启动它，检查流程在进程内调用同一份执行
    内核），所以“这个名字解析不出来”不等于“禁区证明不了完整”——要求一个本部署根本
    没有的服务解析成功，会把所有干净克隆的发送全部拒掉。这条用例固定住该方向。
    """
    guard = TargetGuard(["http://echo:8080", "http://echo-alt:8080"])
    assert guard.required_hosts == ["api", "db", _HOST_ENTRY]
    first = guard.authorize_url("http://echo:8080/echo")
    _pin({"echo": ["172.22.0.4"]}, monkeypatch)

    assert guard.pinned_address(first) == "172.22.0.4"


def test_declared_executor_that_cannot_be_confirmed_blocks_the_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署声明了执行器之后，它就和基线来源一样必须确认：解析不出来就拒绝发送。

    放宽的只是“未声明就不要求”，不是“声明了也能跳过”。复现的缺口是：声明了执行器的
    部署与允许的白名单域名指向同一地址时，若因为“这个名字这次解析不出来”就跳过，
    目标会被**接受**——一台正在运行的执行器于是合法地暴露在禁区之外。

    两种状态都断言拒绝，且断言的是两条不同原因；第一次调用还必须不留下缓存，第二次
    发送要重新完整确认。执行器进程启动时自查的是同一份名单（`require_declared_executor`），
    所以“跑着执行器却没声明”这件事在部署里无法成立。
    """
    guard = TargetGuard(["http://allowed.invalid:8080"], required_hosts=["worker"])
    target = guard.authorize_url("http://allowed.invalid:8080/echo")
    assert "worker" in guard.required_hosts, "部署声明的执行器必须被确认，不是只写进名单"

    # 状态一：声明的执行器解析失败（例如声明了却忘记启动它）。禁区拼不完整，拒绝。
    _pin({"allowed.invalid": ["10.9.9.9"]}, monkeypatch)
    with pytest.raises(TargetPolicyError, match="无法确认") as failure:
        guard.pinned_address(target)
    assert "worker" in str(failure.value), "错误里应点名确认不了的执行器"

    # 状态二：同一个守卫、同一个目标，执行器这次解析到同一地址 → 目标判成平台基础设施。
    _pin({"allowed.invalid": ["10.9.9.9"], "worker": ["10.9.9.9"]}, monkeypatch)
    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(target)


def test_declared_source_that_cannot_be_confirmed_never_shrinks_the_forbidden_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一基础设施 IP 的别名，在 DNS 解析成功与失败两种情况下都不能发送。

    复现的是一个“解析失败就跳过”的缺口：声明了保护来源 ``critical.internal``，它与被
    允许的白名单域名 ``allowed.invalid`` 当前指向同一个地址。解析成功时别名会进禁区，
    目标被拒；解析失败时若把它当作“这次没有这个来源”跳过，同一目标就会被**接受**。

    两种状态都断言拒绝，且断言的是**两条不同的、各自正确的原因**——不是把一个同样错误
    的返回值与自身比较。第一次调用（解析失败）还必须**不留下缓存**：同一个守卫在下一
    次发送时要重新完整确认，而不是拿半个结果继续放行。
    """
    guard = TargetGuard(["http://allowed.invalid:8080"], platform_hosts=["critical.internal"])
    target = guard.authorize_url("http://allowed.invalid:8080/echo")
    assert "critical.internal" in guard.required_hosts, "声明的保护来源必须被确认，不是只写进名单"

    # 状态一：保护来源解析失败。禁区拼不完整，拒绝——这正是旧行为会放行的那一刻。
    _pin({"allowed.invalid": ["10.9.9.9"]}, monkeypatch)
    with pytest.raises(TargetPolicyError, match="无法确认"):
        guard.pinned_address(target)

    # 状态二：同一个守卫、同一个目标，保护来源这次解析成功。若上一步留下了“没有这个
    # 来源”的部分缓存，这里就会放行；正确行为是重新解析并把同一地址判成基础设施。
    _pin({"allowed.invalid": ["10.9.9.9"], "critical.internal": ["10.9.9.9"]}, monkeypatch)
    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(target)


def test_missing_optional_alias_does_not_block_allowed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确实未启用的可选兼容别名缺失，不能误伤真正被授权的受控目标。

    干净安装里往往只有本次部署真正启用的名字能解析（这里只有平台本体的 api／db、容器到
    宿主机的入口与两个受控目标服务），``host.containers.internal``／``gateway.docker.internal``
    这类**其它平台**的兼容别名解析不出来是正常的：它们仍由主机名层静态拒绝，但不得因为
    它们缺席而拒绝一次合法发送。

    这里同时固定住“可选”与“必需”的分界：可选别名缺席时 ``echo``／``echo-alt`` 两个受控
    目标都必须照常发送成功；宿主入口缺席时则必须拒绝（见下一条用例）——把后者也当成
    可选，就是让宿主自己的地址在没有地址层拦截的情况下被连上。
    """
    guard = TargetGuard(["http://echo:8080", "http://echo-alt:8080"])
    assert guard.required_hosts == ["api", "db", _HOST_ENTRY]
    for alias in ("host.containers.internal", "gateway.docker.internal"):
        assert alias not in guard.required_hosts, f"未启用的兼容别名不该是必需来源：{alias}"

    second = guard.authorize_url("http://echo-alt:8080/echo")
    first = guard.authorize_url("http://echo:8080/echo")
    _pin({"echo": ["172.22.0.4"], "echo-alt": ["172.22.0.2"]}, monkeypatch)

    assert guard.pinned_address(first) == "172.22.0.4"
    assert guard.pinned_address(second) == "172.22.0.2"


_HOST_IP = "192.168.65.254"


def test_the_host_entry_is_required_so_the_host_address_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容器到宿主机的入口是**必需**来源：宿主自己的地址不能成为被测目标。

    宿主既跑着平台本体、又常常是公司内网的入口。它只当“可选别名”时有两个缺口，这里
    各固定一条：

    1. **域名指向宿主**：白名单域名当前解析到宿主入口的地址。入口若因解析失败被跳过，
       地址层就只剩部署声明的网段，这个目标会被**接受**——域名在白名单里只说明来源被
       允许，不代表它这次解析到的地址可以连。
    2. **直接写宿主 IP**：目标写成宿主地址本身。主机名层只认名字，换个写法就绕过去了，
       拦下它的只能是“入口解析到的地址进禁区”这条。

    两种状态都断言拒绝，且断言的是两条不同原因；第一次调用（入口解析失败）还必须不留
    缓存，第二次发送要重新完整确认。
    """
    guard = TargetGuard(["http://allowed.invalid:8080", f"http://{_HOST_IP}:8080"])
    by_name = guard.authorize_url("http://allowed.invalid:8080/echo")
    by_ip = guard.authorize_url(f"http://{_HOST_IP}:8080/echo")
    assert _HOST_ENTRY in guard.required_hosts

    # 状态一：宿主入口解析失败（自定义部署没提供等价入口）。禁区证明不了完整，零发送。
    _pin({"allowed.invalid": [_HOST_IP]}, monkeypatch, core=False)
    with pytest.raises(TargetPolicyError, match="无法确认") as failure:
        guard.pinned_address(by_name)
    assert _HOST_ENTRY in str(failure.value), "错误里应点名确认不了的宿主入口"

    # 状态二：入口解析成功 → 它解析到的地址进禁区。域名指向宿主与 IP 直连都必须被拒，
    # 而且第二次调用要重新完整确认（上一步不留下“没有这个来源”的半个结果）。
    _pin(
        {
            _HOST_ENTRY: [_HOST_IP],
            # 目标侧的结果：域名解析到宿主，IP 直连要解析的就是它自己。
            "allowed.invalid": [_HOST_IP],
            _HOST_IP: [_HOST_IP],
            "api": ["192.0.2.11"],
            "db": ["192.0.2.12"],
        },
        monkeypatch,
    )
    for target in (by_name, by_ip):
        with pytest.raises(TargetPolicyError, match="平台基础设施"):
            guard.pinned_address(target)


def test_a_declared_host_address_range_blocks_both_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自定义部署提供不出宿主入口时的替代声明：显式声明宿主地址范围同样生效。

    部署模板无条件提供宿主入口，所以这条是给**改用自己的部署方式**的团队的等价路径：
    在 ``PLATFORM_FORBIDDEN_ADDRESSES`` 里声明宿主的地址／网段。声明与目标两侧的
    IPv4 ↔ 映射 IPv6 等价语义共用同一处实现，换个写法不能绕过。
    """
    guard = TargetGuard(
        ["http://allowed.invalid:8080", f"http://{_HOST_IP}:8080"],
        platform_addresses=[_HOST_IP],
    )
    _pin({"allowed.invalid": [_HOST_IP]}, monkeypatch)

    # 域名指向宿主：地址层拦下（源在白名单里不代表它这次解析到的地址可以连）。
    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(guard.authorize_url("http://allowed.invalid:8080/echo"))
    # 直接写宿主地址：主机名层就用同一份声明拦下，不必等到解析。
    with pytest.raises(TargetPolicyError, match="不允许的地址段"):
        guard.authorize_url(f"http://{_HOST_IP}:8080/echo")


@pytest.mark.parametrize(
    "entry",
    [
        "https://intranet-api:8000",
        "intranet-api:8000",
        "intranet-api/prod",
        "intranet api",
        "intranet-api/",
    ],
)
def test_invalid_required_source_entry_is_a_config_error(entry: str) -> None:
    """必需的来源条目写错了必须明确报出，不能静默变成一个永远匹配不上的名字。

    少确认一个来源等于把禁区悄悄缩小，所以形态检查在**构造守卫时**就做，属于配置错误。
    """
    with pytest.raises(TargetPolicyError, match="来源配置无效"):
        TargetGuard(["http://echo:8080"], platform_hosts=[entry])
    with pytest.raises(TargetPolicyError, match="来源配置无效"):
        TargetGuard(["http://echo:8080"], required_hosts=[entry])


def test_configuring_sources_never_contacts_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置检查只看名字形态：既不解析 DNS，也不向任何来源发请求。

    “保存配置”不该靠试发一个请求来验证，也不该在配置加载时依赖网络可达性。
    """

    def explode_resolve(*args, **kwargs):
        raise AssertionError("构造守卫不应触发 DNS 解析")

    def explode_connect(*args, **kwargs):
        raise AssertionError("地址固定只做名字解析，不应建立连接")

    monkeypatch.setattr("app.kernel.target_policy.socket.getaddrinfo", explode_resolve)
    monkeypatch.setattr("app.kernel.target_policy.socket.socket", explode_connect)
    monkeypatch.setattr("app.kernel.target_policy.socket.create_connection", explode_connect)

    guard = TargetGuard(
        ["http://echo:8080"],
        platform_hosts=["intranet-api"],
        platform_addresses=["10.9.9.0/24"],
        required_hosts=["intranet-api", "intranet-api"],
    )

    assert guard.required_hosts == ["api", "db", _HOST_ENTRY, "intranet-api"]
    # 主机名层同样不解析 DNS：创建运行只做静态判断。
    assert guard.authorize_url("http://echo:8080/echo").origin == "http://echo:8080"


@pytest.mark.parametrize(
    ("declared", "target", "forbidden"),
    [
        # 声明写映射形式、目标写普通形式：覆盖同一批主机，必须拒绝。
        ("::ffff:10.9.9.0/120", "10.9.9.9", True),
        ("::ffff:10.9.9.0/120", "10.9.9.1", True),
        # /120 就是 10.9.9.0/24：上边界在内，紧邻的上下两侧在外。
        ("::ffff:10.9.9.0/120", "10.9.9.255", True),
        ("::ffff:10.9.9.0/120", "10.9.10.0", False),
        ("::ffff:10.9.9.0/120", "10.9.8.255", False),
        # 反向：声明写普通形式、目标写映射形式。只单向展开就会在这里漏过去。
        ("10.9.9.0/24", "::ffff:10.9.9.9", True),
        ("10.9.9.0/24", "::ffff:10.9.10.0", False),
        # 单地址的 /128 与普通 /32 是同一台主机的两种写法。
        ("::ffff:10.9.9.9/128", "10.9.9.9", True),
        ("::ffff:10.9.9.9/128", "10.9.9.10", False),
        ("10.9.9.9/32", "::ffff:10.9.9.9", True),
        ("10.9.9.9", "::ffff:10.9.9.9", True),
        ("10.9.9.9", "10.9.9.10", False),
        # 正常非映射 IPv6 与普通私网地址不受影响：修复的是等价语义，不是一律拒绝。
        ("::ffff:10.9.9.0/120", "2001:db8::1", False),
        ("2001:db8::/32", "::ffff:10.9.9.9", False),
        ("2001:db8::/32", "2001:db8::5", True),
        ("::ffff:10.9.9.0/120", "10.0.0.5", False),
    ],
)
def test_declared_networks_match_across_ip_versions(
    declared: str, target: str, forbidden: bool
) -> None:
    """声明的网段与目标地址双向等价：换任何一种写法都必须得到同一结论。"""
    networks = _parse_networks([declared])
    assert _forbidden_address(target, networks) is forbidden


@pytest.mark.parametrize(
    ("declared", "resolved"),
    [
        # 声明映射网段、目标域名解析到普通 IPv4。
        ("::ffff:10.9.9.0/120", "10.9.9.9"),
        # 声明普通网段、目标域名解析到映射形式。
        ("10.9.9.0/24", "::ffff:10.9.9.9"),
    ],
)
def test_declared_network_blocks_target_regardless_of_declaration_form(
    declared: str, resolved: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """地址层同样按等价形式生效：白名单域名解析到禁区地址一律拒绝。"""
    guard = TargetGuard(["http://alias.test:8080"], platform_addresses=[declared])
    target = guard.authorize_url("http://alias.test:8080/echo")
    _pin({"alias.test": [resolved]}, monkeypatch)

    with pytest.raises(TargetPolicyError, match="平台基础设施"):
        guard.pinned_address(target)


def test_declared_mapped_network_blocks_ip_literal_at_hostname_stage() -> None:
    """直接写 IP 也绕不过：主机名层的声明网段判断使用同一套等价语义。"""
    guard = TargetGuard(["http://10.9.9.9:8080"], platform_addresses=["::ffff:10.9.9.0/120"])

    with pytest.raises(TargetPolicyError, match="不允许的地址段"):
        guard.authorize_url("http://10.9.9.9:8080/echo")


def test_equivalent_declarations_accept_the_same_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一批主机的两种声明写法在**允许**方向上也必须一致，不能只让拒绝方向对齐。"""
    allowed = "10.9.10.1"
    for declared in ("::ffff:10.9.9.0/120", "10.9.9.0/24"):
        guard = TargetGuard(["http://alias.test:8080"], platform_addresses=[declared])
        target = guard.authorize_url("http://alias.test:8080/echo")
        _pin({"alias.test": [allowed]}, monkeypatch)

        assert guard.pinned_address(target) == allowed
