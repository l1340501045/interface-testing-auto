"""JE1：对象相等比较必须保留全部业务字段。

复现（主审给出）：变量 obj 为 {"name":"x","value":1,"enabled":false}，JSON 正文
{"payload":"{{obj}}"}，断言 payload 等于 {"name":"x","value":1,"enabled":true}。
旧实现按**形状**猜来源——对象只要含 name/value 两键就当成“重复键对”缩减成两个
字段，两侧的 enabled 一同被丢弃，于是判 passed 并把请求发出去：断言通过而内容
并不相等。这里锁死：通用字典逐字段递归比较，简写只认取值边界的来源标记。
"""
from __future__ import annotations

from app.kernel import assertion_operators  # noqa: F401 —— 触发操作符注册
from app.kernel.assertion_inputs import SourceRoots, pairs_root
from app.kernel.assertions import AssertionContext, evaluate
from app.kernel.lossless_json import loads


def _json_params(text: str) -> dict:
    """期望对象／数组：与界面同一条路径，json 字面量。"""
    return {"expected": {"type": "json", "text": text}}


def _string_params(text: str) -> dict:
    """只写取值的简写：字符串字面量。"""
    return {"expected": {"type": "string", "text": text}}


def _equals(actual: object, params: dict):
    return evaluate("equals", 1, actual, True, params, ctx=AssertionContext())


def _eq_json(actual: object, expected_text: str):
    return _equals(actual, _json_params(expected_text))


def _loads(text: str):
    return loads(text)


# —— 主审复现：只差一个被丢弃的字段 ——


def test_objects_differing_only_in_a_dropped_field_are_not_equal() -> None:
    actual = _loads('{"name": "x", "value": 1, "enabled": false}')
    outcome = _eq_json(actual, '{"name": "x", "value": 1, "enabled": true}')

    assert outcome.status == "failed", (outcome.status, outcome.reason_code)
    assert outcome.reason_code == "not_equal"
    # 证据里也必须留着第三个字段，不能再被缩减成 name/value 两个键。
    assert set(outcome.expected) == {"name", "value", "enabled"}
    assert set(outcome.actual) == {"name", "value", "enabled"}
    assert outcome.expected["enabled"] is True
    assert outcome.actual["enabled"] is False
    # 同一个对象进集合比较同样不能“沾上”一个只差该字段的候选。
    in_set = evaluate(
        "in_set",
        1,
        actual,
        True,
        {"values": [{"type": "json", "text": '{"name": "x", "value": 1, "enabled": true}'}]},
        ctx=AssertionContext(),
    )
    assert in_set.status == "failed"


def test_complete_equal_objects_pass() -> None:
    actual = _loads('{"name": "x", "value": 1, "enabled": true}')
    assert _eq_json(actual, '{"name": "x", "value": 1, "enabled": true}').status == "passed"
    # 键顺序不同仍是同一个对象。
    assert _eq_json(actual, '{"enabled": true, "value": 1, "name": "x"}').status == "passed"
    # 反方向：真的不等时 not_equals 才通过，相等时不得通过。
    assert evaluate("not_equals", 1, actual, True, _json_params('{"name": "x", "value": 1, "enabled": false}'), ctx=AssertionContext()).status == "passed"
    assert evaluate("not_equals", 1, actual, True, _json_params('{"name": "x", "value": 1, "enabled": true}'), ctx=AssertionContext()).status == "failed"


def test_missing_or_extra_field_is_not_equal() -> None:
    full = _loads('{"name": "x", "value": 1, "enabled": true}')
    assert _eq_json(full, '{"name": "x", "value": 1}').status == "failed"
    assert _eq_json(_loads('{"name": "x", "value": 1}'), '{"name": "x", "value": 1, "enabled": true}').status == "failed"
    # 嵌套层同样逐字段，缺字段不是“子集相等”。
    nested = _loads('{"payload": {"name": "x", "value": 1, "enabled": true}}')
    assert _eq_json(nested, '{"payload": {"name": "x", "value": 1}}').status == "failed"
    assert _eq_json(nested, '{"payload": {"name": "x", "value": 1, "enabled": true}}').status == "passed"
    assert _eq_json(nested, '{"payload": {"name": "x", "value": 1, "enabled": true, "extra": 1}}').status == "failed"


