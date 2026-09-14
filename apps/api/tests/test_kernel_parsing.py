"""cURL 解析与变量解析测试。"""
from __future__ import annotations

import uuid

import pytest

from app.kernel.curl_parser import CurlParseError, parse
from app.kernel.valueliteral import ValueLiteral
from app.kernel.variables import VariableResolutionError, VariableResolver


def test_curl_parse_basic_get() -> None:
    draft = parse("curl http://echo:8080/api/items")
    assert draft.method == "GET"
    assert draft.host == "echo:8080"
    assert draft.path == "/api/items"


def test_curl_parse_repeated_query_and_headers() -> None:
    text = (
        "curl -X POST 'http://echo:8080/x?a=1&a=2' "
        "-H 'Content-Type: application/json' "
        "-H 'X-Trace: 123' "
        "-d '{\"id\": 9007199254740993}'"
    )
    draft = parse(text)
    assert draft.method == "POST"
    assert [q["name"] for q in draft.query_params] == ["a", "a"]
    assert draft.body_type == "json"
    assert "9007199254740993" in draft.body


def test_curl_parse_data_implies_post() -> None:
    draft = parse("curl http://echo:8080/submit -d 'name=x'")
    assert draft.method == "POST"
    assert draft.body == "name=x"


def test_curl_file_field_warns_not_silent() -> None:
    draft = parse("curl -F 'file=@/tmp/x.txt' http://echo:8080/upload")
    assert any("文件字段" in w for w in draft.warnings)


def test_curl_non_http_rejected() -> None:
    with pytest.raises(CurlParseError):
        parse("curl ftp://host/file")


def test_curl_basic_auth_hint() -> None:
    draft = parse("curl -u user:pass http://echo:8080/")
    assert draft.auth_hint is not None
    assert draft.auth_hint["type"] == "basic"


# —— review-curl-01.md 边界回归 ——


def test_explicit_get_with_data_stays_get() -> None:
    """显式 -X GET 与 -d 同时出现时，必须保留用户的 GET，不得改成 POST。"""
    draft = parse("curl -X GET -d 'a=1' https://example.test/path")
    assert draft.method == "GET", "显式方法不得被 -d 覆盖"
    assert draft.body == "a=1"


def test_implicit_method_promoted_to_post_by_data() -> None:
    draft = parse("curl https://example.test/path -d 'a=1'")
    assert draft.method == "POST", "未显式指定方法时 -d 隐含 POST"


def test_data_binary_file_reference_unsupported() -> None:
    """--data-binary @file 引用文件，首版不支持，且不可当作字面正文保存。"""
    draft = parse("curl https://example.test/path --data-binary @payload.json")
    assert draft.sendable is False
    assert any("引用文件" in item for item in draft.unsupported)
    assert draft.body == "", "文件引用不得被当作字面正文"


def test_data_raw_at_is_literal_text() -> None:
    """--data-raw 的 @text 是合法字面正文，与文件引用区分。"""
    draft = parse("curl https://example.test/path --data-raw @literal")
    assert draft.sendable is True
    assert draft.body == "@literal"


def test_url_option_consumed_as_url_not_positional() -> None:
    draft = parse("curl --url https://example.test/api --header 'X: 1'")
    assert draft.host == "example.test"
    assert draft.path == "/api"


def test_unknown_option_with_argument_rejected() -> None:
    """未知带参选项不得静默忽略，也不得把其参数当成 URL；直接拒绝导入。"""
    with pytest.raises(CurlParseError):
        parse("curl --frobnicate value https://example.test/real")


def test_multiple_urls_rejected() -> None:
    with pytest.raises(CurlParseError):
        parse("curl https://a.test/1 https://b.test/2")


def test_form_multipart_marked_unsupported_not_silent_get() -> None:
    """-F 不得被偷换成 GET 文本正文；应明确标记 multipart 不可发送。"""
    draft = parse("curl -F 'name=value' https://example.test/upload")
    assert draft.sendable is False
    assert any("multipart" in item for item in draft.unsupported)
    assert draft.method == "POST"
    assert draft.body_type != "json"


def test_sensitive_header_value_not_stored() -> None:
    """认证值只在本次运行生成；认证头不得落进普通草稿，也不得出现在草稿序列化结果里。

    合成值随运行生成、认证头与命令文本分开构造，源码里不留固定 token；拒泄露语义不变。
    """
    secret = f"it-curlparse-{uuid.uuid4().hex}"
    auth_header = f"Authorization: Bearer {secret}"
    draft = parse(f"curl -H '{auth_header}' https://example.test/")
    assert all(h["name"].lower() != "authorization" for h in draft.headers), "认证头不得落入普通草稿"
    assert draft.auth_hint is not None
    assert secret not in str(draft.to_dict())


def test_url_userinfo_not_stored() -> None:
    draft = parse("curl https://user:pw@example.test/")
    assert "pw" not in str(draft.to_dict())
    assert draft.auth_hint is not None
    assert draft.host == "example.test"


def test_unsupported_flag_marks_not_sendable() -> None:
    draft = parse("curl -L https://example.test/")
    assert draft.sendable is False
    assert any("location" in item.lower() or "-L" in item for item in draft.unsupported)


def test_cookie_option_not_stored_as_plaintext() -> None:
    draft = parse("curl -b 'session=abc123' https://example.test/")
    assert "abc123" not in str(draft.to_dict())
    assert draft.auth_hint is not None


def test_variable_resolution() -> None:
    resolver = VariableResolver({"id": ValueLiteral(type="number", text="9007199254740993")})
    assert resolver.resolve_text("order-{{id}}") == "order-9007199254740993"
    assert resolver.resolve_value("{{id}}") == __import__("decimal").Decimal("9007199254740993")


def test_variable_undefined_fails() -> None:
    resolver = VariableResolver({})
    with pytest.raises(VariableResolutionError):
        resolver.resolve_text("{{missing}}")


def test_variable_string_vs_number_preserved() -> None:
    resolver = VariableResolver({"s": ValueLiteral(type="string", text="0"), "n": ValueLiteral(type="number", text="0")})
    assert resolver.resolve_value("{{s}}") == "0"
    assert resolver.resolve_value("{{n}}") == __import__("decimal").Decimal("0")


def test_structured_resolution_keeps_the_type_of_a_whole_placeholder() -> None:
    """结构化正文里的整体占位保留类型；嵌入更长文本时仍是字符串。

    数字给的是无损数字节点，不是 Decimal：序列化必须按十进制原文写出，而
    `str(Decimal('1E+2'))` 是非法 JSON。
    """
    from app.kernel.lossless_json import NumberNode

    resolver = VariableResolver(
        {
            "n": ValueLiteral(type="number", text="1E+2"),
            "s": ValueLiteral(type="string", text="a"),
        }
    )
    assert resolver.resolve_structured("{{n}}") == NumberNode(text="100")
    # 嵌入更长文本时按既有文本语义写入变量的字面文本，不换类型也不改写法。
    assert resolver.resolve_structured(" x{{n}}") == " x1E+2"
    assert resolver.resolve_structured("{{s}}") == "a"
    assert resolver.resolve_structured("pre{{s}}") == "prea"
    with pytest.raises(VariableResolutionError):
        resolver.resolve_structured("{{missing}}")
