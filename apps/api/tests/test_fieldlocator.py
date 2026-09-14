"""字段定位测试：对象字段、数组下标、重复键，以及缺失与 null 的区分。"""
from __future__ import annotations

from app.kernel.fieldlocator import locate
from app.kernel.lossless_json import loads


def test_locate_nested_key() -> None:
    tree = loads('{"data": {"name": "abc"}}')
    result = locate(tree, [{"kind": "key", "key": "data"}, {"kind": "key", "key": "name"}])
    assert result.found and result.value == "abc"


def test_locate_array_index() -> None:
    tree = loads('{"items": [{"id": 1}, {"id": 2}]}')
    result = locate(tree, [{"kind": "key", "key": "items"}, {"kind": "index", "index": 1}])
    assert result.found
    assert result.value["id"].text == "2", "下标 1 指向第二个元素，数字保留原文"


def test_locate_missing_distinct_from_null() -> None:
    tree = loads('{"a": null}')
    missing = locate(tree, [{"kind": "key", "key": "b"}])
    null_val = locate(tree, [{"kind": "key", "key": "a"}])
    assert missing.found is False
    assert null_val.found is True and null_val.value is None


def test_locate_key_with_dots() -> None:
    tree = loads('{"a.b": 1}')
    result = locate(tree, [{"kind": "key", "key": "a.b"}])
    assert result.found and result.value.text == "1"


def test_locate_repeat_key() -> None:
    # 重复查询参数以 [name,value] 形式表示。
    tree = [["a", "1"], ["a", "2"]]
    first = locate(tree, [{"kind": "repeat_key", "key": "a", "occurrence": 0}])
    second = locate(tree, [{"kind": "repeat_key", "key": "a", "occurrence": 1}])
    assert first.value == "1"
    assert second.value == "2"
