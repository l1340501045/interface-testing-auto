"""断言配置校验：用例草稿与发布版本共用同一份校验。

每条配置包含稳定标识、检查来源与定位、断言类型、参数、严重级别与顺序。
校验同时用于保存草稿、发布快照和试算入口，避免前端与后端出现两套规则。
"""
from __future__ import annotations

import uuid
from typing import Any

from .assertion_catalog import (
    AssertionCatalogError,
    accepts_selector,
    get_type,
    phase_of,
    validate_parameters,
)

_STEP_KINDS = {"key", "index", "repeat_key"}
_SEVERITIES = {"error", "warning"}
_MAX_ASSERTIONS = 100


class AssertionSpecError(ValueError):
    """断言配置非法。"""


def _step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise AssertionSpecError("定位步骤必须是对象")
    kind = step.get("kind")
    if kind not in _STEP_KINDS:
        raise AssertionSpecError(f"未知定位步骤：{kind!r}")
    if kind == "key":
        key = step.get("key")
        if not isinstance(key, str) or not key:
            raise AssertionSpecError("字段定位缺少字段名")
        return {"kind": "key", "key": key}
    if kind == "index":
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise AssertionSpecError("数组下标必须是非负整数")
        return {"kind": "index", "index": index}
    occurrence = step.get("occurrence", 0)
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
        raise AssertionSpecError("重复键序号必须是非负整数")
    key = step.get("key")
    if not isinstance(key, str) or not key:
        raise AssertionSpecError("重复键定位缺少字段名")
    return {"kind": "repeat_key", "key": key, "occurrence": occurrence}


def validate_assertion(raw: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AssertionSpecError("断言配置必须是对象")
    allowed = {
        "id", "target_source", "selector", "type", "parameters",
        "compare_as", "severity", "enabled", "sort_order",
    }
    extra = set(raw) - allowed
    if extra:
        raise AssertionSpecError(f"断言配置包含未知字段：{'、'.join(sorted(extra))}")

    type_id = raw.get("type")
    if not isinstance(type_id, str) or not type_id:
        raise AssertionSpecError("断言配置缺少类型")
    try:
        get_type(type_id)
    except AssertionCatalogError as error:
        raise AssertionSpecError(str(error)) from error

    target_source = raw.get("target_source")
    if not isinstance(target_source, str):
        raise AssertionSpecError("断言配置缺少检查来源")
    try:
        phase_of(target_source)
    except AssertionCatalogError as error:
        raise AssertionSpecError(str(error)) from error

    selector_raw = raw.get("selector", [])
    if not isinstance(selector_raw, list):
        raise AssertionSpecError("字段定位必须是数组")
    selector = [_step(step) for step in selector_raw]
    # 状态码、耗时这类来源只取单值，配上定位步骤就是配置错误。必须在这里拒绝：
    # 取值层对“直接来源 + 定位步骤”会抛错，而执行器取值不在断言的异常处理之内，
    # 错误会冒到 worker 兜底捕获（只记日志、不写终态），运行于是永不结束。
    if selector and not accepts_selector(target_source):
        raise AssertionSpecError(f"检查来源 {target_source} 不接受字段定位，请移除定位步骤")

    parameters = raw.get("parameters", {})
    if not isinstance(parameters, dict):
        raise AssertionSpecError("断言参数必须是对象")
    try:
        validate_parameters(type_id, parameters)
    except AssertionCatalogError as error:
        raise AssertionSpecError(str(error)) from error

    compare_as = raw.get("compare_as")
    if compare_as is not None and compare_as not in ("number", "integer"):
        raise AssertionSpecError("compare_as 只能是 number 或 integer")

    severity = raw.get("severity", "error")
    if severity not in _SEVERITIES:
        raise AssertionSpecError("严重级别只能是 error 或 warning")

    assertion_id = raw.get("id")
    if assertion_id is None:
        assertion_id = uuid.uuid4().hex
    elif not isinstance(assertion_id, str) or not assertion_id or len(assertion_id) > 64:
        raise AssertionSpecError("断言标识无效")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AssertionSpecError("enabled 必须是布尔值")

    sort_order = raw.get("sort_order", index)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise AssertionSpecError("sort_order 必须是整数")

    return {
        "id": assertion_id,
        "target_source": target_source,
        "selector": selector,
        "type": type_id,
        "parameters": parameters,
        "compare_as": compare_as,
        "severity": severity,
        "enabled": enabled,
        "sort_order": sort_order,
    }


def validate_assertions(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise AssertionSpecError("断言配置必须是数组")
    if len(items) > _MAX_ASSERTIONS:
        raise AssertionSpecError(f"单个用例最多 {_MAX_ASSERTIONS} 条断言")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        item = validate_assertion(raw, index)
        if item["id"] in seen:
            raise AssertionSpecError(f"断言标识重复：{item['id']}")
        seen.add(item["id"])
        result.append(item)
    return result


__all__ = [
    "AssertionSpecError",
    "validate_assertion",
    "validate_assertions",
]
