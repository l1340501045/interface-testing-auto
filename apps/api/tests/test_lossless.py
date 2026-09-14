"""无损数值与 JSON 解析测试：验证 9007199254740993 等大整数不丢精度。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.kernel.lossless_json import LosslessJSONError, NumberNode, dumps, loads, to_python
from app.kernel.valueliteral import ValueLiteral, ValueLiteralError


def test_big_integer_not_rounded() -> None:
    node = NumberNode(text="9007199254740993")
    assert node.decimal() == Decimal("9007199254740993")


def test_big_integer_distinct_from_neighbor() -> None:
    a = NumberNode(text="9007199254740993")
    b = NumberNode(text="9007199254740992")
    assert a.decimal() != b.decimal()


def test_json_parse_preserves_big_integer() -> None:
    parsed = loads('{"id": 9007199254740993}')
    assert isinstance(parsed["id"], NumberNode)
    assert parsed["id"].text == "9007199254740993"


def test_decimal_exact_comparison() -> None:
    a = NumberNode(text="0.1")
    b = NumberNode(text="0.2")
    assert (a.decimal() + b.decimal()) == Decimal("0.3")


def test_string_zero_not_number_zero() -> None:
    string_zero = ValueLiteral(type="string", text="0")
    number_zero = ValueLiteral(type="number", text="0")
    assert string_zero.type != number_zero.type
    assert string_zero.as_python() == "0"
    assert number_zero.as_python() == Decimal("0")
    assert string_zero.as_python() != number_zero.as_python()


def test_to_python_converts_numbers_to_decimal() -> None:
    parsed = loads('{"n": 1.5, "big": 9007199254740993}')
    py = to_python(parsed)
    assert py["n"] == Decimal("1.5")
    assert py["big"] == Decimal("9007199254740993")


def test_json_decimal_stays_lossless_through_literal() -> None:
    """json 字面量不得走普通 json.loads 退化成 float。"""
    literal = ValueLiteral.from_dict(
        {"type": "json", "text": '{"n":0.12345678901234567890123456789}'}
    )
    value = literal.as_python()["n"]
    assert isinstance(value, NumberNode)
    assert value.decimal() == Decimal("0.12345678901234567890123456789")


def test_non_standard_json_constants_rejected() -> None:
    """NaN／Infinity 不是合法 JSON，不能混进字段树。"""
    for text in ['{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}']:
        with pytest.raises(LosslessJSONError):
            loads(text)


# —— 回写：脱敏后重新序列化不得丢精度 ——


def test_dumps_preserves_number_lexeme() -> None:
    """脱敏要“解析 → 遮蔽 → 写回”，写回若走 json.dumps 就把无损契约破坏了。"""
    parsed = loads('{"big":9007199254740993,"n":0.12345678901234567890123456789}')
    assert dumps(parsed) == '{"big":9007199254740993,"n":0.12345678901234567890123456789}'


def test_dumps_round_trips_json_syntax() -> None:
    """引号、反斜线与中文都要按标准 JSON 转义，回写结果必须能再次解析。"""
    source = '{"quote":"a\\"b","backslash":"a\\\\b","中文":"值","nested":[1,true,null]}'
    assert dumps(loads(source)) == source
    assert loads(dumps(loads(source))) == loads(source)


def test_dumps_rejects_values_it_cannot_represent() -> None:
    with pytest.raises(LosslessJSONError):
        dumps({1, 2})


# —— 字面量严格校验：不做猜测式转换 ——


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "boolean", "value": "false"},  # bool('false') 会得到 True
        {"type": "boolean", "value": 1},
        {"type": "boolean"},  # 缺 value 字段
        {"type": "number", "text": "abc"},
        {"type": "number", "text": "Infinity"},
        {"type": "number", "text": "NaN"},
        {"type": "json", "text": '{"n":NaN}'},
        {"type": "json", "text": '{"a":1'},  # 截断的 JSON
        {"type": "json"},
        {"type": "string"},
        {"type": "未知"},
        {"text": "abc"},  # 缺 type
        "not-a-dict",
    ],
)
def test_invalid_literals_rejected(raw: object) -> None:
    with pytest.raises(ValueLiteralError):
        ValueLiteral.from_dict(raw)


def test_valid_literals_still_accepted() -> None:
    assert ValueLiteral.from_dict({"type": "boolean", "value": False}).value is False
    assert ValueLiteral.from_dict({"type": "null"}).type == "null"
    assert ValueLiteral.from_dict({"type": "string", "text": "abc"}).text == "abc"
    big = ValueLiteral.from_dict({"type": "number", "text": "9007199254740993"})
    assert big.as_decimal() == Decimal("9007199254740993")
