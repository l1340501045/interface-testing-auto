"""正文变量绑定：JSON 与表单按各自协议逐字段绑定，不整段替换。

此前的实际故障有两处，都不是用户写错了模板，而是绑定落在了错误的位置：

- `{"score":"{{shared}}"}` 里的数字变量 1 被整段文本替换成字符串 "1"，数值断言
  随后判 `type_error`（准备后的 score 不是数字）。
- 表单正文 `q={{value}}` 的取值 `a&b=c` 被当成表单结构的一部分，在线解码后变成
  `q=a` 与 `b=c` 两项。

因此这里同时锁死两件事：**含变量时按协议绑定**（类型与字段边界都不被取值改变），
**不含变量时原文直发**（一个字节都不动）。

纯方法测试：不连接数据库、不发送请求。
"""
from __future__ import annotations

from urllib.parse import parse_qsl

import pytest

from app.kernel.request_spec import RequestSpecError, prepare, validate_request
from app.kernel.valueliteral import ValueLiteral
from app.kernel.variables import VariableResolutionError, VariableResolver

_BASE = "http://echo:8080"


def _literal(kind: str, **kwargs: object) -> ValueLiteral:
    return ValueLiteral(type=kind, **kwargs)


def _resolver(**variables: ValueLiteral) -> VariableResolver:
    return VariableResolver(dict(variables))


def _body(body_type: str, body: str, resolver: VariableResolver) -> str:
    spec = {
        "method": "POST",
        "path": "/echo",
        "query_params": [],
        "headers": [],
        "body_type": body_type,
        "body": body,
    }
    return prepare(spec, _BASE, resolver).raw_body


# —— JSON：整体占位保留类型 ——


def test_a_number_variable_stays_a_number_in_json() -> None:
    """真实故障：数字 1 被写成字符串 "1"，数值断言判类型不符。"""
    resolver = _resolver(shared=_literal("number", text="1"))
    assert _body("json", '{"score":"{{shared}}"}', resolver) == '{"score":1}'


def test_a_long_integer_variable_survives_losslessly() -> None:
    """长整数不得经过二进制浮点：9007199254740993 的相邻整数是另一个值。"""
    resolver = _resolver(big=_literal("number", text="9007199254740993"))
    assert (
        _body("json", '{"id":"{{big}}"}', resolver)
        == '{"id":9007199254740993}'
    )


def test_a_scientific_number_variable_is_written_as_valid_json() -> None:
    """`1E+2` 是合法数字文本，直接写进正文却是非法 JSON；定点写法等值且合法。"""
    resolver = _resolver(n=_literal("number", text="1E+2"))
    assert _body("json", '{"n":"{{n}}"}', resolver) == '{"n":100}'


def test_a_string_variable_with_quotes_and_backslashes_keeps_json_valid() -> None:
    """整段替换会直接拼出 `a"b\\c` 这种残缺正文；字符串位置必须转义。"""
    resolver = _resolver(s=_literal("string", text='a"b\\c'))
    bound = _body("json", '{"s":"{{s}}"}', resolver)
    assert bound == '{"s":"a\\"b\\\\c"}'
    # 结果必须仍是可解析的 JSON，而不是看起来像但读不出来的文本。
    from app.kernel.lossless_json import loads

    assert loads(bound) == {"s": 'a"b\\c'}


def test_boolean_null_object_and_array_keep_their_types() -> None:
    resolver = _resolver(
        b=_literal("boolean", value=True),
        n=_literal("null"),
        o=_literal("json", text='{"k": 9007199254740993}'),
        a=_literal("json", text="[1, 2]"),
    )
    assert (
        _body("json", '{"b":"{{b}}","n":"{{n}}","o":"{{o}}","a":"{{a}}"}', resolver)
        == '{"b":true,"n":null,"o":{"k":9007199254740993},"a":[1,2]}'
    )


def test_a_placeholder_inside_a_longer_string_stays_a_string() -> None:
    """嵌入更长文本时不得换类型，否则模板结构会随变量取值改变。"""
    resolver = _resolver(n=_literal("number", text="1"))
    assert _body("json", '{"t":"pre{{n}}post"}', resolver) == '{"t":"pre1post"}'


def test_object_keys_also_accept_variables() -> None:
    resolver = _resolver(k=_literal("string", text="分数"))
    assert _body("json", '{"{{k}}":1}', resolver) == '{"分数":1}'


def test_a_key_that_renders_to_an_existing_key_fails_instead_of_overwriting() -> None:
    """两个键渲染成同一个键时，后者会静默盖掉前者，必须显式拒绝。"""
    resolver = _resolver(
        first=_literal("string", text="同"),
        second=_literal("string", text="同"),
    )
    with pytest.raises(RequestSpecError, match="渲染后重名"):
        _body("json", '{"{{first}}":1,"{{second}}":2}', resolver)


