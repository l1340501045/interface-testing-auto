"""无损 JSON 解析：数字保留原始词法文本，用 Decimal 无损求值。

普通 json.loads 会把 9007199254740993 变成 float 丢精度，或把小数做
二进制舍入。这里通过 parse_int/parse_float 回调保留每个数字的原文，
求值时用 Decimal，序列化时按原文输出，避免有损往返。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class LosslessJSONError(ValueError):
    """JSON 解析或取值错误。"""


@dataclass(frozen=True)
class NumberNode:
    """无损数字节点：保留原始词法，惰性解析 Decimal。"""

    text: str

    def decimal(self) -> Decimal:
        try:
            return Decimal(self.text)
        except InvalidOperation as error:
            raise LosslessJSONError(f"非法数字：{self.text!r}") from error

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"NumberNode({self.text!r})"


def _parse_int(text: str) -> NumberNode:
    return NumberNode(text=text)


def _parse_float(text: str) -> NumberNode:
    return NumberNode(text=text)


def _reject_constant(text: str) -> Any:
    # Python 的 json 默认接受 NaN／Infinity／-Infinity 这类非标准字面量。它们是
    # “能用浮点表示”的假象，既破坏十进制无损契约，也会让后续比较得到无意义结果；
    # 按契约直接拒绝，而不是让它们混进字段树。
    raise LosslessJSONError(f"JSON 文本包含非标准数字字面量：{text}")


def loads(text: str) -> Any:
    """解析 JSON 文本为无损结构（数字为 NumberNode，其余为原生类型）。"""
    try:
        return json.loads(
            text,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise LosslessJSONError(f"JSON 解析失败：{error.msg}") from error


def to_python(value: Any) -> Any:
    """把无损结构转为普通 Python 值（数字用 Decimal 表示精度）。"""
    if isinstance(value, NumberNode):
        return value.decimal()
    if isinstance(value, list):
        return [to_python(item) for item in value]
    if isinstance(value, dict):
        return {key: to_python(val) for key, val in value.items()}
    return value


def dumps(value: Any) -> str:
    """把无损结构序列化回 JSON 文本，数字按原始词法输出。

    为什么不用 `json.dumps`：它不认识 `NumberNode`，要么抛错要么把长整数降级。
    这种“先解析、脱敏、再写回”的路径一旦丢精度，脱敏本身就把无损契约破坏了。
    因此这里手写递归：只有数字节点走原文直出，其余交给 `json.dumps` 负责字符串
    转义（引号、反斜线、控制字符都按标准转义）。
    """
    return _dump(value)


def _dump(value: Any) -> str:
    if isinstance(value, NumberNode):
        return value.text
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_dump(item) for item in value) + "]"
    if isinstance(value, dict):
        members = (
            f"{json.dumps(str(key), ensure_ascii=False)}:{_dump(val)}"
            for key, val in value.items()
        )
        return "{" + ",".join(members) + "}"
    raise LosslessJSONError(f"不能序列化的取值类型：{type(value).__name__}")
