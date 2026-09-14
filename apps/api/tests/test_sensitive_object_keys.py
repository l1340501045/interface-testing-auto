"""敏感值出现在对象**键名**上时的识别与脱敏。

此前只有值参与“是否含秘密”的扫描：目标回显 `{"<凭据>": "ok"}` 时，这个对象里
没有任何字段的值含秘密，于是整对象等值断言既求得了值（通过／不通过本身就是猜
秘密的通道），`actual` 又把明文键写进了结果与数据库。键名和值一样是取值的一部分，
必须同等标注、同等遮蔽。

这里只验证纯方法，不连接数据库、不发送请求。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.kernel import assertion_operators  # noqa: F401 —— 触发操作符注册
from app.kernel.assertion_inputs import SourceRoots, json_root, paths_containing
from app.kernel.assertions import AssertionContext, AssertionPolicyError, evaluate
from app.kernel.redaction import MASK, redact_pairs, redact_value

# 合成秘密：只在本次运行生成，源码里不留固定 token，也不打印到任何交付说明里。
# 本文件全部用例都按**变量名**引用它（含 JSON 构造与断言），因此生成方式改掉后
# 覆盖语义逐条不变：键名命中即受保护、证据被遮蔽、兄弟来源仍可用。
SECRET = f"it-objkey-{uuid.uuid4().hex}"


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _roots(body: dict) -> SourceRoots:
    roots = SourceRoots()
    roots.add_tree("response.body", json_root(_json(body)))
    return roots


def _context(body: dict) -> AssertionContext:
    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(_roots(body), [SECRET])
    return ctx


# —— 识别：键名命中即受保护 ——


def test_secret_used_as_object_key_is_recognized() -> None:
    found = paths_containing(_roots({SECRET: "ok"}), [SECRET])
    assert ("response.body", SECRET) in found


def test_nested_secret_key_is_recognized() -> None:
    found = paths_containing(_roots({"outer": {"inner": {SECRET: "ok"}}}), [SECRET])
    assert ("response.body", "outer", "inner", SECRET) in found


def test_secret_key_in_an_array_element_is_recognized() -> None:
    found = paths_containing(_roots({"items": [{SECRET: "ok"}]}), [SECRET])
    assert ("response.body", "items", 0, SECRET) in found


def test_unrelated_object_has_no_sensitive_paths() -> None:
    assert paths_containing(_roots({"a": 1, "b": {"c": "d"}}), [SECRET]) == set()


# —— 拒绝：整对象与子路径都不得成为猜秘密的通道 ——


def _whole_object_assertion(roots_ctx: AssertionContext):
    """整对象相等：value 是 dict，期望值用 json 字面量表示。"""
    return evaluate(
        "equals",
        1,
        {SECRET: "ok"},
        True,
        {"expected": {"type": "json", "text": _json({SECRET: "ok"})}},
        path=("response.body",),
        ctx=roots_ctx,
    )


def test_whole_object_equality_assertion_on_a_secret_keyed_object_is_refused() -> None:
    with pytest.raises(AssertionPolicyError):
        _whole_object_assertion(_context({SECRET: "ok"}))


def test_sub_path_assertion_on_the_secret_key_is_refused() -> None:
    ctx = _context({SECRET: "ok"})
    with pytest.raises(AssertionPolicyError):
        evaluate(
            "equals",
            1,
            "ok",
            True,
            {"expected": {"type": "string", "text": "ok"}},
            path=("response.body", SECRET),
            ctx=ctx,
        )


def test_projection_on_the_containing_object_is_refused() -> None:
    """对包含秘密键的对象做不取值只看结构的检查，同样不能放行。"""
    ctx = _context({SECRET: "ok"})
    for operator, params in (("is_empty", {}), ("not_empty", {}), ("exists", {})):
        with pytest.raises(AssertionPolicyError):
            evaluate(operator, 1, {SECRET: "ok"}, True, params, path=("response.body",), ctx=ctx)


def test_policy_error_does_not_reveal_the_key_name() -> None:
    """错误与路径提示不能反而把键名写出来——那就换了一条通道泄露。"""
    with pytest.raises(AssertionPolicyError) as error:
        _whole_object_assertion(_context({SECRET: "ok"}))
    assert SECRET not in str(error.value)


def test_unrelated_sibling_node_stays_usable() -> None:
    """保护要精确到坐标，不能因为同一对象里有个秘密键就把整份响应变成只读。"""
    ctx = _context({SECRET: "ok", "safe": "value"})
    outcome = evaluate(
        "equals",
        1,
        "value",
        True,
        {"expected": {"type": "string", "text": "value"}},
        path=("response.body", "safe"),
        ctx=ctx,
    )
    assert outcome.status == "passed"


def test_unrelated_source_stays_usable() -> None:
    sections = SourceRoots()
    sections.add_direct("response.status", 200)
    sections.add_tree("response.body", json_root(_json({SECRET: "ok"})))
    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(sections, [SECRET])
    outcome = evaluate(
        "equals",
        1,
        200,
        True,
        {"expected": {"type": "number", "text": "200"}},
        path=("response.status",),
        ctx=ctx,
    )
    assert outcome.status == "passed"


# —— 转义回显：判定必须看解码后的内容，与证据写出同源 ——


def _text_roots(raw: str) -> SourceRoots:
    roots = SourceRoots()
    roots.add_direct("response.text", raw)
    return roots


def test_escaped_echo_in_response_text_is_recognized() -> None:
    """`response.text` 是**原文**：回显写作 `\\u0061bc` 时原文串里没有 `abc`。

    只看字面子串的判定会漏掉它，于是对它求值既不报策略拒绝、又把明文写进结果，
    布尔结果本身成了逐位猜秘密的通道——报告里的正文遮住了，真假却还答着。
    """
    assert ("response.text",) in paths_containing(_text_roots('{"token":"\\u0061bc"}'), ["abc"])


def test_assertion_on_the_escaped_echo_in_response_text_is_refused() -> None:
    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(_text_roots('{"token":"\\u0061bc"}'), ["abc"])
    with pytest.raises(AssertionPolicyError):
        evaluate(
            "contains",
            1,
            '{"token":"\\u0061bc"}',
            True,
            {"expected": {"type": "string", "text": "abc"}},
            path=("response.text",),
            ctx=ctx,
        )


def test_escaped_secret_written_as_an_object_key_is_recognized() -> None:
    """键名被转义回显时同样要标为受保护，不能只认原文形态。"""
    found = paths_containing(_text_roots('{"\\u006f\\u0062\\u006a":"ok"}'), ["obj"])
    assert ("response.text",) in found


def test_an_unrelated_source_stays_usable_next_to_an_escaped_echo() -> None:
    """保护要精确到坐标：同一个来源被标为敏感，不牵连其他来源。"""
    roots = _text_roots('{"token":"\\u0061bc"}')
    roots.add_direct("response.status", 200)
    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(roots, ["abc"])
    outcome = evaluate(
        "equals",
        1,
        200,
        True,
        {"expected": {"type": "number", "text": "200"}},
        path=("response.status",),
        ctx=ctx,
    )
    assert outcome.status == "passed"


def test_plain_text_with_backslashes_is_not_marked_sensitive() -> None:
    """只有合法转义才解码：`C:\\temp` 不会因为一个反斜线被当成秘密的编码形态。

    解码后是 `C:<TAB>emp`，里面没有秘密；原文里也没有。判定与遮蔽都不该动它。
    """
    assert paths_containing(_text_roots(r"C:\temp\build"), ["abc"]) == set()


# —— 脱敏：键名同样要被遮蔽 ——


def test_redact_value_masks_secret_object_keys() -> None:
    assert redact_value({SECRET: "ok"}, [SECRET]) == {MASK: "ok"}
    assert redact_value({"outer": {SECRET: "ok"}}, [SECRET]) == {"outer": {MASK: "ok"}}
    assert redact_value([{SECRET: "ok"}], [SECRET]) == [{MASK: "ok"}]


def test_redact_pairs_masks_a_name_that_carries_the_secret() -> None:
    """响应头名称同样可能被目标用来承载凭据。"""
    assert redact_pairs([{"name": SECRET, "value": "ok"}], [SECRET]) == [
        {"name": MASK, "value": "ok"}
    ]


def test_redact_value_keeps_neighbouring_content() -> None:
    assert redact_value({"safe": "value", "n": 1}, [SECRET]) == {"safe": "value", "n": 1}
