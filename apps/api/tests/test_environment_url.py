"""环境基础地址的语法校验：接受什么、拒绝什么，以及错误信息里不出现原文。

这一层是校验的**唯一实现**：保存入口（创建／PATCH）与运行入口（`resolve_target`）都调
它，因此这里的行为就是两处共同的行为。过去两处都只做长度检查，裸主机名与“主机:端口”
能存进库，直到执行时才失败，并且被报成 `target_not_allowed`——用户看到的是“目标不在
白名单”，会去改用例路径，而真正要改的是环境地址。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx
import pytest

from app.kernel.environment_url import (
    ENVIRONMENT_URL_EXAMPLE,
    EnvironmentUrlError,
    check_base_url,
    normalize_base_url,
)


@pytest.mark.parametrize(
    "raw",
    [
        "http://echo:8080",
        "https://api.example.test",
        "https://service.example/api/v1",
        # IP＋端口必须继续支持：内网直连时它才是常见写法。地址用 TEST-NET-1（RFC 5737），
        # 不指向任何真实主机。
        "http://192.0.2.10:8080/base",
        "http://[2001:db8::1]:9000/api",
        "http://echo",
        "http://echo:8080/",
        "HTTP://Echo:8080/Api",
        # 基础路径里的百分号编码原样保留，不能被 URL 规范化改写。
        "http://echo:8080/a%20b/c",
        # 内网短主机名（无点）与带下划线的主机名都是真实存在的写法。
        "http://my_host.local:8080",
        "http://echo.",
    ],
)
def test_accepts_complete_service_addresses(raw: str) -> None:
    check_base_url(raw)
    assert normalize_base_url(raw) == raw, "合法地址必须原样保留，不能被改写"


@pytest.mark.parametrize(
    "raw",
    [
        "http://192.0.2.10:8080",
        "https://192.0.2.10",
        "http://192.0.2.10:8080/api/v1",
        "http://[2001:db8::1]:9000",
        "http://10.20.30.40:8080/base",
    ],
)
def test_ip_and_port_addresses_stay_supported(raw: str) -> None:
    """IP＋端口是受支持的写法，不能因为“日常用域名”而收窄。

    这里单独锁一遍：把 IP／端口支持删掉，这条会立刻失败，而不用依赖上面那条
    接受清单被逐项核对。
    """
    check_base_url(raw)
    assert normalize_base_url(raw) == raw


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        # 缺协议：这是本次要修的主要缺陷，裸主机名／主机:端口过去能存进库。
        ("echo", "http:// 或 https://"),
        ("echo:8080", "http:// 或 https://"),
        ("target-service:8080", "http:// 或 https://"),
        ("", "http:// 或 https://"),
        # 非 HTTP 协议不猜、不改写。
        ("ftp://echo:8080", "只支持 http 或 https"),
        ("file:///etc/hosts", "只支持 http 或 https"),
        # 缺主机。
        ("http://", "缺少主机名"),
        ("http:///echo", "缺少主机名"),
        # 畸形与越界端口。
        ("http://echo:abc", "端口号"),
        ("http://echo:99999", "端口号"),
        ("http://echo:0", "端口号"),
        ("http://echo:", "端口号"),
        ("http://echo:/echo", "端口号"),
        ("http://echo:80:90", "端口号"),
        # 控制字符与内部空白。
        ("http://echo/ec\nho", "控制字符"),
        ("http://echo:8080/a b", "控制字符"),
        ("http://echo\u200b:8080", "控制字符"),  # 零宽空格
        # 反斜杠：不同解析器对它的处理并不一致，不接受。
        ("http://echo:8080\\a", "反斜杠"),
        # userinfo 是凭证，必须走身份配置。
        ("http://user:pass@echo:8080", "账号信息"),
        # 查询串与片段都不是可以正确拼接的基础地址。
        ("http://echo:8080/api?token=1", "查询参数"),
        ("http://echo:8080/api#frag", "# 片段"),
        # 主机名形态不合法。
        ("http://-bad-:8080", "主机名不合法"),
        ("http://a..b:8080", "主机名不合法"),
        ("http://[::1", "无法解析"),
    ],
)
def test_rejects_addresses_that_cannot_be_joined(raw: str, reason: str) -> None:
    with pytest.raises(EnvironmentUrlError) as error:
        check_base_url(raw)
    assert reason in str(error.value)


def test_save_entry_trims_only_outer_spaces() -> None:
    """保存入口去首尾普通空格；其余部分（含路径）一个字都不改。"""
    assert normalize_base_url("  http://echo:8080/api/v1  ") == "http://echo:8080/api/v1"


def test_save_entry_does_not_invent_a_scheme() -> None:
    """trim 之后仍然缺协议就报错——不静默补 http://。"""
    with pytest.raises(EnvironmentUrlError):
        normalize_base_url(" echo:8080 ")


