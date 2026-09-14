"""断言求值测试：区间默认开区间、字符串/存在/类型/集合语义。"""
from __future__ import annotations

import pytest

from app.kernel import assertion_operators  # noqa: F401 —— 触发操作符注册
from app.kernel.assertions import AssertionConfigError, AssertionContext, evaluate
from app.kernel.lossless_json import NumberNode, loads


def _num(text: str) -> NumberNode:
    return NumberNode(text=text)


def test_range_default_open_interval() -> None:
    params = {"min": {"type": "number", "text": "0"}, "max": {"type": "number", "text": "100"}}
    assert evaluate("range", 1, _num("0"), True, params).status == "failed"
    assert evaluate("range", 1, _num("50"), True, params).status == "passed"
    assert evaluate("range", 1, _num("100"), True, params).status == "failed"


def test_range_inclusive_boundaries() -> None:
    params = {
        "min": {"type": "number", "text": "0"},
        "max": {"type": "number", "text": "100"},
        "include_min": True,
        "include_max": True,
    }
    assert evaluate("range", 1, _num("0"), True, params).status == "passed"
    assert evaluate("range", 1, _num("100"), True, params).status == "passed"


def test_range_min_must_be_less_than_max() -> None:
    params = {"min": {"type": "number", "text": "100"}, "max": {"type": "number", "text": "0"}}
    with pytest.raises(AssertionConfigError):
        evaluate("range", 1, _num("50"), True, params)


def test_equals_string() -> None:
    params = {"expected": {"type": "string", "text": "abc"}}
    assert evaluate("equals", 1, "abc", True, params).status == "passed"
    assert evaluate("equals", 1, "ABC", True, params).status == "failed"
    assert evaluate("equals", 1, 123, True, params).status == "failed"


def test_equals_type_sensitive() -> None:
    # 字符串 "0" 与数字 0 不相等。
    assert evaluate("equals", 1, "0", True, {"expected": {"type": "number", "text": "0"}}).status == "failed"
    assert evaluate("equals", 1, _num("0"), True, {"expected": {"type": "string", "text": "0"}}).status == "failed"


def test_exists_distinguishes_null() -> None:
    assert evaluate("exists", 1, None, True, {}).status == "passed"
    assert evaluate("exists", 1, None, False, {}).status == "failed"
    assert evaluate("not_exists", 1, None, False, {}).status == "passed"


def test_is_null_vs_missing() -> None:
    assert evaluate("is_null", 1, None, True, {}).status == "passed"
    assert evaluate("is_null", 1, None, False, {}).status == "failed"


def test_is_empty_semantics() -> None:
    assert evaluate("is_empty", 1, None, False, {}).status == "passed"
    assert evaluate("is_empty", 1, "", True, {}).status == "passed"
    assert evaluate("is_empty", 1, [], True, {}).status == "passed"
    assert evaluate("is_empty", 1, 0, True, {}).status == "failed"
    assert evaluate("not_empty", 1, False, True, {}).status == "passed"


def test_is_type_integer_vs_number() -> None:
    assert evaluate("is_type", 1, _num("5"), True, {"type": "integer"}).status == "passed"
    assert evaluate("is_type", 1, _num("5"), True, {"type": "number"}).status == "failed"
    assert evaluate("is_type", 1, _num("1.5"), True, {"type": "number"}).status == "passed"


def test_in_set() -> None:
    params = {"values": [{"type": "number", "text": "200"}, {"type": "number", "text": "201"}]}
    assert evaluate("in_set", 1, _num("200"), True, params).status == "passed"
    assert evaluate("in_set", 1, _num("500"), True, params).status == "failed"


def test_contains_string() -> None:
    assert evaluate("contains", 1, "hello world", True, {"expected": {"type": "string", "text": "world"}}).status == "passed"
    assert evaluate("starts_with", 1, "A123", True, {"expected": {"type": "string", "text": "A"}}).status == "passed"
    assert evaluate("ends_with", 1, "A123", True, {"expected": {"type": "string", "text": "123"}}).status == "passed"


def test_length_equals() -> None:
    assert evaluate("length_equals", 1, [1, 2], True, {"expected": {"type": "number", "text": "2"}}).status == "passed"
    assert evaluate("length_equals", 1, "abc", True, {"expected": {"type": "number", "text": "3"}}).status == "passed"


