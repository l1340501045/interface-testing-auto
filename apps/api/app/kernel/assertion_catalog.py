"""断言类型目录：已实现能力的单一来源。

前端与后端都从这里读取类型、适用数据类型、参数表单与中文文案，避免维护
两套相互矛盾的定义。目录只暴露已经实现并有行为测试的操作符；阶段 2 的
量词、组合、正则、JSON Schema 不在此列出。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import assertion_operators  # noqa: F401  导入即注册全部已实现操作符
from .assertions import list_operators
from .valueliteral import ValueLiteral, ValueLiteralError

_VALUE_TYPES = ["string", "number", "integer", "boolean", "null", "object", "array"]

_NUMBER = {"control": "number", "type": "number"}
_NUMBER_PAIR = {
    "min": {"control": "number", "type": "number", "label": "最小值"},
    "max": {"control": "number", "type": "number", "label": "最大值"},
}


@dataclass(frozen=True)
class AssertionType:
    id: str
    label: str
    group: str
    applies_to: list[str]
    params_schema: dict[str, Any]
    summary: str
    operator_version: int = 1


_TYPES: list[AssertionType] = [
    AssertionType("equals", "等于", "值与集合", _VALUE_TYPES,
                  {"expected": {"control": "value", "type": "any", "label": "期望值"}},
                  "值严格等于期望值"),
    AssertionType("not_equals", "不等于", "值与集合", _VALUE_TYPES,
                  {"expected": {"control": "value", "type": "any", "label": "期望值"}},
                  "值不等于期望值"),
    AssertionType("in_set", "属于", "值与集合", _VALUE_TYPES,
                  {"values": {"control": "value_list", "type": "any", "label": "候选值"}},
                  "值属于给定集合之一"),
    AssertionType("not_in_set", "不属于", "值与集合", _VALUE_TYPES,
                  {"values": {"control": "value_list", "type": "any", "label": "候选值"}},
                  "值不属于给定集合"),
    AssertionType("greater_than", "大于", "数值比较", ["number", "integer"],
                  {"expected": _NUMBER}, "值大于边界"),
    AssertionType("greater_or_equal", "大于等于", "数值比较", ["number", "integer"],
                  {"expected": _NUMBER}, "值大于或等于边界"),
    AssertionType("less_than", "小于", "数值比较", ["number", "integer"],
                  {"expected": _NUMBER}, "值小于边界"),
    AssertionType("less_or_equal", "小于等于", "数值比较", ["number", "integer"],
                  {"expected": _NUMBER}, "值小于或等于边界"),
    AssertionType("range", "区间", "数值比较", ["number", "integer"],
                  {**_NUMBER_PAIR,
                   "include_min": {"control": "switch", "type": "boolean", "label": "包含最小值", "default": False},
                   "include_max": {"control": "switch", "type": "boolean", "label": "包含最大值", "default": False}},
                  "值在区间内，默认不含边界"),
    AssertionType("not_range", "不在区间", "数值比较", ["number", "integer"],
                  {**_NUMBER_PAIR,
                   "include_min": {"control": "switch", "type": "boolean", "label": "包含最小值", "default": False},
                   "include_max": {"control": "switch", "type": "boolean", "label": "包含最大值", "default": False}},
                  "值不在区间内，默认不含边界"),
    AssertionType("exists", "存在", "字段存在", _VALUE_TYPES, {},
                  "字段存在即可通过，值为 null 也算存在"),
    AssertionType("not_exists", "不存在", "字段存在", _VALUE_TYPES, {},
                  "字段缺失才通过"),
    AssertionType("is_null", "为 null", "空值", _VALUE_TYPES, {}, "字段值为 null"),
    AssertionType("not_null", "非 null", "空值", _VALUE_TYPES, {}, "字段值不是 null"),
    AssertionType("is_empty", "为空", "空值", ["string", "object", "array"], {},
                  "字符串、数组或对象为空；字段缺失视为为空"),
    AssertionType("not_empty", "非空", "空值", ["string", "object", "array"], {},
                  "字符串、数组或对象非空；缺失、null 与空值均不通过"),
    AssertionType("is_type", "类型", "类型", _VALUE_TYPES,
                  {"type": {"control": "select", "type": "string", "label": "期望类型",
                            "options": ["string", "number", "integer", "boolean", "object", "array", "null"]}},
                  "按解析后的类型判断，不做隐式转换"),
    AssertionType("contains", "包含", "字符串", ["string"],
                  {"expected": {"control": "value", "type": "string", "label": "子串"}},
                  "字符串包含指定子串，区分大小写"),
    AssertionType("not_contains", "不包含", "字符串", ["string"],
                  {"expected": {"control": "value", "type": "string", "label": "子串"}},
                  "字符串不包含指定子串"),
    AssertionType("starts_with", "开头是", "字符串", ["string"],
                  {"expected": {"control": "value", "type": "string", "label": "前缀"}},
                  "字符串以指定前缀开头"),
    AssertionType("ends_with", "结尾是", "字符串", ["string"],
                  {"expected": {"control": "value", "type": "string", "label": "后缀"}},
                  "字符串以指定后缀结尾"),
    AssertionType("length_equals", "长度等于", "长度与数量", ["string", "array", "object"],
                  {"expected": _NUMBER}, "长度或元素数量等于指定值"),
    AssertionType("length_range", "长度区间", "长度与数量", ["string", "array", "object"],
                  {**_NUMBER_PAIR,
                   "include_min": {"control": "switch", "type": "boolean", "label": "包含最小值", "default": False},
                   "include_max": {"control": "switch", "type": "boolean", "label": "包含最大值", "default": False}},
                  "长度或元素数量在区间内，默认不含边界"),
]

_BY_ID = {item.id: item for item in _TYPES}

# 检查来源：入参在发送前求值，出参/响应在收到响应后求值。
TARGET_SOURCES = {
    "request.path": "pre_request",
    "request.query": "pre_request",
    "request.header": "pre_request",
    "request.body": "pre_request",
    "request.text": "pre_request",
    "response.status": "post_response",
    "response.elapsed": "post_response",
    "response.header": "post_response",
    "response.cookie": "post_response",
    "response.body": "post_response",
    "response.text": "post_response",
}

# 直接取单值、不接受字段定位的来源。对它们配置定位步骤是配置错误，必须在保存／发布
# 时拒绝：这类配置能通过校验、却在执行时由取值环节抛错，而执行器取值发生在断言
# 异常处理之外，错误会一路冒到 worker 的兜底捕获——那里只记日志不写终态，运行于是
# 永远停在“可被重新领取”，表现为工作项反复被领取却永不结束。规则放在这里是为了让
# 校验层和取值层共用同一份来源分类，不再各写一套。
DIRECT_SOURCES = {
    "request.path",
    "request.text",
    "response.status",
    "response.elapsed",
    "response.text",
}


def accepts_selector(target_source: str) -> bool:
    """该检查来源是否接受字段定位步骤。"""
    return target_source not in DIRECT_SOURCES


class AssertionCatalogError(ValueError):
    """断言目录查询或校验错误。"""


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "group": item.group,
            "applies_to": item.applies_to,
            "params_schema": item.params_schema,
            "summary": item.summary,
            "operator_version": item.operator_version,
        }
        for item in _TYPES
    ]


def get_type(type_id: str) -> AssertionType:
    item = _BY_ID.get(type_id)
    if item is None:
        raise AssertionCatalogError(f"未实现的断言类型：{type_id}")
    return item


def phase_of(target_source: str) -> str:
    phase = TARGET_SOURCES.get(target_source)
    if phase is None:
        raise AssertionCatalogError(f"未知的检查来源：{target_source}")
    return phase


def _check_param_value(label: str, name: str, spec: dict[str, Any], raw: Any) -> None:
    """按目录声明的参数类型校验取值。

    数值参数必须符合 ValueLiteral 契约，因此这里复用契约校验器，而不是就地再解析
    一次文本。重复实现会漏掉契约本身已有的约束：``Decimal('NaN')`` 能解析成功，
    却不是可用于比较的数值。就地实现放过了它，于是这样的配置在保存和发布时都被
    接受，直到真正执行才变成一条运行时“类型错误”——用户看到的却是业务失败。
    同一条规则只保留一个来源，边界与内核才不会一强一弱。
    """
    declared = spec.get("type")
    if declared == "number":
        try:
            literal = ValueLiteral.from_dict(raw)
        except ValueLiteralError as error:
            raise AssertionCatalogError(
                f"断言 {label} 的参数 {name} 不是合法数字：{error}"
            ) from error
        if literal.type != "number":
            raise AssertionCatalogError(
                f"断言 {label} 的参数 {name} 必须按数字字面量提供，收到 {literal.type}"
            )


def validate_parameters(type_id: str, parameters: dict[str, Any]) -> None:
    """按目录声明的参数校验，缺参、多余参数或取值非法都视为配置错误。"""
    item = get_type(type_id)
    allowed = set(item.params_schema)
    extra = set(parameters) - allowed
    if extra:
        raise AssertionCatalogError(f"断言 {item.label} 不支持参数：{'、'.join(sorted(extra))}")
    for name, spec in item.params_schema.items():
        if name not in parameters:
            if spec.get("control") == "switch":
                continue
            raise AssertionCatalogError(f"断言 {item.label} 缺少参数：{spec.get('label', name)}")
        _check_param_value(item.label, name, spec, parameters[name])


def assert_registry_complete() -> None:
    """目录与注册表必须一一对应，避免暴露未实现能力或漏掉已实现能力。"""
    registered = set(list_operators())
    catalogued = set(_BY_ID)
    if registered != catalogued:
        missing = registered - catalogued
        extra = catalogued - registered
        raise AssertionCatalogError(
            f"断言目录与实现不一致：缺少实现 {sorted(extra)}，缺少目录 {sorted(missing)}"
        )
