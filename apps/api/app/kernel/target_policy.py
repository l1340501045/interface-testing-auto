"""目标访问控制：显式来源白名单、平台基础设施禁区、生产阻断与 DNS 改绑防护。

被测请求只允许发往显式允许的 origin。白名单按 scheme/host/port 精确匹配，
不因为目标带“测试环境”标签就放开环回、平台基础设施或云元数据地址；生产环境
在本阶段一律拒绝执行，避免用草稿标签绕过未来的生产许可。

两层判断缺一不可：

- **主机名层**（`authorize_url`，创建运行时就跑）：目标写成环回、未指定地址或平台
  基础设施的名字（管理 API／数据库／执行器宿主机入口）时直接拒绝。这一层**不解析
  DNS**，创建运行因此不会因解析波动被拒。
- **地址层**（`pinned_address`，发送前一刻）：把主机名解析出的**每一个**地址都与
  禁区比对，命中任意一个就整体拒绝。域名在白名单内只说明来源被允许，不代表这次
  解析到的地址可以连接——DNS 改绑正发生在两者之间。

平台基础设施的禁区来自**可信部署配置**（`Settings.target_guard`），并且分成两类，
不能混为一谈：

- **静态禁止访问的名称**：基础名单覆盖当前 Compose 的 api／db／web／worker 与容器到
  宿主机的入口，外加各平台／运行时的兼容别名（当前部署可能并没有在用）。
  `PLATFORM_FORBIDDEN_HOSTS` 与 `PLATFORM_FORBIDDEN_ADDRESSES` 只能追加、不能删减。
  这一层完全静态、不解析，因此“别名存不存在”与本层无关。
- **必须完整确认地址的来源**：代码基线的管理 API 与数据库，加上**本次部署显式声明**的
  来源：`PLATFORM_FORBIDDEN_HOSTS` 声明的保护来源、`PLATFORM_REQUIRED_HOSTS` 里且本部署
  真的在用的名字、实际 `PGHOST`，以及声明自己在运行的执行器。它们在每次发送前必须
  **全部**解析成功，否则这次发送被拒绝——解析失败意味着拼不出完整禁区，而“解析不了就
  跳过”会让指向同一台主机的别名合法地漏过去。可选来源（兼容别名、本次部署没有启动的
  服务）解析不出来是正常的，跳过。

  执行器（`worker`）属于**部署可选**的服务：`make up` 只起平台本体，它由显式命令启动，
  验证流程在进程内调用同一份执行内核（`claim_job`／`execute_claim`），干净克隆不必运行
  它。因此它是部署声明而不是无条件基线——但放宽只落在“未声明就不要求名字解析”上：只要
  执行器进程在跑，声明就必须在，见 `app/worker.py` 的启动自查。

普通项目的目标白名单（执行池 `allowed_targets`）与这里无关：把禁区地址填进白名单也不会
被放行。
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

# 任何情况下都不得作为被测目标的地址段：未指定、环回、链路本地与云元数据。
# 这些地址指向执行器自己或运行它的宿主，而不是“另一个待测系统”：连上去等于让用例
# 打到平台自身的回环接口。私网段**不在**这里——公司内网的显式授权目标必须仍可用。
_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)

_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "metadata"}

# 容器到**实际宿主机**的入口名。部署模板（compose.yaml）用
# ``extra_hosts: host.docker.internal:host-gateway`` 提供它：`host-gateway` 在 Linux 与
# Docker Desktop 上都解析到宿主自己，所以这个入口在两种平台上都可靠，不需要用户去找
# 宿主 IP。名字只有一个定义处，下面的静态名单与必需名单用的是同一个常量。
_HOST_ENTRY = "host.docker.internal"

# 平台基础设施主机名的基线名单：当前 Compose 里的服务名（管理 API、数据库、前端、
# 执行器）与容器到宿主机的入口，外加各平台／运行时的兼容别名。名字由可信部署配置
# 定义，不是页面上的字符串黑名单：目标白名单覆盖不了这里，部署配置也只能追加。
#
# 这一层**完全静态**：不解析，也不依赖解析是否成功。兼容别名在当前部署里是否真的存在
# 与本层无关——把它们写成目标，在任何 DNS 状态下都由主机名层直接拒绝；反过来也不能
# 要求它们必须存在，否则缺这些名字的平台上连干净安装都跑不起来。
_BASELINE_PLATFORM_HOSTS = (
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    _HOST_ENTRY,
    "host.containers.internal",
    "gateway.docker.internal",
    "api",
    "db",
    "web",
    "worker",
)

# 当前部署**必须完整确认地址**的基线来源：管理 API、数据库与容器到宿主机的入口。
# 它们不是“可选名字”——解析不出来就证明不了禁区完整，宁可拒绝发送。这三项随平台本体
# 与部署模板一起存在，不能由部署配置删减；部署只能再追加自己环境里已启用的来源
# （前端、正在运行的执行器等），见 `Settings.platform_required_hosts`。
#
# 宿主入口为什么必须**默认**就在这份名单里：它当前指向的地址就是宿主自己的地址，
# 而宿主既跑着平台本体、又常常是公司内网的入口。只把它当“可选别名”，在解析不出来时
# 跳过（Linux 上的容器默认就没有这个入口），指向宿主的允许域名或直接写宿主 IP 的两条
# 路径就都没有地址层的拦截，只剩主机名层——而主机名层只认名字，换个域名就绕过去了。
# 所以模板无条件提供这个入口（见 compose.yaml 的 extra_hosts），守卫无条件要求它可确认；
# 自定义部署若提供不了等价入口，发送会被拒绝，而不是悄悄少一层保护。
_BASELINE_REQUIRED_HOSTS = ("api", "db", _HOST_ENTRY)
# 执行器（`worker`）**不在**基线里：它是部署可选的服务，`make up` 不启动它，干净克隆的
# 验证流程也不运行它，把一个当前部署没有的名字要求成“必须解析成功”会让所有发送被拒。
# 反过来，运行执行器的部署必须显式声明它（`PLATFORM_REQUIRED_HOSTS`），并且执行器进程
# 启动时会自查这份声明——所以不存在“跑着执行器却没人要求确认它”的部署。
# 前端 `web` 同理属于部署声明，不在这里。

# IPv4 映射 IPv6 的根：`::ffff:0:0/96`。它与 IPv4 地址空间是同一批主机。
_MAPPED_V4_ROOT = ipaddress.ip_network("::ffff:0:0/96")
_MAPPED_V4_BASE = int(_MAPPED_V4_ROOT.network_address)

# 主机名里不允许出现的字符：配置声明的是单个名字，不是 URL、路径或“名字:端口”。
# 冒号尤其不能放过——``PLATFORM_FORBIDDEN_HOSTS=intranet-api:8000`` 这种写法若被当成
# 一个永远匹配不上的名字收下，就等于声明了却不生效（声明禁区是**放开方向**的失误）。
# IP 字面量（含 IPv6 的冒号）单独放行，它们本身就是合法的来源写法。
_HOSTNAME_FORBIDDEN_CHARS = "/@[]*?: \t"


class TargetPolicyError(ValueError):
    """目标不允许访问，属于策略拒绝而非网络失败。"""


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    origin: str
    url: str


def _host_key(host: str) -> str:
    """主机名的比较形式：小写、去尾点。``API.`` 与 ``api`` 是同一台主机。"""
    return host.strip().lower().rstrip(".")


def _mapped_v4_address(ip: ipaddress.IPv4Address) -> ipaddress.IPv6Address:
    return ipaddress.IPv6Address(_MAPPED_V4_BASE + int(ip))


def _mapped_v4_network(network: ipaddress.IPv4Network) -> ipaddress.IPv6Network:
    """IPv4 网段的映射写法：前缀加 96，网络地址整体上移。

    ``10.9.9.0/24`` 与 ``::ffff:10.9.9.0/120`` 覆盖的是同一批主机。
    """
    return ipaddress.IPv6Network(
        (_MAPPED_V4_BASE + int(network.network_address), network.prefixlen + 96)
    )


def _unmapped_v4_network(network: ipaddress.IPv6Network) -> ipaddress.IPv4Network | None:
    """映射网段对应的 IPv4 网段；整段不在映射根里的返回 None。"""
    if network.prefixlen < _MAPPED_V4_ROOT.prefixlen or not network.subnet_of(_MAPPED_V4_ROOT):
        return None
    return ipaddress.IPv4Network(
        (int(network.network_address) - _MAPPED_V4_BASE, network.prefixlen - 96)
    )


def _equivalents(
    value: ipaddress.IPv4Address
    | ipaddress.IPv6Address
    | ipaddress.IPv4Network
    | ipaddress.IPv6Network,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address | ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """一个地址／网段的全部等价形式：它自己，以及跨 IPv4 ↔ 映射 IPv6 的那一种写法。

    两个版本是同一台主机的两种写法，但在 Python 里是不同的对象、版本也不同：直接比较
    只会得到 False，于是“声明的网段写映射形式、目标写普通形式”（或反过来）就能绕过禁区。

    等价语义只有这一处：地址（`_address_forms`，含解析出来的基础设施地址）与部署配置里
    的网段（`_parse_networks`）都经过它规范化，换任何一种写法都落到同一个判断上。
    """
    if isinstance(value, ipaddress.IPv4Network):
        return [value, _mapped_v4_network(value)]
    if isinstance(value, ipaddress.IPv6Network):
        unmapped = _unmapped_v4_network(value)
        return [value] if unmapped is None else [value, unmapped]
    if isinstance(value, ipaddress.IPv4Address):
        return [value, _mapped_v4_address(value)]
    mapped = value.ipv4_mapped
    return [value] if mapped is None else [value, mapped]


def parse_target(url: str) -> Target:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise TargetPolicyError(f"仅支持 http/https 目标，收到 {parts.scheme or '空'} 协议")
    if not parts.hostname:
        raise TargetPolicyError("目标地址缺少主机名")
    host = _host_key(parts.hostname)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    origin = f"{parts.scheme}://{host}:{port}"
    return Target(scheme=parts.scheme, host=host, port=port, origin=origin, url=url)


def normalize_origin(raw: str) -> str:
    """把白名单条目规范化为 scheme://host:port；非法条目直接报配置错误。"""
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TargetPolicyError(f"白名单条目无效：{raw}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{_host_key(parts.hostname)}:{port}"


def _literal_address(host: str) -> str | None:
    """主机名本身就是 IP 字面量时返回其规范写法，否则 None。"""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _checked_host_keys(hosts: Iterable[str]) -> frozenset[str]:
    """主机名名单：去空白、规范化，并做形态检查。

    “必需来源”少确认一个等于把禁区悄悄缩小，所以写错的条目按配置错误报出，而不是
    当成一个永远匹配不上的名字忽略过去。检查只看形态（单个主机名或 IP 字面量，不是
    URL、路径或“名字:端口”），**不查 DNS**：能否解析要到发送前一刻才知道，配置加载
    不该依赖网络，也不该靠“试发一个请求”来验证。
    """
    keys = set()
    for item in hosts:
        text = item.strip()
        if not text:
            continue
        key = _host_key(text)
        if _literal_address(key) is None and any(ch in key for ch in _HOSTNAME_FORBIDDEN_CHARS):
            raise TargetPolicyError(
                f"平台基础设施来源配置无效：{item}（应写成单个主机名，不带协议、路径或端口）"
            )
        keys.add(key)
    return frozenset(keys)


def _address_forms(address: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """一个地址的全部等价形式，用于禁段判断。

    IPv4 映射 IPv6 地址（``::ffff:169.254.169.254``）与对应的 IPv4 地址是同一个
    主机，但它们是不同的 Python 对象、版本也不同：直接与另一版本的网段比较只会得到
    False。若只按原样比较，改写成映射形式就能绕过全部禁段。两个方向都由
    `_equivalents` 统一展开——声明的网段写成映射形式、目标写成普通形式同样成立。

    带 scope id 的链路本地地址（``fe80::1%eth0``）先去掉 scope 再解析：它仍是同一个
    链路本地地址，原样解析只会抛 ValueError。解析不出来的形式返回空列表，由调用方
    按“无法证明安全”处理。
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return []
    return [
        form
        for form in _equivalents(ip)
        if isinstance(form, (ipaddress.IPv4Address, ipaddress.IPv6Address))
    ]


def _forbidden_address(
    address: str,
    extra_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> bool:
    """地址是否落在禁区：默认禁段，外加调用方追加的网段（部署配置与平台地址）。

    解析不出来的地址按**拒绝**处理：证明不了它安全，就不能连接。
    """
    forms = _address_forms(address)
    if not forms:
        return True
    networks = _FORBIDDEN_NETWORKS + tuple(extra_networks)
    return any(ip in network for ip in forms for network in networks)


def _host_networks(addresses: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """把地址列表变成单主机网段，供 `_forbidden_address` 作为追加禁段使用。

    每个地址的**全部等价写法**都进去：只按原样记一条，目标用另一种写法连同一台主机
    就查不到了。
    """
    networks = []
    for address in addresses:
        networks.extend(
            ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}") for ip in _address_forms(address)
        )
    return tuple(networks)


def _parse_networks(
    entries: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """把部署配置里的网段／地址解析成网络对象；非法条目按配置错误报出。

    每一条都展开成全部等价形式（`_equivalents`）：声明写成 ``::ffff:10.9.9.0/120``
    与写成 ``10.9.9.0/24`` 必须挡住同一批目标，单地址的 ``/128`` 同理。
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries:
        text = entry.strip()
        if not text:
            continue
        try:
            parsed = ipaddress.ip_network(text, strict=False)
        except ValueError as error:
            raise TargetPolicyError(f"平台禁区条目无效：{entry}") from error
        networks.extend(
            form
            for form in _equivalents(parsed)
            if isinstance(form, (ipaddress.IPv4Network, ipaddress.IPv6Network))
        )
    return tuple(networks)


def resolve_addresses(host: str) -> list[str]:
    """解析主机名到全部地址；解析失败按策略错误处理，不静默放行。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise TargetPolicyError(f"目标主机无法解析：{host}") from error
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise TargetPolicyError(f"目标主机无法解析：{host}")
    return addresses


class TargetGuard:
    """按允许来源集合校验目标，并给出可用于固定连接的真实地址。"""

    def __init__(
        self,
        allowed_origins: list[str],
        allow_production: bool = False,
        *,
        platform_hosts: Iterable[str] = (),
        platform_addresses: Iterable[str] = (),
        required_hosts: Iterable[str] = (),
    ) -> None:
        self._allowed = {normalize_origin(item) for item in allowed_origins}
        self._allow_production = allow_production
        # 显式部署声明的保护来源（`PLATFORM_FORBIDDEN_HOSTS`）同时也是**必须完整确认**
        # 的来源：声明了却不确认，等于用一条确认不了的名字假装禁区已经算全。
        declared_hosts = _checked_host_keys(platform_hosts)
        # 必需来源 = 代码基线的平台本体 ∪ 部署声明 ∪ 部署配置里列为必需的来源。
        # 基线两项删不掉：把 api／db 当成“默认可选名字”就等于允许它们在解析失败时
        # 静默退出禁区。执行器属于部署可选服务，由部署声明（见模块文档）。
        self._required_hosts = frozenset(
            (*_BASELINE_REQUIRED_HOSTS, *declared_hosts, *_checked_host_keys(required_hosts))
        )
        # 平台基础设施的名字：基线 ∪ 必需来源，只增不减。必需来源里可能有基线不认识
        # 的名字（实际 PGHOST、部署自己的入口、正在运行的执行器），它们同样不许作为被测目标。
        self._platform_hosts = frozenset(
            _host_key(item) for item in (*_BASELINE_PLATFORM_HOSTS, *self._required_hosts)
        )
        # 部署自己声明的禁区网段（例如平台所在的私网段）。
        self._declared_networks = _parse_networks(platform_addresses)
        self._resolved_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None = None

    @property
    def allowed_origins(self) -> list[str]:
        return sorted(self._allowed)

    @property
    def required_hosts(self) -> list[str]:
        """本次部署必须完整确认的基础设施来源（去重后的展示形式）。"""
        return sorted(self._required_hosts)

    def requires(self, host: str) -> bool:
        """该名字是否属于本部署必须完整确认的来源（按主机名的比较规则）。

        给部署侧的自查用：执行器要确认“本部署声明了自己”，比对的口径必须与守卫
        组装必需来源时完全一致，否则自查通过而发送前仍按另一份名单判断。
        """
        return _host_key(host) in self._required_hosts

    def ensure_infrastructure_confirmed(self) -> None:
        """确认本部署的地址层禁区**现在**就是完整的；不完整则抛策略错误。

        给进程启动时的自查用：发送准入本来就会在每次发送前确认一次，但在启动时先确认
        一次能把“这个部署提供不出必需来源”暴露成启动失败，而不是等第一条运行被拒。
        两者用的是同一段判断（`_infrastructure_networks`），不会出现启动自查通过而
        发送前按另一套规则判断的情况。

        只看名字解析，不向任何来源发请求；不做任何缓存写入以外的副作用。
        """
        self._infrastructure_networks()

    def _infrastructure_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """平台基础设施的名字当前解析到的地址，每个按单主机网段处理。

        “直接写 IP”绕不过主机名检查：别名指向的地址同样是禁区。地址随容器重建变化，
        所以在发送前一刻取用、并按守卫实例缓存一次。守卫是每次运行新建的，缓存不跨
        运行复用，也就不存在“拿着过期结果批准请求”。

        **必需来源必须全部确认**：任一必需来源解析失败时整体拒绝，并且**不缓存**——
        用失败或部分结果拼出来的禁区会合法地漏掉那台主机当前指向的地址，等于“解析
        不了就放行”。下一次发送会重新完整确认，不会在同一个守卫上留下半个结果。

        可选来源（跨平台兼容别名）解析不出来是正常情况：跳过即可，名字本身仍由主机名
        层静态拒绝，这里补的只是“改用 IP 直连”这条等价路径。
        """
        if self._resolved_networks is None:
            resolved: list[str] = []
            unconfirmed: list[str] = []
            for host in sorted(self._platform_hosts):
                try:
                    resolved.extend(resolve_addresses(host))
                except TargetPolicyError:
                    if host in self._required_hosts:
                        unconfirmed.append(host)
            if unconfirmed:
                raise TargetPolicyError(
                    "平台基础设施来源无法确认："
                    + "、".join(unconfirmed)
                    + " 当前解析不出地址；无法证明禁区完整（可能有别名指向这些地址），"
                    "已阻止本次发送。"
                )
            self._resolved_networks = _host_networks(resolved)
        return self._resolved_networks

    def _reject_forbidden_host(self, host: str) -> None:
        """主机名层的禁区判断：完全静态，不做 DNS，也不依赖解析结果。"""
        if host in _METADATA_HOSTS:
            raise TargetPolicyError("云元数据地址不允许作为被测目标")
        if host in self._platform_hosts:
            raise TargetPolicyError(
                f"目标 {host} 是平台基础设施（管理 API／数据库／执行器宿主机入口），"
                "不允许作为被测目标"
            )
        literal = _literal_address(host)
        if literal is not None and _forbidden_address(literal, self._declared_networks):
            raise TargetPolicyError(f"目标 {host} 位于不允许的地址段，不允许作为被测目标")

    def authorize_url(self, url: str) -> Target:
        """校验单个 URL 是否允许访问。

        **不解析 DNS**：解析会随网络与 DNS 可用性波动，创建运行不应因此被拒；真正的
        解析与地址固定必须发生在发送前一刻，否则既挡不住 DNS 改绑，也分不清“网络
        失败”与“策略拒绝”。
        """
        target = parse_target(url)
        self._reject_forbidden_host(target.host)
        if target.origin not in self._allowed:
            raise TargetPolicyError(
                f"目标 {target.origin} 不在允许来源白名单内，已阻止执行。"
            )
        return target

    def pinned_address(self, target: Target) -> str:
        """解析并校验最终连接地址，防止 DNS 改绑到禁段或平台基础设施。"""
        addresses = resolve_addresses(target.host)
        extra = self._declared_networks + self._infrastructure_networks()
        for address in addresses:
            if _forbidden_address(address):
                raise TargetPolicyError(
                    f"目标 {target.host} 解析到不允许的地址段，已阻止执行。"
                )
            # 与上一条分开判断只为了给出准确的原因：追加网段里既有部署声明的禁区，
            # 也有平台基础设施名字当前指向的地址，两类都是“平台自己”。
            if _forbidden_address(address, extra):
                raise TargetPolicyError(
                    f"目标 {target.host} 解析到平台基础设施地址，已阻止执行。"
                )
        return addresses[0]

    def check_environment(self, kind: str) -> None:
        """生产环境在本阶段统一拒绝，不因环境标签放开。"""
        if kind == "production" and not self._allow_production:
            raise TargetPolicyError("本阶段不允许对生产环境执行，请使用测试环境。")
