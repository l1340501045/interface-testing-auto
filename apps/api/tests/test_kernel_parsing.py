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


# —— 多行 cURL 续行：浏览器复制出来的命令按反斜杠＋换行续接 ——
#
# 单行与多行是同一个命令的两种写法，必须得到同一份请求定义。等价单行由同一组
# token 连接而成，断言整份草稿相等，避免只对比个别字段而漏掉被续行破坏的部分。


def _browser_header_lines(token: str, session: str) -> list[str]:
    """脱敏的浏览器请求头：普通头与认证头混排在真实顺序里。

    认证值随运行生成、与命令文本分开构造，源码里不留固定凭据形状。
    """
    return [
        "Accept: application/json",
        "Accept-Language: zh-CN,zh;q=0.9",
        "Origin: https://example.test",
        "Referer: https://example.test/orders/list",
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        'sec-ch-ua: "Chromium";v="140", "Not=A?Brand";v="24"',
        "sec-fetch-site: same-origin",
        f"Authorization: Bearer {token}",
        f"Cookie: it_session={session}; it_locale=zh-CN",
    ]


def _single_line_curl(url: str, headers: list[str]) -> str:
    return " ".join([f"curl '{url}'", *(f"-H '{header}'" for header in headers)])


def _multiline_curl(url: str, headers: list[str], newline: str = "\n") -> str:
    """每行以反斜杠续接、下一行带缩进，与浏览器复制出来的形状一致。"""
    lines = [f"curl '{url}'"]
    lines.extend(f"      -H '{header}'" for header in headers)
    return (" \\" + newline).join(lines)


def _browser_curl_sample(newline: str = "\n") -> tuple[str, str, str]:
    """返回 (等价的单行、多行、认证 token)。"""
    token = f"it-curlparse-{uuid.uuid4().hex}"
    session = uuid.uuid4().hex
    url = "https://example.test/api/orders?page=1&size=20&sort=desc&filter=open"
    headers = _browser_header_lines(token, session)
    return _single_line_curl(url, headers), _multiline_curl(url, headers, newline), token


def test_multiline_lf_curl_matches_the_single_line_equivalent() -> None:
    single, multiline, _token = _browser_curl_sample()
    assert parse(multiline).to_dict() == parse(single).to_dict()


def test_multiline_crlf_curl_matches_the_single_line_equivalent() -> None:
    """粘贴到 Windows 换行的文本同样要能导入，结果与单行一致。"""
    single, multiline, _token = _browser_curl_sample(newline="\r\n")
    assert parse(multiline).to_dict() == parse(single).to_dict()


def test_multiline_curl_keeps_query_params_headers_and_auth_hint() -> None:
    _single, multiline, token = _browser_curl_sample()
    draft = parse(multiline)
    assert draft.method == "GET"
    assert draft.path == "/api/orders"
    assert [(q["name"], q["value"]) for q in draft.query_params] == [
        ("page", "1"),
        ("size", "20"),
        ("sort", "desc"),
        ("filter", "open"),
    ]
    # 九个请求头里两个是认证头：它们不落普通草稿，其余七个按原顺序保留。
    assert [h["name"] for h in draft.headers] == [
        "Accept",
        "Accept-Language",
        "Origin",
        "Referer",
        "User-Agent",
        "sec-ch-ua",
        "sec-fetch-site",
    ]
    assert draft.headers[5]["value"] == '"Chromium";v="140", "Not=A?Brand";v="24"'
    assert draft.auth_hint is not None
    assert token not in str(draft.to_dict())


def test_line_continuation_inside_a_word_joins_without_inserting_whitespace() -> None:
    """词中续行是拼接，不是插入：换行不能变成请求头里的一个字符。

    用请求头取值断言，而不是 URL：urlsplit 会自行剥掉换行，拿 URL 断言会让
    “换行被留下”这种错误看上去通过。
    """
    draft = parse("\n".join(['curl https://example.test/x -H "X-Trace: ab\\', 'cd"']))
    assert draft.headers == [{"name": "X-Trace", "value": "abcd"}]