def test_nested_numbers_are_still_lossless() -> None:
    """2**53+1 与 2**53 经二进制浮点会相等，十进制比较必须能区分。"""
    huge = _loads('{"name": "x", "value": 9007199254740993, "enabled": true}')
    assert _eq_json(huge, '{"name": "x", "value": 9007199254740993, "enabled": true}').status == "passed"
    assert _eq_json(huge, '{"name": "x", "value": 9007199254740992, "enabled": true}').status == "failed"
    # 嵌套深处的长整数与小数同样无损。
    deep = _loads('{"payload": {"ids": [9007199254740993], "ratio": 0.1}}')
    assert _eq_json(deep, '{"payload": {"ids": [9007199254740993], "ratio": 0.1}}').status == "passed"
    assert _eq_json(deep, '{"payload": {"ids": [9007199254740992], "ratio": 0.1}}').status == "failed"
    assert _eq_json(deep, '{"payload": {"ids": [9007199254740993], "ratio": 0.10}}').status == "passed"


# —— 同样形状的业务数组不得被当成重复键简写 ——


def test_business_array_of_one_pair_is_not_unwrapped() -> None:
    one_item = _loads('[{"name": "x", "value": 1}]')
    # 正文里的数组就是一个数组：与同形对象不相等，与单键取值也不相等。
    assert _eq_json(one_item, '{"name": "x", "value": 1}').status == "failed"
    assert _eq_json(one_item, '{"name": "x", "value": 2}').status == "failed"
    assert _eq_json(one_item, '[{"name": "x", "value": 1}]').status == "passed"
    assert _equals(one_item, _string_params("x")).status == "failed"


def test_no_field_gets_a_special_comparison_rule() -> None:
    """名字叫 name／value 的字段不享有特殊比较规则，与其它字段同一条规则。

    旧实现给“恰好只有 name/value 两个键”的对象单独开了一条比较捷径，左侧名字用
    原生相等判断，于是 1 与 1.0 因为字面不同被判不相等——同一个值在不同字段上
    结论不一致。字段比较只该有一条规则：数字按十进制值相等。
    """
    actual = _loads('{"name": 1, "value": 2}')
    assert _eq_json(actual, '{"name": 1.0, "value": 2}').status == "passed"
    assert _eq_json(actual, '{"name": 1, "value": 2.5}').status == "failed"
    # 同一条捷径用原生宽松相等还带来了数字与布尔的混同：1 == true 在 Python 里
    # 成立，类型却被吞掉了。字段比较必须保留类型。
    assert _eq_json(actual, '{"name": true, "value": 2}').status == "failed"


# —— 取值边界的简写仍然有效 ——


def _header_value(pairs: list[dict[str, str]]):
    roots = SourceRoots()
    roots.add_tree("request.header", pairs_root(pairs))
    found, value = roots.extract("request.header", [])
    assert found
    return value


def test_header_and_query_pair_shorthand_still_works() -> None:
    single = _header_value([{"name": "X-Trace", "value": "abc"}])
    # 整份数组、单个 pair、只写取值三种写法都保留。
    assert _eq_json(single, '[{"name": "X-Trace", "value": "abc"}]').status == "passed"
    assert _eq_json(single, '{"name": "X-Trace", "value": "abc"}').status == "passed"
    assert _equals(single, _string_params("abc")).status == "passed"
    assert _equals(single, _string_params("abd")).status == "failed"
    assert _eq_json(single, '{"name": "X-Other", "value": "abc"}').status == "failed"


def test_repeated_key_pairs_still_compare_every_occurrence() -> None:
    twice = _header_value([{"name": "X-Trace", "value": "abc"}, {"name": "X-Trace", "value": "def"}])
    assert _eq_json(twice, '[{"name": "X-Trace", "value": "abc"}, {"name": "X-Trace", "value": "def"}]').status == "passed"
    # 出现两次却拿单个 pair 当期望：这是长度不符，不能因为“简写”就放过。
    assert _eq_json(twice, '{"name": "X-Trace", "value": "abc"}').status == "failed"
    assert _equals(twice, _string_params("abc")).status == "failed"


# —— 同一函数的其它形状猜测：长度也数全部字段 ——


def test_object_length_counts_every_field() -> None:
    actual = _loads('{"name": "x", "value": 1, "enabled": true}')
    assert evaluate("length_equals", 1, actual, True, {"expected": {"type": "number", "text": "3"}}).status == "passed"
    # 旧实现按形状缩减成 2 个键，期望 2 会被误判通过。
    assert evaluate("length_equals", 1, actual, True, {"expected": {"type": "number", "text": "2"}}).status == "failed"
