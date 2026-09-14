"""无损值契约 ValueLiteral。

统一表示断言、变量、结果中的字面量，数值用十进制文本保存并以 Decimal
无损解析，避免 JavaScript Number 或二进制 float 的精度损失。

序列化形式（与执行契约第 7 节一致）：
- 数字：{"type": "number", "text": "9007199254740993"}
- 字符串：{"type": "string", "text": "abc"}
- 布尔：{"type": "boolean", "value": false}
- 空：{"type": "null"}
- 数组/对象：{"type": "json", "text": "...合法 JSON 原文..."}

字符串 "0" 与数字 0 不混淆；数字/整数由 text 精确区分。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .lossless_json import LosslessJSONError
from .lossless_json import loads as loads_lossless

_LITERAL_TYPES = ("number", "string", "boolean", "null", "json")


class ValueLiteralError(ValueError):
    """字面量构造或运算错误。"""


@dataclass(frozen=True)
class ValueLiteral:
    type: str  # number / string / boolean / null / json
    text: str | None = None  # number/string/json 的原始文本
    value: bool | None = None  # boolean 的值

    def to_dict(self) -> dict[str, Any]:
        if self.type == "boolean":
            return {"type": "boolean", "value": self.value}
        if self.type == "null":
            return {"type": "null"}
        if self.type == "json":
            return {"type": "json", "text": self.text}
        return {"type": self.type, "text": self.text}

    @classmethod
    def from_dict(cls, raw: Any) -> ValueLiteral:
        """按契约严格构造，不做猜测式转换。

        这是 API 边界与内核共用的入口：把 'false'（字符串）当作布尔真、把
        非数字文本当作数字、接受 NaN 这类非法 JSON，都会让用户在一个地方看到
        “保存成功”，在另一个地方得到与输入无关的结果。宁可在这里显式报错。
        """
        if not isinstance(raw, dict):
            raise ValueLiteralError("字面量必须是对象")
        if "type" not in raw:
            raise ValueLiteralError("字面量缺少 type 字段")
        kind = raw["type"]
        if kind not in _LITERAL_TYPES:
            raise ValueLiteralError(f"未知字面量类型：{kind!r}")
        if kind == "null":
            return cls(type="null")
        if kind == "boolean":
            if "value" not in raw:
                raise ValueLiteralError("boolean 类型必须有 value 字段")
            value = raw["value"]
            # 只接受真正的布尔；bool('false') 会得到 True，是典型的静默反转。
            if not isinstance(value, bool):
                raise ValueLiteralError(
                    f"boolean 的 value 必须是布尔值，收到 {type(value).__name__}"
                )
            return cls(type="boolean", value=value)
        text = raw.get("text")
        if not isinstance(text, str):
            raise ValueLiteralError(f"{kind} 类型必须有 text 文本")
        if kind == "number":
            cls._validate_number_text(text)
        elif kind == "json":
            cls._validate_json_text(text)
        return cls(type=kind, text=text)

    @staticmethod
    def _validate_number_text(text: str) -> None:
        try:
            number = Decimal(text)
        except InvalidOperation as error:
            raise ValueLiteralError(f"非法数字文本：{text!r}") from error
        # Decimal('NaN')／Decimal('Infinity') 能解析成功，却不是可用于比较的数值。
        if not number.is_finite():
            raise ValueLiteralError(f"数字必须是有限值：{text!r}")

    @staticmethod
    def _validate_json_text(text: str) -> None:
        try:
            loads_lossless(text)
        except LosslessJSONError as error:
            raise ValueLiteralError(f"非法 JSON 文本：{error}") from error

    @classmethod
    def of(cls, value: Any) -> ValueLiteral:
        """从 Python 原生值构造，字符串/数字/布尔/null/容器分别映射。"""
        if value is None:
            return cls(type="null")
        if isinstance(value, bool):
            return cls(type="boolean", value=value)
        if isinstance(value, str):
            return cls(type="string", text=value)
        if isinstance(value, (int, float, Decimal)):
            # 数字统一走十进制文本，避免 float 二次舍入。
            if isinstance(value, float):
                text = repr(value)
            else:
                text = str(value)
            return cls(type="number", text=text)
        if isinstance(value, (list, dict)):
            import json

            return cls(type="json", text=json.dumps(value, ensure_ascii=False))
        raise ValueLiteralError(f"无法表示的值类型：{type(value).__name__}")

    def as_decimal(self) -> Decimal:
        """数值类型解析为 Decimal，非数值报错。"""
        if self.type != "number":
            raise ValueLiteralError("当前值不是数字")
        assert self.text is not None
        try:
            return Decimal(self.text)
        except InvalidOperation as error:
            raise ValueLiteralError("非法数字文本") from error

    def as_python(self) -> Any:
        """转换为普通 Python 值；json 类型返回无损结构（数字为 NumberNode）。"""
        if self.type == "null":
            return None
        if self.type == "boolean":
            return self.value
        if self.type == "number":
            return self.as_decimal()
        if self.type == "string":
            return self.text
        if self.type == "json":
            assert self.text is not None
            # 必须走无损解析：json 字面量里的长整数与小数不能经二进制浮点变形。
            return loads_lossless(self.text)
        raise ValueLiteralError(f"未知类型 {self.type}")