def test_line_continuation_inside_double_quotes_still_joins() -> None:
    draft = parse("\n".join(['curl https://example.test/x -d "a\\', 'b"']))
    assert draft.body == "ab"


def test_indentation_after_a_continuation_inside_quotes_is_kept() -> None:
    """双引号内续行只删掉反斜杠与换行；下一行的缩进是正文，char 不能一起吃掉。"""
    draft = parse("\n".join(['curl https://example.test/x -d "a\\', '    b"']))
    assert draft.body == "a    b"


def test_bare_carriage_return_is_not_a_line_continuation() -> None:
    """裸 CR 不是换行：反斜杠与 CR 都属于正文，不能被当成续行删掉。

    旧 shlex 对 `"a\\<CR>b"` 的正文是反斜杠＋CR＋b；只兼容 LF 与完整 CRLF 时，
    这个既有数据语义必须原样保留。
    """
    draft = parse('curl https://example.test/x --data-raw "a\\\rb"')
    assert draft.body == "a\\\rb"


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 16001])
def test_long_backslash_runs_inside_double_quotes(count: int) -> None:
    r"""长反斜杠段（奇数／偶数，续行／非续行）的正文必须与 shell 语义一致。

    奇数个反斜杠后的换行是续行：(count-1)/2 个反斜杠，两侧直接拼接；偶数个不是
    续行：count/2 个反斜杠，换行本身留在正文。非续行时与旧 shlex 的结果一致。

    段长取到 16001，超过接口 20000 字符上限内的合理规模：这不是毫秒级性能断言，
    而是让“逐对消费”的扫描在长段上仍给出正确正文（此前的重复扫描是 O(n²)）。
    """
    backslashes = "\\" * count
    odd = count % 2 == 1
    halved = "\\" * (count // 2)

    for newline in ("\n", "\r\n"):
        draft = parse(
            f'curl https://example.test/x --data-raw "A{backslashes}{newline}B"'
        )
        expected = f"A{halved}B" if odd else f"A{halved}{newline}B"
        assert draft.body == expected, (count, repr(newline))

    # 非续行：段后直接是普通文本。shlex 在双引号内把每对反斜杠折半，奇数段末尾那个
    # 反斜杠后面不是特殊字符，原样保留，因此余下的反斜杠数是 (count + 1) // 2。
    literal = parse(f'curl https://example.test/x --data-raw "A{backslashes}B"')
    assert literal.body == f"A{'\\' * ((count + 1) // 2)}B"


def test_backslash_newline_inside_single_quotes_stays_literal() -> None:
    """单引号内反斜杠是字面量，`\\`＋换行在这里不是续行，不能删。"""
    draft = parse("\n".join(["curl https://example.test/x -d 'a\\", "b'"]))
    assert draft.body == "a\\\nb"


def test_double_backslash_before_newline_is_not_a_continuation() -> None:
    """偶数个反斜杠把换行留给外层：换行仍是分隔符，不能被当成续行吞掉。

    单行里 `\\\\b` 是转义后的字面反斜杠，拼进同一个词；多行里换行必须把两行分开，
    因此同样的片段在续行位置只能得到非法结果，而不是悄悄并进 URL。
    """
    assert parse("curl https://example.test/a\\\\b").path == "/a\\b"
    with pytest.raises(CurlParseError):
        parse("curl https://example.test/a\\\\\nb")


def test_real_newline_inside_a_quoted_body_is_preserved() -> None:
    draft = parse("\n".join(["curl https://example.test/x -d '{\"a\":", "1}'"]))
    assert "\n" in draft.body
    assert draft.body_type == "json"


def test_escaped_quote_does_not_end_its_string() -> None:
    draft = parse('curl https://example.test/x -H "X-Q: a\\"b"')
    assert draft.headers == [{"name": "X-Q", "value": 'a"b'}]


def test_unterminated_quote_is_still_rejected_after_continuation_handling() -> None:
    with pytest.raises(CurlParseError):
        parse("curl https://example.test/x -H 'X: 1")


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
