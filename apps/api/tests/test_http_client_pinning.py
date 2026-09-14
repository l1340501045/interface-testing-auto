"""固定连接地址的传输层：没有固定地址时必须拒绝，不得退回普通 DNS 连接。

准入阶段按实际要发送的 URL 解析并校验了连接地址；若传输层在该主机查不到固定地址，
就说明“校验的”和“要连的”已经不是同一台主机（环境在准备与发送之间被改到别处就会
这样）。此时原样放行等于静默走一次普通 DNS 解析——地址校验在这一步被整体绕过。

纯方法测试：不连接数据库，也不发起任何真实网络请求。
"""
from __future__ import annotations

import logging
import socket
from urllib.parse import quote_plus, unquote, unquote_plus

import httpx
import pytest

from app.kernel.http_client import PinnedAddressMissing, build_client


def _forbid_any_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何 DNS 解析都直接判失败：这条路径根本不该去解析主机名。"""

    def explode(*_args: object, **_kwargs: object):
        raise AssertionError("缺少固定地址时不得回退普通 DNS 解析")

    monkeypatch.setattr(socket, "getaddrinfo", explode)


def test_request_without_a_pinned_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定地址表里没有这台主机：直接拒绝，连解析都不尝试。"""
    _forbid_any_resolution(monkeypatch)
    with build_client({}, 1.0, 1.0) as client:
        with pytest.raises(PinnedAddressMissing):
            client.get("http://unpinned-host.invalid:8080/echo")


def test_pin_keyed_by_another_host_does_not_cover_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定地址是按主机名取用的：别的主机的地址不能替这台主机放行。

    这正是“按重读环境重算目标”造成的形态——固定地址是 B 的，请求却发往 A。
    """
    _forbid_any_resolution(monkeypatch)
    with build_client({"other-host": "10.0.0.9"}, 1.0, 1.0) as client:
        with pytest.raises(PinnedAddressMissing):
            client.get("http://echo:8080/echo")


def test_pinned_request_rewrites_the_connection_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有固定地址时，连接地址被改写为该 IP，原主机名只留在 Host 与 SNI 上。

    用一个假的传输基类截住真正发出的请求，既不联网也能看到改写结果。
    """
    seen: list[httpx.Request] = []

    def fake_handle(self, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    with build_client({"echo": "10.0.0.5"}, 1.0, 1.0) as client:
        response = client.get("http://echo:8080/echo")

    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].url.host == "10.0.0.5", "连接地址应改写为已校验的 IP"
    assert seen[0].headers["host"] == "echo:8080", "Host 头保留原主机名"
    assert seen[0].extensions.get("sni_hostname") == "echo", "TLS SNI 保留原主机名"


def test_request_line_with_credentials_is_not_written_to_the_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """传输库不得把带查询参数的请求行写进日志：那是引擎脱敏之外的一条凭据出口。

    worker 启动时把根日志开到 INFO，httpx 于是逐条记录 `HTTP Request: GET <完整 URL>`
    （httpcore 的 DEBUG 追踪里也有同一个 Request）。查询参数认证值到运行时就在
    `request.url` 里，而这一行不经过引擎的任何脱敏：日志里的 `a%2Bb%2Fc%3D+d+e` 用
    表单解码一解就是凭据原文，报告里再干净也没用。

    断言看的是**日志里到底留下了什么**，不是某个 logger 的级别取值：用假传输截住真实
    发送，把根日志开到 DEBUG（比 worker 的 INFO 更宽），再看凭据能不能从日志里解回来。
    """
    secret = "a+b/c= d e"

    def fake_handle(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    with caplog.at_level(logging.DEBUG):
        with build_client({"echo": "10.0.0.5"}, 1.0, 1.0) as client:
            response = client.get(f"http://echo:8080/echo?api_key={quote_plus(secret)}")

    assert response.status_code == 200, "这条用例必须先真的走到发送"
    logged = "\n".join(
        f"{record.getMessage()} | {record.args!r}" for record in caplog.records
    )
    for decoded in (unquote(logged), unquote_plus(logged)):
        assert secret not in decoded, f"日志里留下了可解码还原的凭据：{logged}"