def test_sensitive_path_rejected() -> None:
    ctx = AssertionContext(sensitive_paths={("headers", "Authorization")})
    from app.kernel.assertions import AssertionPolicyError

    with pytest.raises(AssertionPolicyError):
        evaluate("equals", 1, "Bearer x", True, {"expected": {"type": "string", "text": "Bearer x"}}, path=("headers", "Authorization"), ctx=ctx)


# —— 布尔与数字：类型语义必须分开 ——


def test_boolean_equality_does_not_crash() -> None:
    """bool 是 int 的子类，不能落进数字分支交给 Decimal。

    此前 expected/actual 均为布尔时抛 InvalidOperation，断言不是“失败”而是
    直接把工作项打挂。
    """
    for expected, actual, status in [
        (True, True, "passed"),
        (False, False, "passed"),
        (True, False, "failed"),
        (False, True, "failed"),
    ]:
        outcome = evaluate(
            "equals", 1, actual, True, {"expected": {"type": "boolean", "value": expected}}
        )
        assert outcome.status == status, f"布尔 {actual} 与期望 {expected} 应得到 {status}"


def test_boolean_inside_containers_and_sets() -> None:
    nested = evaluate(
        "equals",
        1,
        {"a": [True, {"b": False}]},
        True,
        {"expected": {"type": "json", "text": '{"a":[true,{"b":false}]}'}},
    )
    assert nested.status == "passed"
    assert evaluate(
        "in_set", 1, False, True, {"values": [{"type": "boolean", "value": False}]}
    ).status == "passed"


def test_json_expected_value_with_numbers_compares_by_value() -> None:
    """json 期望值必须递归转换内部的 NumberNode，否则含数字时恒判不相等。

    这是同一类断言“一半能用一半不能用”的隐蔽缺陷：期望 `{"k":true}` 恰好相等，
    期望 `[1,2]` 或 `{"k":2}` 却必然失败——实际值来自响应，数字是 NumberNode，
    期望值走字面量转换得到 Decimal，`type is type` 比较直接否掉。用户看到的只是
    “值不等于期望”，无从判断是自己写错还是比较实现有洞。
    """
    for text in [
        "[1,2]",
        '{"k":2}',
        '{"a":[true,{"b":2}]}',
        "[9007199254740993]",
    ]:
        outcome = evaluate(
            "equals", 1, loads(text), True, {"expected": {"type": "json", "text": text}}
        )
        assert outcome.status == "passed", f"期望 {text} 与同构实际值应相等：{outcome.reason_code}"
    # 真正不相等的仍然失败，修复不是把比较放宽成恒真。
    assert evaluate(
        "equals", 1, loads("[1,3]"), True, {"expected": {"type": "json", "text": "[1,2]"}}
    ).status == "failed"


def test_boolean_is_not_number() -> None:
    # True 与 1 是不同类型，不能被当成相等。
    assert evaluate("equals", 1, True, True, {"expected": {"type": "number", "text": "1"}}).status == "failed"
    assert evaluate("equals", 1, 1, True, {"expected": {"type": "boolean", "value": True}}).status == "failed"


def test_number_equality_ignores_lexical_spelling() -> None:
    assert evaluate("equals", 1, _num("1"), True, {"expected": {"type": "number", "text": "1.0"}}).status == "passed"
    assert evaluate("equals", 1, _num("1e2"), True, {"expected": {"type": "number", "text": "100"}}).status == "passed"


# —— 敏感值：祖先容器与后代同样受限 ——


def test_sensitive_container_and_descendants_rejected() -> None:
    """对包含敏感字段的整行求值，实际值里就带着秘密。"""
    from app.kernel.assertions import AssertionPolicyError

    ctx = AssertionContext(sensitive_paths={("request.header", "Authorization")})
    for path in [
        ("request.header",),  # 祖先：整行请求头
        ("request.header", "Authorization"),  # 敏感位置本身
        ("request.header", "Authorization", "x"),  # 敏感值的后代
    ]:
        with pytest.raises(AssertionPolicyError):
            evaluate(
                "equals",
                1,
                "synthetic",
                True,
                {"expected": {"type": "string", "text": "synthetic"}},
                path=path,
                ctx=ctx,
            )


