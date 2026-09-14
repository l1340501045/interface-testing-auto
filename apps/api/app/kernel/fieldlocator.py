"""字段定位：在无损 JSON 树上按稳定路径取值，不使用 eval。

定位路径是步骤数组，每步为：
- {"kind": "key", "key": "字段名"}           —— 对象字段（真实字段名，可含点/括号/空格）
- {"kind": "index", "index": 0}              —— 数组下标
- {"kind": "repeat_key", "key": "x", "occurrence": 0} —— 同名重复项（header/query 数组）

返回 (found, value)：found=False 表示字段缺失（区别于值为 null）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lossless_json import NumberNode


class FieldLocatorError(ValueError):
    """定位配置或取值错误。"""


@dataclass(frozen=True)
class LocateResult:
    found: bool
    value: Any = None


def _get_key(obj: dict, key: str) -> LocateResult:
    if not isinstance(obj, dict):
        raise FieldLocatorError("定位键要求当前节点是对象")
    if key not in obj:
        return LocateResult(found=False)
    return LocateResult(found=True, value=obj[key])


def _get_index(arr: list, index: int) -> LocateResult:
    if not isinstance(arr, list):
        raise FieldLocatorError("定位下标要求当前节点是数组")
    if index < 0 or index >= len(arr):
        return LocateResult(found=False)
    return LocateResult(found=True, value=arr[index])


def _get_repeat_key(arr: list, key: str, occurrence: int) -> LocateResult:
    """在同名重复项数组（元素含 name/value 或 [name,value]）中按序号取值。"""
    if not isinstance(arr, list):
        raise FieldLocatorError("重复键定位要求当前节点是数组")
    matches: list[Any] = []
    for item in arr:
        if isinstance(item, dict):
            name = item.get("name")
            value = item.get("value")
            if name == key:
                matches.append(value)
        elif isinstance(item, list) and len(item) >= 2:
            name, value = item[0], item[1]
            if name == key:
                matches.append(value)
    if occurrence < 0 or occurrence >= len(matches):
        return LocateResult(found=False)
    return LocateResult(found=True, value=matches[occurrence])


def locate(root: Any, path: list[dict]) -> LocateResult:
    """沿 path 步骤数组逐级取值。"""
    current = root
    for step in path:
        if not isinstance(step, dict):
            raise FieldLocatorError("定位步骤必须是对象")
        kind = step.get("kind")
        if kind == "key":
            result = _get_key(current, step.get("key"))
        elif kind == "index":
            result = _get_index(current, int(step.get("index")))
        elif kind == "repeat_key":
            result = _get_repeat_key(current, step.get("key"), int(step.get("occurrence", 0)))
        else:
            raise FieldLocatorError(f"未知定位步骤：{kind!r}")
        if not result.found:
            return result
        current = result.value
    return LocateResult(found=True, value=current)


def contains_number(value: Any) -> bool:
    """递归判断值（或其任意后代）是否包含数字节点，供敏感标签检测用。"""
    if isinstance(value, NumberNode):
        return True
    if isinstance(value, list):
        return any(contains_number(item) for item in value)
    if isinstance(value, dict):
        return any(contains_number(val) for val in value.values())
    return False
