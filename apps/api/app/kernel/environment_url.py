"""环境基础地址的语法校验：保存入口与运行入口共用同一份规则。

基础地址是**拼到用例路径前面**的服务前缀（`https://service.example/api/v1`），
因此它必须是一个完整的 HTTP／HTTPS 服务地址。裸主机名或 `主机:端口` 看起来像地址，
却没有协议信息，拼出来的目标根本无法解析——过去这类值只做长度检查就能存进库，直到
执行时才失败，而失败又被归到“目标不在白名单”，把用户引向去修改本来正确的用例路径。
地址本身的问题必须在保存时就说清楚。

规则按显式解析写，不用一整条 URL 正则：

- 必须以 `http://` 或 `https://` 开头；**不猜协议**，绝不自动补 `http://`；
- 必须带主机名：域名（含内网短名与国际化域名）或 IPv4／方括号 IPv6 字面量；
- 端口必须是 1..65535 的十进制数字，缺数字的冒号同样不合法；
- 允许基础路径，**路径与其中的百分号编码原样保留**，不做 URL 规范化改写；
- 拒绝 userinfo、query、fragment、反斜杠、控制字符与内部空白：它们都不是能正确
  拼接的基础地址，查询串之类属于当前用例的请求参数；
- 只做语法判断：**不解析 DNS、不建立连接**，所以目标服务停着也能改配置。

主机名分三种情况判断，共同点是**解析成功不等于客户端支持**：

- 方括号里只能是 IPv6 字面量。`urlsplit` 允许 IPvFuture 并把 `[v1.echo]` 的主机名交给
  下一层，若落进域名分支就会被放行，而前端与真实 HTTP 客户端都拒绝它；
- 四段全数字的外形只可能是 IPv4；越界段（`256.256.256.256`）与前导零（`1.2.3.04`）
  一律拒绝，**不退回域名分支**——退回时纯数字标签能通过标签规则，等于把客户端根本发不
  出去的地址认证成合法；
- 域名标签允许非 ASCII 字母（国际化域名），长度与标签规则按去尾点后的主机名判断。

最后一道闸门是**真实发送客户端本身**（`httpx`，执行内核构造被测请求用的就是它）：它回答
“这份原文我能不能用”，并顺带覆盖国际化域名的 IDNA 合法性（全角字母、罗马数字等能通过
宽松标签规则、但 IDNA 不接受的主机）。这样就不必自造一套完整 URL 标准，也不必让两套规则
互相追平。闸门只构造对象，不建立连接、不解析 DNS，也不拿 HTTPX 规范化后的字符串替换原文。

错误信息**不回显原始地址**。地址常常是从浏览器或抓包工具里整段粘过来的，原文里可能
带着凭证、令牌或签名；把它放进错误消息再返回页面或写进日志，等于把这些值回显出去。
因此每条消息只说“哪一部分不合法”，不复述输入，连底层客户端抛出的原文消息也不透传。

运行入口对**历史值**不宽容：`check_base_url` 严格按库里的原文判断，既不补协议也不
悄悄用 trim 后的地址替换目标。存量里带多余空格的地址会被明确拒绝，用户重新保存一次
即可——这比按一个与库中不一致的地址发出去要好。
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import SplitResult, urlsplit

import httpx

_ALLOWED_SCHEMES = ("http", "https")
_SEPARATOR = "://"

_MIN_PORT = 1
_MAX_PORT = 65535

_MAX_HOST_LENGTH = 253
_MAX_LABEL_LENGTH = 63

# 主机标签：字母（含非 ASCII，供国际化域名）、数字与下划线，内部可含连字符。
# 用 `\w` 而不是 `[A-Za-z0-9_]`：国际化域名的标签是非 ASCII 字母，ASCII-only 规则会把
# 合法域名先拒掉；这些域名在目标策略里本来就作为来源被接受。下划线在真实内网名字里很
# 常见，而它本身没有安全含义——真正决定“能不能访问”的是执行池白名单。
_LABEL_PATTERN = re.compile(r"\w(?:[\w-]*\w)?")

# 错误文案里给用户照着填的示例地址。用保留域名 `service.example`（RFC 2606），不是任何真实
# 环境：示例会返回页面，也可能出现在日志里，不能指向真实主机。
ENVIRONMENT_URL_EXAMPLE = "https://service.example"


class EnvironmentUrlError(ValueError):
    """环境基础地址语法不合法，属于配置错误而非网络失败。"""


def _has_control_or_space(text: str) -> bool:
    """是否含空白或控制／格式字符；制表符、换行与零宽字符都会落在这里。"""
    return any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"} for char in text)


def _check_port(parts: SplitResult) -> None:
    """端口必须是 1..65535；``host:`` 这种缺数字的写法单独报出来。"""
    if parts.netloc.endswith(":"):
        raise EnvironmentUrlError("环境地址的端口号不完整：冒号后面缺少数字。")
    try:
        port = parts.port
    except ValueError as error:
        raise EnvironmentUrlError("环境地址的端口号必须是 1 到 65535 之间的数字。") from error
    if port is not None and not (_MIN_PORT <= port <= _MAX_PORT):
        raise EnvironmentUrlError("环境地址的端口号必须是 1 到 65535 之间的数字。")


def _looks_like_ipv4(host: str) -> bool:
    """是否“四段全 ASCII 数字”的 IPv4 外形。

    只看外形，不看是否越界或有前导零：外形一旦成立，这个主机就只可能是 IPv4，不允许
    退回域名分支（退回时纯数字标签会通过标签规则，把客户端发不出去的地址放行）。
    非 ASCII 的数字不在此列，交给域名分支由 IDNA 判断。
    """
    parts = host.split(".")
    return len(parts) == 4 and all(part.isascii() and part.isdigit() for part in parts)


def _check_host(host: str, *, bracketed: bool) -> None:
    """主机名必须是 IPv6 字面量（方括号写法）、合法 IPv4，或形态合法的域名。"""
    if bracketed:
        # 方括号只能装 IP 字面量，且只支持 IPv6。这个分支在**接受方向**承重：IPv6 字面量
        # 含冒号，不做这一步就会掉进域名标签规则被误拒。IPvFuture 也从这里得到精确原因，
        # 否则 `urlsplit` 会把 `[v1.echo]` 的括号内容当普通主机名交下去。
        try:
            ipaddress.IPv6Address(host)
        except ValueError as error:
            raise EnvironmentUrlError(
                "环境地址的方括号里必须是 IPv6 地址，例如 http://[2001:db8::1]:8080。"
            ) from error
        return
    if _looks_like_ipv4(host):
        # 这一步为的是**可操作的原因**：同样是拒绝，客户端兼容闸门只能说“无法被客户端解析”，
        # 用户看不出该改哪一段。正确性由闸门兜底，这里给出具体到“越界／前导零”的说明。
        try:
            ipaddress.IPv4Address(host)
        except ValueError as error:
            raise EnvironmentUrlError(
                "环境地址的 IPv4 地址不合法：每段必须是 0 到 255，且不能有前导零。"
            ) from error
        return
    # 尾部单个点是根域写法，不占主机名长度；先去掉再判断长度与标签。
    name = host[:-1] if host.endswith(".") else host
    if not name:
        raise EnvironmentUrlError("环境地址缺少主机名。")
    if len(name) > _MAX_HOST_LENGTH:
        raise EnvironmentUrlError("环境地址的主机名过长。")
    for label in name.split("."):
        if not label or len(label) > _MAX_LABEL_LENGTH or not _LABEL_PATTERN.fullmatch(label):
            raise EnvironmentUrlError(
                "环境地址的主机名不合法：只能写域名（含内网短名与国际化域名）或 IP 地址。"
            )


def _check_client_support(raw: str) -> None:
    """确认真实发送客户端能构造这份原文。

    HTTPX 就是执行内核把被测请求发出去时用的客户端。`urlsplit` 能拆开的地址不等于它支持：
    越界 IPv4、方括号里的 IPvFuture、以及通过宽松标签规则但 IDNA 不接受的主机（全角字母、
    罗马数字）都是“能拆开、构造请求时就抛错”。与其自造一套完整 URL 标准、让两套规则互相
    追平，不如让真正要发请求的那一层回答“这份原文我能不能用”。

    只构造对象：不建立连接、不解析 DNS，也不拿 HTTPX 规范化后的字符串替换原文（基础路径
    与百分号编码必须原样保留）。**不透传底层消息**——HTTPX 的原始消息里带着主机名，而
    错误信息会回到页面并可能写进日志。
    """
    try:
        httpx.URL(raw)
    except (httpx.InvalidURL, UnicodeError, ValueError) as error:
        raise EnvironmentUrlError(
            "环境地址无法被 HTTP 客户端解析：主机名或端口不是受支持的写法。"
        ) from error


def check_base_url(raw: str) -> None:
    """按**原文**校验环境基础地址；不合法时抛 `EnvironmentUrlError`。

    调用方决定要不要先去掉首尾空格：保存入口走 `normalize_base_url`，运行入口直接
    用本函数判断库里的存量值，避免用一个与库中不一致的地址发请求。
    """
    if _SEPARATOR not in raw:
        raise EnvironmentUrlError(
            "环境地址必须以 http:// 或 https:// 开头，不能只写主机名或“主机:端口”。"
            f"例如 {ENVIRONMENT_URL_EXAMPLE}。"
        )
    if _has_control_or_space(raw):
        raise EnvironmentUrlError("环境地址不能包含空格、制表符、换行或其他控制字符。")
    if "\\" in raw:
        raise EnvironmentUrlError("环境地址不能包含反斜杠；路径分隔符请使用正斜杠。")
    if "?" in raw:
        raise EnvironmentUrlError("环境地址不能带查询参数，请把查询参数写在用例的请求参数里。")
    if "#" in raw:
        raise EnvironmentUrlError("环境地址不能带 # 片段。")

    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
    except ValueError as error:
        raise EnvironmentUrlError("环境地址格式不合法，无法解析。") from error

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise EnvironmentUrlError("环境地址只支持 http 或 https 协议，其他协议不会被发送。")
    if "@" in parts.netloc:
        raise EnvironmentUrlError(
            "环境地址不能包含账号信息（账号:口令@主机），凭证请在身份配置里维护。"
        )
    if not hostname:
        raise EnvironmentUrlError(f"环境地址缺少主机名，例如 {ENVIRONMENT_URL_EXAMPLE}。")
    _check_port(parts)
    # 方括号是“主机是 IP 字面量”的写法，按它区分分支；括号不平衡的地址 `urlsplit` 已拒绝。
    _check_host(hostname, bracketed=parts.netloc.startswith("["))
    _check_client_support(raw)


def normalize_base_url(raw: str) -> str:
    """保存入口：去掉首尾普通空格后校验，返回**将写入库中的同一份文本**。

    只 trim 首尾空格，不补协议、不改大小写、不重写路径与百分号编码：基础路径是用户
    配置的一部分，被“规范化”改掉会让请求打到别的地方。
    """
    text = raw.strip(" ")
    check_base_url(text)
    return text


__all__ = [
    "ENVIRONMENT_URL_EXAMPLE",
    "EnvironmentUrlError",
    "check_base_url",
    "normalize_base_url",
]
