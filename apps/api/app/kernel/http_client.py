"""受控 HTTP 客户端：固定连接地址、禁用重定向与环境代理。

目标已在发送前通过白名单校验；这里再把连接地址固定为校验过的 IP，避免
校验与实际连接之间发生 DNS 改绑。重定向默认关闭，跟随重定向会让请求落到
未经校验的目标；环境变量代理（HTTP_PROXY 等）同样关闭，防止被绕过。
"""
from __future__ import annotations

import logging

import httpx


def _silence_transport_logging() -> None:
    """把传输库的日志压到 WARNING：请求行本身就是一条凭据出口。

    查询参数认证是按用户配置注入的，凭据到运行时就在 `request.url` 里。httpx 在
    INFO 级别逐条记录 `HTTP Request: GET <完整 URL> "200 OK"`——引擎自己的证据都
    经过脱敏，传输库这一行却绕过了全部脱敏：日志里的 `a%2Bb%2Fc%3D+d+e` 用
    `unquote` 一解就是凭据原文。请求行的排障价值远低于凭据泄露的代价，因此在这里
    关掉，而不是去维护一条“猜日志里秘密会怎么编码”的替换分支。

    放在构造客户端处而不是进程启动处：发送是唯一会产生这行日志的动作，任何调用
    方（worker、试算、测试）都必然经过这里。
    """
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class PinnedAddressMissing(RuntimeError):
    """请求的主机没有对应的固定地址：宁可拒绝，也不能退回普通 DNS 连接。"""


class PinnedTransport(httpx.HTTPTransport):
    """把 host 重写为已校验的 IP，并保留原 Host 与 TLS SNI。"""

    def __init__(self, pin: dict[str, str], **kwargs) -> None:
        super().__init__(retries=0, **kwargs)
        self._pin = pin

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        address = self._pin.get(original_host)
        if address is None:
            # 找不到固定地址意味着“这次要连的主机没被校验过”。早先的实现会原样放行，
            # 于是静默退回普通 DNS 连接：准入阶段按重读后的环境重算目标与固定地址，
            # 而实际发送的仍是先前准备的那个 URL，两者主机不同时，固定地址根本没套用
            # 上，请求绕过了全部地址校验。缺固定地址是明确的内部矛盾，必须拒绝。
            raise PinnedAddressMissing(
                f"目标主机 {original_host!r} 没有可用的固定连接地址，已阻止发送。"
            )
        host_header = original_host
        if request.url.port not in (None, 80, 443):
            host_header = f"{original_host}:{request.url.port}"
        request.url = request.url.copy_with(host=address)
        request.headers["Host"] = host_header
        request.extensions = {**request.extensions, "sni_hostname": original_host}
        return super().handle_request(request)


def build_client(
    pin: dict[str, str],
    connect_timeout: float,
    read_timeout: float,
) -> httpx.Client:
    """构造禁代理、禁重定向、固定连接地址的同步客户端。"""
    _silence_transport_logging()
    return httpx.Client(
        transport=PinnedTransport(pin),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        ),
    )
