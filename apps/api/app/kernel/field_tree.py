"""字段树投影：把无损 JSON 原文展开为前端可渲染的字段树。

前端若直接用 `JSON.parse` 展开字段，`9007199254740993` 会先变成 IEEE-754
双精度再回到屏幕，用户看到和选中的是相邻整数，整条无损契约在最后一个环节失效。
因此字段树统一由后端用无损解析生成：数字只以原始十进制文本下发，前端负责展示
与选择，不自行解析正文。

树节点同时携带定位路径（selector），用户点选字段即可得到与执行内核一致的
`{"kind": "key"}`／`{"kind": "index"}`／`{"kind": "repeat_key"}` 步骤，
不需要手写路径，也不会出现界面显示名与内部定位不一致。
"""
from __future__ import annotations

from typing import Any

from .fieldlocator import FieldLocatorError
from .lossless_json import LosslessJSONError, NumberNode, loads

# 与执行契约的资源上限同量级：字段树是展示用途，超过上限时截断并明确告知，
# 不做静默截取（静默截断会让用户以为字段不存在）。
MAX_DEPTH = 64
MAX_NODES = 5000


class FieldTreeError(ValueError):
    """字段树构建错误，通常是正文不是合法 JSON。"""


def _number_kind(text: str) -> str:
    """按词法区分整数与小数，不用 float 判断，避免精度问题影响类型判定。"""
    lowered = text.lower()
    return "number" if ("." in text or "e" in lowered) else "integer"


def _is_pair(item: Any) -> bool:
    return isinstance(item, dict) and set(item) == {"name", "value"} and isinstance(item["name"], str)


def _pairs_array(items: list) -> list[dict[str, str]] | None:
    """全部元素都是 {name, value} 时按重复键数组处理；否则返回 None。"""
    if not items:
        return None
    if not all(_is_pair(item) for item in items):
        return None
    return [{"name": item["name"], "value": _scalar_text(item["value"])} for item in items]


def _scalar_text(value: Any) -> str:
    if isinstance(value, NumberNode):
        return value.text
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, NumberNode):
        return _number_kind(value.text)
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _node(value: Any, selector: list[dict], label: str) -> dict[str, Any]:
    return {
        "label": label,
        "type": _type_of(value),
        "text": _scalar_text(value) if not isinstance(value, (list, dict)) else "",
        "selector": selector,
        "children": [],
    }


def _build(value: Any, selector: list[dict], label: str, depth: int, budget: list[int]) -> dict:
    node = _node(value, selector, label)
    budget[0] -= 1
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            node["truncated"] = "达到最大嵌套深度"
            return node
        for key, item in value.items():
            if budget[0] <= 0:
                node["truncated"] = "字段数超过上限"
                break
            node["children"].append(
                _build(item, [*selector, {"kind": "key", "key": key}], key, depth + 1, budget)
            )
    elif isinstance(value, list):
        if depth >= MAX_DEPTH:
            node["truncated"] = "达到最大嵌套深度"
            return node
        pairs = _pairs_array(value)
        if pairs is not None:
            # 重复键数组（查询参数、请求头、Cookie）按出现次序展开，
            # 用 repeat_key 定位，才能区分同名参数的第一次与第二次出现。
            occurrences: dict[str, int] = {}
            for entry in pairs:
                index = occurrences.get(entry["name"], 0)
                occurrences[entry["name"]] = index + 1
                if budget[0] <= 0:
                    node["truncated"] = "字段数超过上限"
                    break
                child = _scalar_node(
                    entry["value"],
                    [*selector, {"kind": "repeat_key", "key": entry["name"], "occurrence": index}],
                    f"{entry['name']}[{index}]",
                )
                node["children"].append(child)
            return node
        for index, item in enumerate(value):
            if budget[0] <= 0:
                node["truncated"] = "字段数超过上限"
                break
            node["children"].append(
                _build(item, [*selector, {"kind": "index", "index": index}], f"[{index}]", depth + 1, budget)
            )
    return node


def _scalar_node(text: str, selector: list[dict], label: str) -> dict[str, Any]:
    try:
        value = loads(text)
    except LosslessJSONError:
        value = text
    return _node(value, selector, label)


def build_field_tree(text: str) -> dict[str, Any] | None:
    """从 JSON 原文构建字段树；空文本或非 JSON 返回 None。"""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = loads(stripped)
    except LosslessJSONError as error:
        raise FieldTreeError(f"正文不是合法 JSON：{error}") from error
    if not isinstance(parsed, (dict, list)):
        raise FieldTreeError("正文顶层需为对象或数组，标量无法展开字段树")
    try:
        return _build(parsed, [], "$", 0, [MAX_NODES])
    except FieldLocatorError as error:  # pragma: no cover - 防御性，构建过程不再定位
        raise FieldTreeError(str(error)) from error


__all__ = ["MAX_DEPTH", "MAX_NODES", "FieldTreeError", "build_field_tree"]
