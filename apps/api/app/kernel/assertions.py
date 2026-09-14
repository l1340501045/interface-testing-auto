"""断言求值：统一公共方法注册表与求值调度。

每条断言配置（type + operator_version + parameters + target_source + selector）
经同一注册表求值。求值前先校验配置与敏感性，缺失作为独立标志；
数值按十进制文本无损比较，区间默认开区间。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .lossless_json import NumberNode
from .valueliteral import ValueLiteral, ValueLiteralError


class AssertionConfigError(ValueError):
    """断言配置无效（区别于业务失败）。

    规则本身写错（缺参数、参数多余、区间 min >= max、类型名未知）属于配置错误，
    调用方应拒绝这条配置；求值前的参数校验也应给出同一结果。
    """


class AssertionValueTypeError(ValueError):
    """实际值的类型不满足该断言（数据问题，不是配置问题）。

    例如对字符串字段配了数值区间、对数字配了字符串包含。这必须记录为
    status="error" 并保留期望与实际，而不是当成配置拒绝，也不能算通过。
    它与 AssertionConfigError 分开，正是为了避免把数据问题误报成用户配错。
    """


class AssertionPolicyError(ValueError):
    """策略拒绝（如敏感值参与普通断言）。"""


@dataclass(frozen=True)
class AssertionOutcome:
    status: str  # passed / failed / error / skipped
    reason_code: str | None = None
    expected: Any = None
    actual: Any = None
    message: str = ""


@dataclass
class AssertionContext:
    """求值上下文：敏感路径判定与资源上限。

    路径是“取值来源 + 定位步骤”的元组，与执行器实际提取的位置同一坐标，例如
    ("request.header", "Authorization")。只有精确匹配会漏掉两类同样能反推秘密的
    定位：包含敏感字段的祖先（对整个请求头对象求值，实际值里就有 Authorization）
    和敏感容器的后代（对敏感对象内部的子字段求值）。因此按前缀关系判定。
    """

    sensitive_paths: set[tuple] = field(default_factory=set)
    max_assertions: int = 100

    def is_sensitive_path(self, path: tuple) -> bool:
        for sensitive in self.sensitive_paths:
            shorter, longer = (
                (path, sensitive) if len(path) <= len(sensitive) else (sensitive, path)
            )
            if longer[: len(shorter)] == shorter:
                return True
        return False


# 操作符实现签名：(value, found, params, ctx) -> AssertionOutcome
Operator = Callable[[Any, bool, dict, AssertionContext], AssertionOutcome]

_REGISTRY: dict[str, dict[int, Operator]] = {}


def register(operator_id: str, version: int, func: Operator) -> None:
    _REGISTRY.setdefault(operator_id, {})[version] = func


def list_operators() -> list[str]:
    return sorted(_REGISTRY.keys())


def _num(value: Any) -> NumberNode | None:
    """取数值节点；value 为 NumberNode 直接返回，ValueLiteral/str 尝试转。"""
    if isinstance(value, NumberNode):
        return value
    if isinstance(value, ValueLiteral):
        return NumberNode(text=value.text) if value.type == "number" else None
    if isinstance(value, (int, float)):
        return NumberNode(text=str(value))
    return None


def _param_number(params: dict, key: str) -> NumberNode:
    """从参数取数值（十进制文本），缺省或非法抛配置错误。"""
    raw = params.get(key)
    if raw is None:
        raise AssertionConfigError(f"缺少数值参数 {key}")
    text = raw.get("text") if isinstance(raw, dict) else str(raw)
    if not isinstance(text, str):
        raise AssertionConfigError(f"数值参数 {key} 必须是十进制文本")
    return NumberNode(text=text)


def evaluate(
    operator_id: str,
    operator_version: int,
    value: Any,
    found: bool,
    params: dict,
    path: tuple = (),
    ctx: AssertionContext | None = None,
) -> AssertionOutcome:
    """求值入口：校验配置与敏感性后调用对应操作符版本。"""
    ctx = ctx or AssertionContext()
    # 敏感性判定先于比较与证据构造：先算出结果再遮盖，布尔结果本身仍会泄露秘密。
    # 不能写 `if path`——空路径表示“对整个取值来源求值”，正是最需要拦截的用例。
    if ctx.is_sensitive_path(path):
        raise AssertionPolicyError("受保护值（或其所在容器、内部字段）不能用于普通断言")
    versions = _REGISTRY.get(operator_id)
    if not versions:
        raise AssertionConfigError(f"未实现的断言类型：{operator_id}")
    func = versions.get(operator_version)
    if func is None:
        raise AssertionConfigError(f"断言类型 {operator_id} 无版本 {operator_version}")
    try:
        return func(value, found, params, ctx)
    except AssertionConfigError:
        raise
    except AssertionPolicyError:
        raise
    except (ValueLiteralError, ValueError, TypeError) as error:
        return AssertionOutcome(
            status="error",
            reason_code="type_error",
            message=f"断言求值类型错误：{error}",
        )