def test_an_unknown_variable_in_the_body_fails_before_send() -> None:
    """未知变量必须显式失败，不能把 {{...}} 原样当取值发出去。"""
    with pytest.raises(VariableResolutionError, match="未定义变量"):
        _body("json", '{"a":"{{missing}}"}', _resolver())


def test_an_undefined_variable_in_a_body_without_other_variables_still_fails() -> None:
    """正文里只有一个未定义引用时同样要失败——快速路径不能把它当成无变量。"""
    with pytest.raises(VariableResolutionError, match="未定义变量"):
        _body("form", "q={{missing}}", _resolver())


# —— JSON：不含变量时原文直发 ——


def test_a_json_body_without_variables_is_sent_byte_for_byte() -> None:
    """用户调好的排版、键顺序与重复写法都原样上线，不被“规范化”。"""
    body = '{ "amount" : 25 ,\n  "name": "abc" }'
    assert _body("json", body, _resolver()) == body
    # 重复键在原文里是一个字节，解析再序列化会把它折叠掉：这里必须不动。
    duplicated = '{"a":1,"a":2}'
    assert _body("json", duplicated, _resolver()) == duplicated


def test_a_json_body_with_a_variable_is_re_serialised_from_the_parsed_structure() -> None:
    """含变量时正文会被重排——这是“无损解析后写回”的可见结果，不是静默改内容。"""
    resolver = _resolver(n=_literal("number", text="1"))
    assert _body("json", '{ "n" : "{{n}}" }', resolver) == '{"n":1}'


# —— 表单：字段边界不被取值改变 ——


def test_a_form_value_containing_separators_stays_one_field() -> None:
    """真实故障：`q=a&b=c` 在线解码后变成 q=a 与 b=c 两项。"""
    resolver = _resolver(value=_literal("string", text="a&b=c"))
    bound = _body("form", "q={{value}}", resolver)
    assert parse_qsl(bound, keep_blank_values=True) == [("q", "a&b=c")]


def test_form_separators_plus_space_and_chinese_do_not_move_field_boundaries() -> None:
    resolver = _resolver(value=_literal("string", text="a&b=c+d e 中文&f="))
    bound = _body("form", "q={{value}}", resolver)
    assert parse_qsl(bound, keep_blank_values=True) == [("q", "a&b=c+d e 中文&f=")]


def test_repeated_form_fields_keep_their_order_and_count() -> None:
    resolver = _resolver(value=_literal("string", text="1"))
    bound = _body("form", "a=1&a=2&b={{value}}&a=3", resolver)
    assert parse_qsl(bound, keep_blank_values=True) == [
        ("a", "1"),
        ("a", "2"),
        ("b", "1"),
        ("a", "3"),
    ]


def test_a_form_body_without_variables_is_sent_byte_for_byte() -> None:
    """不含变量时连 `a=b&c` 这种省略等号的写法也原样保留。"""
    body = "a=b&c&d=%20&e=中 文"
    assert _body("form", body, _resolver()) == body


def test_a_form_reference_mangled_by_decoding_fails_instead_of_sending_literally() -> None:
    """`{{a+b}}` 解码后不再是变量引用；按字面量发出去等于发了一份没写过的正文。"""
    with pytest.raises(RequestSpecError, match="表单编码会改变含义"):
        _body("form", "q={{a+b}}", _resolver())


# —— 其它正文与位置不受影响 ——


def test_text_body_keeps_whole_text_substitution() -> None:
    """纯文本内部没有结构，沿用既有语义：数字变量按文本写入。"""
    resolver = _resolver(n=_literal("number", text="1"))
    assert _body("text", "value={{n}}", resolver) == "value=1"


def test_query_and_headers_keep_their_own_protocol_semantics() -> None:
    """查询参数与请求头是文本协议，数字变量在这里必须是文本而不是数字。"""
    resolver = _resolver(n=_literal("number", text="1"))
    spec = {
        "method": "GET",
        "path": "/echo",
        "query_params": [{"name": "n", "value": "{{n}}"}],
        "headers": [{"name": "X-N", "value": "{{n}}"}],
        "body_type": "none",
        "body": "",
    }
    prepared = prepare(spec, _BASE, resolver)
    assert prepared.query == [("n", "1")]
    assert ("X-N", "1") in prepared.headers


# —— 保存校验：给出可用的数值写法 ——


def test_unquoted_placeholder_is_rejected_with_a_usable_alternative() -> None:
    """未加引号的占位在保存时就被拒；错误信息要直接给出可照抄的写法。"""
    with pytest.raises(RequestSpecError, match="需要写在 JSON 字符串的引号内"):
        validate_request(
            {
                "method": "POST",
                "path": "/echo",
                "body_type": "json",
                "body": '{"score":{{shared}}}',
            }
        )
    # 同样的取值写在引号内就是合法定义，且渲染后仍是数字。
    normalized = validate_request(
        {
            "method": "POST",
            "path": "/echo",
            "body_type": "json",
            "body": '{"score":"{{shared}}"}',
        }
    )
    assert normalized["body"] == '{"score":"{{shared}}"}'
