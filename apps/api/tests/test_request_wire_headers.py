"""请求头“可发送性”边界：无法按 HTTP 线上格式表示的头必须在发送前失败。

这些情况此前的实际行为都不是配置错误：非 ASCII 值让 httpx 抛
`UnicodeEncodeError`，它不是 `httpx.HTTPError`，会穿过执行器 `_send` 的网络错误
处理并让 worker 在执行中途崩溃；CR／LF／NUL 由 httpx 原样并入请求头，等到 h11
组包才报错，被归类为“网络中断、副作用不明”，用户只是写错一个字符却得到
“结果不明，需人工确认”。两者都在这里锁死为发送前的配置错误。

纯方法测试：不连接数据库、不发送请求。
"""
from __future__ import annotations

import re

import pytest

from app.kernel.request_spec import RequestSpecError, prepare
from app.kernel.variables import VariableResolver

_BASE = "http://echo:8080"


def _spec(**overrides: object) -> dict:
    spec = {
        "method": "GET",
        "path": "/echo",
        "query_params": [],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    spec.update(overrides)
    return spec


def _prepare(**overrides: object) -> object:
    return prepare(_spec(**overrides), _BASE, VariableResolver({}))


# —— 值：非 ASCII 是原始故障，latin-1 能编码的字符同样会崩 ——


def test_chinese_header_value_rejected_before_send() -> None:
    """认证注入的中文秘密值就是本用例的真实来源。"""
    with pytest.raises(RequestSpecError, match="无法按 HTTP 头传输"):
        _prepare(headers=[{"name": "X-Demo-Token", "value": "demo-token-7f3a91c0-本机集成测试"}])


def test_latin1_only_value_rejected_too() -> None:
    """latin-1 理论上限（obs-text）不是 httpx 的实际契约，放行等于留着同一个崩溃。"""
    with pytest.raises(RequestSpecError, match="无法按 HTTP 头传输"):
        _prepare(headers=[{"name": "X-Tag", "value": "café"}])


# —— 值：ASCII 控制字符会被拆头或截断 ——


@pytest.mark.parametrize("value", ["a\r\nX-Injected: 1", "a\nX-Injected: 1", "a\rb", "a\x00b"])
def test_control_chars_in_header_value_rejected(value: str) -> None:
    with pytest.raises(RequestSpecError, match="无法按 HTTP 头传输"):
        _prepare(headers=[{"name": "X-Ok", "value": value}])


# —— 名称：必须是 token ——


@pytest.mark.parametrize("name", ["X-Bad\r\nX-Injected", "X Bad", "X:Bad", "X-奥"])
def test_invalid_header_name_rejected(name: str) -> None:
    with pytest.raises(RequestSpecError, match="不是合法的字段名"):
        _prepare(headers=[{"name": name, "value": "v"}])


# —— 注入头走同一道校验，不能只查用户填写的头 ——


def test_injected_header_value_is_validated() -> None:
    with pytest.raises(RequestSpecError, match="无法按 HTTP 头传输"):
        prepare(_spec(), _BASE, VariableResolver({}), injected_headers=[("X-Demo-Token", "含中文")])


def test_injected_header_name_is_validated() -> None:
    with pytest.raises(RequestSpecError, match="不是合法的字段名"):
        prepare(_spec(), _BASE, VariableResolver({}), injected_headers=[("X Bad", "v")])


# —— 校验失败的错误信息不得回显值：它会被写进报告与数据库 ——


def test_rejected_value_is_not_echoed_in_the_error() -> None:
    """注入值触发传输校验时，错误信息里出现值就等于把秘密写进了报告。

    认证秘密完全可能是中文等无法按 HTTP 头传输的内容（创建时不限字符集，运行时才
    会撞上这条边界）。公开的错误信息必须只给位置、数量和可操作说明，不给出值本身。
    """
    secret = "壹贰叁-0f3a-陆柒捌"
    with pytest.raises(RequestSpecError) as error:
        prepare(
            _spec(),
            _BASE,
            VariableResolver({}),
            injected_headers=[("X-Demo-Token", secret)],
        )
    message = str(error.value)
    assert secret not in message
    assert secret[:4] not in message, "开头片段同样不得回显"
    assert secret[-4:] not in message, "结尾片段同样不得回显"
    # 仍然可操作：指出是哪个头、有几个非法字符、首个非法码位在哪里。
    illegal_count = sum(
        1 for char in secret if char != "\t" and not (" " <= char <= "~")
    )
    assert "X-Demo-Token" in message
    assert f"{illegal_count} 个" in message
    assert re.search(r"U\+[0-9A-F]{4}", message), message
    assert "不回显" in message


def test_rejected_value_message_still_names_the_position() -> None:
    """用户填写的头同样不回显值，但仍要能定位到是哪一个头。"""
    with pytest.raises(RequestSpecError) as error:
        _prepare(headers=[{"name": "X-Trace", "value": "壹贰叁"}])
    message = str(error.value)
    assert "X-Trace" in message
    assert "壹贰叁" not in message


# —— 相邻允许边界：不能因为收紧而误伤正常请求 ——


def test_ascii_value_and_tab_still_allowed() -> None:
    prepared = _prepare(headers=[{"name": "X-Trace", "value": "a\tb c~!@#"}])
    assert ("X-Trace", "a\tb c~!@#") in prepared.headers


def test_chinese_in_json_body_is_not_a_header_problem() -> None:
    """正文按 UTF-8 承载中文是正常用法，收紧只针对请求头。"""
    prepared = _prepare(body_type="json", body='{"名称":"中文值"}')
    assert prepared.content is not None
    assert "中文值" in prepared.raw_body
