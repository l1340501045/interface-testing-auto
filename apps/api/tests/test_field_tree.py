"""字段树投影行为：长整数无损、重复键按出现次序、定位路径可用。

字段树是用户点选字段的生入口，它一旦经由浮点往返或丢失重复键语义，
后面再正确的断言内核也救不回来；因此这里验证的是投影本身，而不是渲染。
"""
from __future__ import annotations

import pytest

from app.kernel.field_tree import MAX_DEPTH, FieldTreeError, build_field_tree


def test_big_integer_keeps_lexical_text() -> None:
    """9007199254740993 必须以原文出现，不能变成相邻的浮点数。"""
    tree = build_field_tree('{"id": 9007199254740993, "next": 9007199254740994}')
    fields = {child["label"]: child for child in tree["children"]}
    assert fields["id"]["text"] == "9007199254740993"
    assert fields["id"]["type"] == "integer"
    assert fields["next"]["text"] == "9007199254740994"


def test_decimal_is_not_rounded() -> None:
    tree = build_field_tree('{"sum": 0.30000000000000004}')
    field = tree["children"][0]
    assert field["text"] == "0.30000000000000004"
    assert field["type"] == "number"


def test_nested_object_and_array_paths() -> None:
    tree = build_field_tree('{"data": {"items": [{"price": 5}]}}')
    data = tree["children"][0]
    items = data["children"][0]
    price = items["children"][0]["children"][0]
    assert data["selector"] == [{"kind": "key", "key": "data"}]
    assert items["selector"] == [{"kind": "key", "key": "data"}, {"kind": "key", "key": "items"}]
    assert price["selector"] == [
        {"kind": "key", "key": "data"},
        {"kind": "key", "key": "items"},
        {"kind": "index", "index": 0},
        {"kind": "key", "key": "price"},
    ]
    assert price["text"] == "5"


def test_repeated_query_keys_keep_occurrence_order() -> None:
    """重复查询参数不能折叠成 dict：同名两次必须各自可定位。"""
    text = '{"query": [{"name": "tag", "value": "a"}, {"name": "tag", "value": "b"}]}'
    tree = build_field_tree(text)
    query = tree["children"][0]
    assert [child["text"] for child in query["children"]] == ["a", "b"]
    assert query["children"][0]["selector"] == [
        {"kind": "key", "key": "query"},
        {"kind": "repeat_key", "key": "tag", "occurrence": 0},
    ]
    assert query["children"][1]["selector"][-1] == {
        "kind": "repeat_key",
        "key": "tag",
        "occurrence": 1,
    }
    assert [child["label"] for child in query["children"]] == ["tag[0]", "tag[1]"]


def test_null_and_boolean_and_string_types_are_distinct() -> None:
    tree = build_field_tree('{"a": null, "b": false, "c": "0", "d": 0}')
    kinds = {child["label"]: child["type"] for child in tree["children"]}
    assert kinds == {"a": "null", "b": "boolean", "c": "string", "d": "integer"}
    texts = {child["label"]: child["text"] for child in tree["children"]}
    assert texts["c"] == "0" and texts["d"] == "0"


def test_deep_nesting_is_truncated_with_reason() -> None:
    """超深结构明确标记截断，不静默丢字段让用户以为字段不存在。"""
    depth = MAX_DEPTH + 5
    text = "{\"a\":" * depth + "1" + "}" * depth
    tree = build_field_tree(text)
    node = tree
    while node["children"]:
        node = node["children"][0]
    assert node.get("truncated") == "达到最大嵌套深度"


def test_non_json_and_scalar_are_rejected() -> None:
    with pytest.raises(FieldTreeError):
        build_field_tree("not json at all")
    with pytest.raises(FieldTreeError):
        build_field_tree("123")
    assert build_field_tree("   ") is None
