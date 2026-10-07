"""变量解析：普通变量（项目级/环境级）按 ValueLiteral 契约解析。

变量在发送前解析，未定义变量显式失败，不做静默替换。数值保留十进制
文本，字符串与数字不混淆；秘密不允许通过普通变量传递（调用方负责过滤）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .lossless_json import NumberNode
from .valueliteral import ValueLiteral, ValueLiteralError


class VariableResolutionError(ValueError):
    """变量未定义或类型错误。"""


# 变量名可以是中文、字母、数字、下划线、点与短横线等任意非花括号、非空白字符：
# 产品面向中文用户，环境变量名（如“租户”）必须可用；限制 ASCII 标识符会让界面
# 上的合法名字在发送前才失败。花括号与空白仍然排除，避免模板嵌套歧义。
_VAR_PATTERN = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")


@dataclass(frozen=True)
class VariableReference:
    """原模板中的一次变量引用及 Python 字符索引范围。"""

    name: str
    start: int
    end: int
    text: str


def variable_references(text: str) -> list[VariableReference]:
    """按原文顺序返回引用；定位转换由调用边界决定。"""
    return [
        VariableReference(
            name=match.group(1), start=match.start(), end=match.end(), text=match.group(0)
        )
        for match in _VAR_PATTERN.finditer(text)
    ]


def variable_reference_for_name(name: str) -> str | None:
    """返回能按旧扁平语法唯一还原原名的引用；不可表达则返回 None。"""
    reference = "{{" + name + "}}"
    matches = variable_references(reference)
    if (
        len(matches) != 1
        or matches[0].start != 0
        or matches[0].end != len(reference)
        or matches[0].name != name
    ):
        return None
    return reference


def has_variable_reference(text: str) -> bool:
    """文本里是否出现变量引用。

    正文绑定按它决定走协议路径还是原文直发；必须与 `VariableResolver` 用同一个
    模式，否则会出现“判定为无变量、实际含变量”的正文被原样发出去。
    """
    return _VAR_PATTERN.search(text) is not None


def variable_reference_count(text: str) -> int:
    """文本里变量引用的个数，用于判断引用是否被协议解码改写过。"""
    return len(_VAR_PATTERN.findall(text))


class VariableResolver:
    """持有变量表（name -> ValueLiteral），解析模板文本或整体值。"""

    def __init__(self, variables: dict[str, ValueLiteral]):
        self._variables = variables

    def has(self, name: str) -> bool:
        return name in self._variables

    def _literal_text(self, name: str) -> str:
        literal = self._variables[name]
        if literal.type in ("string", "number", "json"):
            return literal.text or ""
        if literal.type == "boolean":
            return "true" if literal.value else "false"
        if literal.type == "null":
            return ""
        return ""

    def resolve_text(self, template: str) -> str:
        """文本内嵌变量替换，未定义抛错。"""
        def repl(match: re.Match) -> str:
            name = match.group(1)
            if name not in self._variables:
                raise VariableResolutionError(f"未定义变量：{name}")
            return self._literal_text(name)

        return _VAR_PATTERN.sub(repl, template)

    def resolve_value(self, template: str) -> Any:
        """整体值解析：若模板恰为单个变量引用则保留其类型，否则按文本。"""
        literal = self._whole_variable(template)
        if literal is None:
            return self.resolve_text(template)
        return literal.as_python()

    def resolve_structured(self, template: str) -> Any:
        """结构化正文里的字段值：整体恰为一个变量引用时保留其类型。

        与 `resolve_value` 是同一条整体值判定，差别只在数字：这里返回**无损数字
        节点**，让 JSON 序列化按十进制原文输出。`as_python()` 给出的是 Decimal，
        而 `str(Decimal('1E+2'))` 是 `1E+2`——那不是合法 JSON，直接写进正文会让
        用户拿到一份自己没写过的残缺正文。

        嵌在更长文本里的引用仍是字符串：`"前缀{{变量}}后缀"` 不会因为变量是数字
        就变成别的类型，否则用户的模板会随变量取值改变 JSON 结构。
        """
        literal = self._whole_variable(template)
        if literal is None:
            return self.resolve_text(template)
        if literal.type == "number":
            # `format(..., 'f')` 一律给出定点写法：Decimal('1E+2') → 100、
            # Decimal('1_0') → 10，等值且始终是合法 JSON 数字，全程不经过 float。
            return NumberNode(text=format(literal.as_decimal(), "f"))
        return literal.as_python()

    def resolve_literal(self, name: str) -> ValueLiteral:
        if name not in self._variables:
            raise VariableResolutionError(f"未定义变量：{name}")
        return self._variables[name]

    def _whole_variable(self, template: str) -> ValueLiteral | None:
        """模板整体恰为一个变量引用时返回其字面量，否则 None；未定义即抛错。"""
        match = _VAR_PATTERN.fullmatch(template.strip())
        if match is None:
            return None
        return self.resolve_literal(match.group(1))


def build_resolver(variable_list: list[dict]) -> VariableResolver:
    """从 API 传入的变量数组构建解析器。每项为 {name, value: ValueLiteral}。"""
    table: dict[str, ValueLiteral] = {}
    for item in variable_list:
        name = item.get("name")
        raw_value = item.get("value")
        if not isinstance(name, str) or not name:
            raise VariableResolutionError("变量名无效")
        try:
            table[name] = ValueLiteral.from_dict(raw_value)
        except ValueLiteralError as error:
            raise VariableResolutionError(f"变量 {name} 值无效：{error}") from error
    return VariableResolver(table)