def test_runtime_check_does_not_trim() -> None:
    """运行入口按库里的原文校验：带多余空格的存量地址被拒绝，而不是被悄悄换掉。"""
    with pytest.raises(EnvironmentUrlError):
        check_base_url(" http://echo:8080 ")


def test_validation_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验只做语法判断：改环境地址不该依赖 DNS，更不该向目标发请求。

    目标服务停着的时候也必须能把配置改完——一旦这里悄悄加了解析或探活，改配置就会被
    网络状态绑住，而且“保存失败”与“目标不可达”会混成同一个错误。
    """
    # 先导入再替换已定位的对象属性：`target_policy` 走的是 socket 模块属性，替换后
    # 用 monkeypatch 恢复，不会留下持久的替身。
    import app.kernel.environment_url as module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("环境地址校验不得解析 DNS")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    assert not hasattr(module, "socket"), "校验模块本身不应依赖 socket"

    check_base_url("http://not-resolvable.invalid:8080/api/v1")
    assert normalize_base_url("  http://not-resolvable.invalid  ") == "http://not-resolvable.invalid"


def test_error_messages_never_echo_the_raw_address() -> None:
    """错误信息不回显原始地址：粘进来的原文里可能带着凭证或签名。"""
    secret = "s3cr3t-token-9f3a"
    raws = [
        f"http://echo:8080/path?access_token={secret}",
        f"http://user:{secret}@echo:8080",
        f"echo:8080?access_token={secret}",
    ]
    for raw in raws:
        with pytest.raises(EnvironmentUrlError) as error:
            check_base_url(raw)
        message = str(error.value)
        assert secret not in message, "错误信息不得回显原文里的秘密"
        assert raw not in message, "错误信息不得整段复述原始地址"


_RESERVED_TLDS = ("example", "test", "invalid", "localhost")
# RFC 5737 的文档用网段；示例地址取这里，不会指向任何真实主机。
_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


def _is_reserved_placeholder(url: str) -> bool:
    """主机是保留域名或文档网段：示例地址必须满足这一点。"""
    host = urlsplit(url).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.split(".")[-1] in _RESERVED_TLDS
    return any(address in network for network in _DOCUMENTATION_NETWORKS)


def test_the_example_in_the_message_is_a_reserved_placeholder() -> None:
    """示例只用保留域名／文档网段，不写任何真实环境。

    这里**不比对真实域名的字面量**——把它写进测试本身就是把真实地址带进了仓库。
    改为校验“主机属于保留占位”这一属性，效果相同且不引入真实值。
    """
    with pytest.raises(EnvironmentUrlError) as error:
        check_base_url("echo:8080")
    message = str(error.value)
    assert ENVIRONMENT_URL_EXAMPLE in message
    assert _is_reserved_placeholder(ENVIRONMENT_URL_EXAMPLE)


def test_the_placeholder_check_rejects_a_real_looking_host() -> None:
    """反例：这条属性检查本身是有效的，不是恒真的空断言。"""
    assert not _is_reserved_placeholder("https://intranet.corp.example.com.cn")
    assert not _is_reserved_placeholder("https://10.20.30.40:8080")


# —— 后端独立审查确认的漏验与域名兼容（P2×3、P3）——
#
# 这四组的共同根因是同一个：**解析成功不等于 HTTP 客户端支持**。`urlsplit` 能把
# `256.256.256.256` 和方括号里的 IPvFuture 拆开，本模块随后把它们当成普通域名放行；
# 而实际构造成被测请求的 HTTPX 会直接拒绝。反过来，含非 ASCII 的合法域名被 ASCII-only
# 的标签规则先拒掉了。两组都要在**保存与运行**两个入口同时成立。


@pytest.mark.parametrize(
    "raw",
    [
        "http://256.256.256.256:8080/api",  # 每段越界
        "http://1.2.3.04/api",  # 前导零
        "http://999.1.1.1/",
        "http://1.2.3.256:8080/",
    ],
)
def test_ipv4_shaped_host_must_be_a_valid_ipv4(raw: str) -> None:
    """四段全数字的外形只可能是 IPv4；越界段与前导零要说清**该改哪一段**。

    这条锁的是错误原因，不是“会不会被拒绝”：拒绝由客户端兼容闸门兜底，但那句话是泛化的
    “无法被 HTTP 客户端解析”，用户看不出问题出在 IP 的哪一段。因此这里断言具体原因——
    去掉外形分支后，本例会退化成闸门的泛化消息而失败。
    """
    with pytest.raises(EnvironmentUrlError) as error:
        check_base_url(raw)
    assert "IPv4" in str(error.value)
    with pytest.raises(EnvironmentUrlError):
        normalize_base_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://[v1.echo]:8080/api",
        "http://[vF.service.example]/api",
    ],
)
def test_ipvfuture_bracketed_host_is_rejected(raw: str) -> None:
    """方括号里只接受受支持的 IPv6 字面量；IPvFuture 不得落进域名分支。

    当前标准库的 `urlsplit` 允许 IPvFuture 并把 `[v1.echo]` 的 hostname 交成 `v1.echo`，
    随后按普通域名通过标签规则。前端与 WHATWG 解析器都拒绝它，HTTPX 构造请求也拒绝。
    同一条分支还承担**接受**方向：IPv6 字面量含冒号，不先走方括号分支会被域名标签规则误拒。
    """
    with pytest.raises(EnvironmentUrlError) as error:
        check_base_url(raw)
    assert "IPv6" in str(error.value)


def test_ipv6_bracketed_host_is_measured_as_an_ip_not_a_domain() -> None:
    """反例方向：方括号分支必须真的接受 IPv6 字面量。

    没有这条，把方括号分支删掉也不会被发现——IPv6 会被域名标签规则按“含冒号的标签”误拒。
    """
    for raw in ("http://[2001:db8::1]:9000/api", "http://[::1]/", "https://[::ffff:10.0.0.5]/x"):
        check_base_url(raw)
        assert normalize_base_url(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "http://例子.测试:8080/api",
        "https://bücher.example/api",
        "http://xn--fsqu00a.xn--0zwm56d:8080/api",
    ],
)
def test_international_domains_stay_accepted(raw: str) -> None:
    """合法国际化域名必须继续可用：它既有来源本就通过目标策略，只是被本次新增规则拒了。"""
    check_base_url(raw)
    assert normalize_base_url(raw) == raw, "原文与基础路径都不能被改写"


def test_root_dot_length_is_measured_without_the_root_dot() -> None:
    """根域点不占长度：253 字符的主机带尾点仍合法，254 字符的即使不带尾点也过长。

    原实现先把 253 的上限套在**含**尾点的文本上，于是最大合法 FQDN 被误拒——“尾部单个点
    接受”那条分支根本轮不到执行。
    """
    max_host = ".".join(["a" * 63] * 3 + ["a" * 61])  # 253 字符
    assert len(max_host) == 253
    assert len(max_host + ".") == 254

    check_base_url(f"http://{max_host}./api")
    check_base_url(f"http://{max_host}/api")

    with pytest.raises(EnvironmentUrlError):
        check_base_url(f"http://{max_host}a/api")  # 254 字符且不带尾点


@pytest.mark.parametrize(
    "raw",
    [
        # 罗马数字与全角字母都落在 `\\w` 里，但 IDNA 不接受它们；只有真实 HTTP 客户端
        # 那一层能判出来。这一组保证“客户端兼容闸门”不是装饰：删掉它，这两条会被放行。
        "http://Ⅷ.example/",
        "http://ａ.example/",
    ],
)
def test_host_the_real_client_cannot_parse_is_rejected(raw: str) -> None:
    """标签规则挡不住的主机由真实客户端那一层拒绝，错误仍是稳定的地址错误。

    这两个样本能通过宽松的标签规则，但 IDNA 不接受（罗马数字不在 IDNA2008 字符集、
    全角字母需要先做兼容分解）。它们是闸门承重的证据：没有闸门就会被存进库。
    """
    with pytest.raises(EnvironmentUrlError) as error:
        check_base_url(raw)
    assert "HTTP 客户端" in str(error.value)


def test_label_rule_rejects_hyphen_edges_that_the_client_accepts() -> None:
    """反方向承重：HTTPX 接受 `-bad-`，是我们自己的标签规则拒绝它。

    两个方向都要有：闸门挡住标签规则放行的（上面一条），标签规则挡住闸门放行的（这一条）。
    否则“闸门 + 标签规则”里总有一半是装饰。
    """
    assert httpx.URL("http://-bad-:8080/")  # 客户端本身接受
    with pytest.raises(EnvironmentUrlError):
        check_base_url("http://-bad-:8080/")


def test_ipv4_shaped_and_bracket_hosts_are_also_rejected_at_run_entry() -> None:
    """运行入口与保存入口用同一份判断：坏地址在创建运行时就被拒绝。

    只用保存入口验证是不够的——存量坏数据的表现取决于 `resolve_target` 是否也走这份校验。
    """
    from app.kernel.target_policy import TargetGuard
    from app.models import Environment
    from app.services.run_coordinator import RunRejected, resolve_target

    request = {"method": "GET", "path": "/echo"}
    for raw in (
        "http://256.256.256.256:8080",
        "http://1.2.3.04",
        "http://[v1.echo]:8080",
    ):
        guard = TargetGuard([raw])
        with pytest.raises(RunRejected) as error:
            resolve_target(request, Environment(kind="test", base_url=raw), guard)
        assert error.value.code == "environment_url_invalid"

    # 合法国际化域名在运行入口也不该被拦（错误码稳定为具体地址问题，而不是白名单问题）。
    international = "http://例子.测试:8080"
    target = resolve_target(
        request, Environment(kind="test", base_url=international), TargetGuard([international])
    )
    assert target[1] == "http://例子.测试:8080"