def test_sensitive_check_allows_unrelated_paths() -> None:
    """保护不能外溢到无关字段，否则普通断言全部不可用。"""
    ctx = AssertionContext(sensitive_paths={("request.header", "Authorization")})
    for path in [("request.header", "X-Trace"), ("request.body", "name"), ("response.status",)]:
        assert evaluate(
            "equals",
            1,
            "synthetic",
            True,
            {"expected": {"type": "string", "text": "synthetic"}},
            path=path,
            ctx=ctx,
        ).status == "passed"


def test_unknown_operator_rejected() -> None:
    with pytest.raises(AssertionConfigError):
        evaluate("nonexistent", 1, "x", True, {})


# —— 参数校验：保存边界必须与内核同样严格 ——


def test_non_finite_number_parameter_rejected_at_save_boundary() -> None:
    """保存／发布时就要拒绝 NaN、Infinity 这类“能解析但不能比较”的数值参数。

    边界校验曾就地另写一份数字解析，只捕获 InvalidOperation，于是 ``Decimal('NaN')``
    被当成合法数字通过保存与发布，直到执行阶段才变成一条运行时类型错误——用户看到
    的是业务失败，而不是自己配错了边界。契约校验器已排除非有限值，这里断言边界
    复用它而不是再实现一遍。
    """
    from app.kernel.assertion_catalog import AssertionCatalogError, validate_parameters

    for text in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(AssertionCatalogError):
            validate_parameters("range", {"min": {"type": "number", "text": text}, "max": {"type": "number", "text": "100"}})
        with pytest.raises(AssertionCatalogError):
            validate_parameters("greater_than", {"expected": {"type": "number", "text": text}})


def test_number_parameter_rejects_wrong_literal_type() -> None:
    from app.kernel.assertion_catalog import AssertionCatalogError, validate_parameters

    with pytest.raises(AssertionCatalogError):
        validate_parameters("greater_than", {"expected": {"type": "boolean", "value": True}})
    with pytest.raises(AssertionCatalogError):
        validate_parameters("greater_than", {"expected": {"type": "string", "text": "10"}})


def test_valid_number_parameters_still_accepted() -> None:
    from app.kernel.assertion_catalog import validate_parameters

    validate_parameters(
        "range",
        {
            "min": {"type": "number", "text": "0"},
            "max": {"type": "number", "text": "100"},
            "include_min": False,
            "include_max": True,
        },
    )
    for text in ("9007199254740993", "0.10000000000000000000000000001", "-1e-10", "0"):
        validate_parameters("greater_than", {"expected": {"type": "number", "text": text}})


# —— 定位步骤只能用于结构化来源 ——


def test_selector_rejected_on_direct_value_sources() -> None:
    """状态码、耗时等来源只取单值，配置定位步骤必须在保存／发布时被拒绝。

    这类配置曾能通过校验，却在执行时由取值环节抛错；执行器的取值发生在断言异常
    处理之外，错误冒到 worker 的兜底捕获——那里只记日志、不写终态，于是工作项
    反复被领取却永不结束。
    """
    from app.kernel.assertion_spec import AssertionSpecError, validate_assertions

    def item(source: str, selector: list[dict]) -> dict:
        return {
            "id": "a",
            "target_source": source,
            "selector": selector,
            "type": "equals",
            "parameters": {"expected": {"type": "number", "text": "200"}},
            "severity": "error",
        }

    for source in ("response.status", "response.elapsed", "response.text", "request.path", "request.text"):
        with pytest.raises(AssertionSpecError):
            validate_assertions([item(source, [{"kind": "key", "key": "x"}])])


def test_selector_still_allowed_on_structured_sources() -> None:
    """收紧不能外溢到真正需要定位的来源，否则字段断言整体不可用。"""
    from app.kernel.assertion_spec import validate_assertions

    def item(source: str, selector: list[dict]) -> dict:
        return {
            "id": "a",
            "target_source": source,
            "selector": selector,
            "type": "exists",
            "parameters": {},
            "severity": "error",
        }

    for source, selector in [
        ("response.status", []),
        ("response.elapsed", []),
        ("response.body", [{"kind": "key", "key": "data"}]),
        ("response.header", [{"kind": "repeat_key", "key": "Set-Cookie", "occurrence": 0}]),
        ("request.query", [{"kind": "index", "index": 0}]),
    ]:
        assert validate_assertions([item(source, selector)])[0]["selector"] == selector
