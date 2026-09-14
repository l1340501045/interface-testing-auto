"""断言操作符实现：阶段 1 值比较、数值、集合、存在/空/类型、字符串、长度。

每个操作符通过 assertions.register 注册，携带语义版本。值比较保留类型，
数值用 Decimal 无损比较，区间默认开区间（min < value < max）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .assertion_inputs import NameValuePairs
from .assertions import (
    AssertionConfigError,
    AssertionContext,
    AssertionOutcome,
    AssertionValueTypeError,
    register,
)
from .lossless_json import NumberNode, loads
from .valueliteral import ValueLiteral


def _cmp_value(value: Any) -> Any:
    """把无损节点/字面量转为可比较 Python 值，数字统一 Decimal，保留类型。

    内部包装（NumberNode／ValueLiteral）按**显式类型**分支处理，不做形状猜测。
    字典一律逐字段递归：业务对象的每个字段都要参与比较，按形状丢掉任何一个都会
    让“只差一个字段”的两个对象被判成相等。

    重复的键（例如两次出现的同名查询参数）不能折叠成 dict，否则后面的值会
    覆盖前面，等于断言就会对“全部出现”误判。这里按出现顺序保留为成对列表。
    """
    if isinstance(value, NumberNode):
        return value.decimal()
    if isinstance(value, ValueLiteral):
        if value.type == "number":
            return Decimal(value.text)
        if value.type == "string":
            return value.text
        if value.type == "boolean":
            return value.value
        if value.type == "null":
            return None
        if value.type == "json":
            # 必须递归转换：json 字面量里的数字同样是 NumberNode，直接返回会让
            # “期望 json 与同构取值”在**含数字**时判不相等（Decimal 与 NumberNode
            # 类型不同），而仅含字符串/布尔时又恰好相等——同一类断言一半能用一半
            # 不能用，最难排查。与上面的 list/dict 分支保持同一条转换规则。
            return _cmp_value(loads(value.text))
    if isinstance(value, list):
        return [_cmp_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _cmp_value(item) for key, item in value.items()}
    return value  # str / bool / None


def _deep_equal(left: Any, right: Any) -> bool:
    """深度相等；只对**取值边界标明来源**的重复键数组接受简写。

    请求头／查询参数／响应头／Cookie 在协议上就是重复键集合，取值边界
    （`pairs_root`／`parse_cookie_root`）把它标成 NameValuePairs，对这些值，期望
    可以写整份数组、单个 {"name": n, "value": v}、或只写取值 "v"。正文里的业务
    对象没有这个标记，一律逐字段严格比较——按形状猜来源会把字段丢掉，断言通过、
    请求还照发，而实际内容并不相等，是最坏的一类假通过。
    """
    # 期望侧先按字面量契约还原：界面上写的单个 pair 是以 json 字面量送来的，
    # 不先还原就看不到它其实是一个对象，单对简写会被误读成“只比取值”。
    expected = _cmp_value(right)
    left, expected = _unwrap_pairs(left, expected)
    return _strict_equal(_cmp_value(left), expected)


def _unwrap_pairs(left: Any, right: Any) -> tuple[Any, Any]:
    """把「只出现一次的重复键数组」与它对应的简写期望拉平成同一形态。

    只看**来源标记**，不看形状：期望侧是用户写的，永远不带标记，因此只判断左侧。
    取值侧不是标记值、出现次数不为 1、或唯一那项不是 {name,value} 时原样返回，
    交给严格深度比较——正文里的业务对象走的正是这条路。
    """
    if not isinstance(left, NameValuePairs) or len(left) != 1:
        return left, right
    only = left[0]
    if not isinstance(only, dict) or set(only) != {"name", "value"}:
        return left, right
    if isinstance(right, list):
        # 期望写的是整份数组：逐项严格比较，不做简写。
        return left, right
    if isinstance(right, dict):
        if set(right) == {"name", "value"}:
            return only, right
        # 期望是其它对象：严格比较，不能改成只比这一项的取值。
        return left, right
    # 期望写的是标量：只比较唯一那一项的取值。
    return only["value"], right


def _is_number(value: Any) -> bool:
    """布尔不是数字。

    bool 在 Python 里是 int 的子类，用 (int, float, Decimal) 判断会把 True 送进
    数字分支，交给 Decimal 解析时抛 InvalidOperation。数字分支必须先排除布尔。
    """
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _strict_equal(lv: Any, rv: Any) -> bool:
    """严格深度相等：对象比全部字段（忽略键顺序），数组比顺序与长度。

    这里不做任何形状特判——`{name,value}` 之间不比别的对象“更特殊”，重复键数组的
    简写已在 `_unwrap_pairs` 按来源处理完，能走到这里的两个对象都必须字段完全一致。
    """
    if isinstance(lv, dict) and isinstance(rv, dict):
        return set(lv.keys()) == set(rv.keys()) and all(_strict_equal(lv[k], rv[k]) for k in lv)
    if isinstance(lv, list) and isinstance(rv, list):
        if len(lv) != len(rv):
            return False
        return all(_strict_equal(a, b) for a, b in zip(lv, rv, strict=True))
    if _is_number(lv) and _is_number(rv):
        # 数字相等不受字面拼写影响：1、1.0、"1.0" 的十进制值相同即相等。
        return Decimal(str(lv)) == Decimal(str(rv))
    return type(lv) is type(rv) and lv == rv


def _expect(params: dict) -> Any:
    raw = params.get("expected")
    if raw is None and "expected" not in params:
        raise AssertionConfigError("缺少期望值 expected")
    return _param_literal(raw)


def _param_literal(raw: Any) -> Any:
    """参数中的期望/数值按 ValueLiteral 契约传入，dict 且含 type 视为字面量。"""
    if isinstance(raw, dict) and "type" in raw:
        return ValueLiteral.from_dict(raw)
    return raw


def _value_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict)):
        return len(value) == 0
    return False


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, NumberNode):
        text = value.text
        if "." in text or "e" in text or "E" in text:
            return "number"
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


# —— 值与集合 ——


def _equals(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", expected=_cmp_value(_expect(params)), actual=None, message="字段缺失，等于比较失败")
    expected = _expect(params)
    ok = _deep_equal(value, expected)
    return AssertionOutcome(
        "passed" if ok else "failed",
        None if ok else "not_equal",
        expected=_cmp_value(expected),
        actual=_cmp_value(value),
        message="" if ok else "值不等于期望",
    )


def _not_equals(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，不等于比较失败")
    ok = not _deep_equal(value, _expect(params))
    return AssertionOutcome("passed" if ok else "failed", None if ok else "equal", message="" if ok else "值等于期望，不应相等")


def _in_set(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，属于比较失败")
    values = params.get("values")
    if not isinstance(values, list):
        raise AssertionConfigError("属于断言需要 values 数组")
    ok = any(_deep_equal(value, _param_literal(item)) for item in values)
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_in_set", message="" if ok else "值不在集合中")


def _not_in_set(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，不属于比较失败")
    values = params.get("values")
    if not isinstance(values, list):
        raise AssertionConfigError("不属于断言需要 values 数组")
    ok = not any(_deep_equal(value, _param_literal(item)) for item in values)
    return AssertionOutcome("passed" if ok else "failed", None if ok else "in_set", message="" if ok else "值在集合中，不应属于")


# —— 数值比较 ——


def _as_number(value: Any) -> Decimal:
    node = _cmp_value(value)
    if isinstance(node, (Decimal, int, float)):
        return Decimal(str(node))
    raise AssertionValueTypeError("当前值不是数字，无法数值比较")


def _greater_than(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，数值比较失败")
    actual = _as_number(value)
    threshold = _cmp_value(_expect(params))
    ok = actual > threshold
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_greater", expected=str(threshold), actual=str(actual), message="" if ok else "值不大于期望")


def _greater_or_equal(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，数值比较失败")
    actual = _as_number(value)
    threshold = _cmp_value(_expect(params))
    ok = actual >= threshold
    return AssertionOutcome("passed" if ok else "failed", None if ok else "less", expected=str(threshold), actual=str(actual), message="" if ok else "值小于期望")


def _less_than(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，数值比较失败")
    actual = _as_number(value)
    threshold = _cmp_value(_expect(params))
    ok = actual < threshold
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_less", expected=str(threshold), actual=str(actual), message="" if ok else "值不小于期望")


def _less_or_equal(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，数值比较失败")
    actual = _as_number(value)
    threshold = _cmp_value(_expect(params))
    ok = actual <= threshold
    return AssertionOutcome("passed" if ok else "failed", None if ok else "greater", expected=str(threshold), actual=str(actual), message="" if ok else "值大于期望")


def _param_bound(params: dict, key: str) -> Decimal:
    """取区间边界；缺失或非法按配置错误处理，不抛 KeyError。"""
    if key not in params:
        raise AssertionConfigError(f"区间缺少边界参数 {key}")
    return _cmp_value(_param_literal(params[key]))


def _range(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    # 先校验配置（min < max），再判断取值是否存在。
    # 配置无效说明这条断言本身写错了，与本次运行是否恰好有该字段无关；
    # 若先看 missing，一次字段不存在就会把配置错误显示成业务断言失败。
    lo = _param_bound(params, "min")
    hi = _param_bound(params, "max")
    if not (lo < hi):
        raise AssertionConfigError("区间需满足 min < max")
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，区间比较失败")
    actual = _as_number(value)
    include_min = bool(params.get("include_min", False))
    include_max = bool(params.get("include_max", False))
    left_ok = actual > lo if not include_min else actual >= lo
    right_ok = actual < hi if not include_max else actual <= hi
    ok = left_ok and right_ok
    return AssertionOutcome("passed" if ok else "failed", None if ok else "out_of_range", expected=f"{lo} {'<' if not include_min else '≤'} x {'<' if not include_max else '≤'} {hi}", actual=str(actual), message="" if ok else "值不在区间内")


def _not_range(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    inner = _range(value, found, params, ctx)
    if inner.status == "error":
        return inner
    ok = inner.status == "failed"
    return AssertionOutcome("passed" if ok else "failed", None if ok else "in_range", message="" if ok else "值在区间内，不应在区间内")


# —— 存在 / 空 / 类型 ——


def _exists(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    return AssertionOutcome("passed" if found else "failed", None if found else "missing", message="" if found else "字段不存在")


def _not_exists(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    return AssertionOutcome("passed" if not found else "failed", None if not found else "exists", message="" if not found else "字段存在，应不存在")


def _is_null(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，非 null")
    # 归一化后再判断：字面量 {"type":"null"} 与原生 None 必须是同一含义。
    ok = _cmp_value(value) is None
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_null", message="" if ok else "值不是 null")


def _not_null(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，非 null 比较失败")
    ok = _cmp_value(value) is not None
    return AssertionOutcome("passed" if ok else "failed", None if ok else "null", message="" if ok else "值是 null")


def _is_empty(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    ok = _value_is_empty(_cmp_value(value)) if found else True
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_empty", message="" if ok else "值非空")


def _not_empty(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    ok = (not _value_is_empty(_cmp_value(value))) if found else False
    return AssertionOutcome("passed" if ok else "failed", None if ok else "empty", message="" if ok else "值为空")


def _is_type(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，类型比较失败")
    want = params.get("type")
    actual = _type_of(value)
    ok = actual == want
    return AssertionOutcome("passed" if ok else "failed", None if ok else "type_mismatch", expected=want, actual=actual, message="" if ok else f"类型不符：期望 {want}，实际 {actual}")


# —— 字符串 ——


def _as_str(value: Any) -> str:
    node = _cmp_value(value)
    if not isinstance(node, str):
        raise AssertionValueTypeError("当前值不是字符串")
    return node


def _contains(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，包含比较失败")
    text = _as_str(value)
    needle = str(_cmp_value(_expect(params)))
    ok = needle in text
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_contains", message="" if ok else "不包含期望子串")


def _not_contains(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，不包含比较失败")
    text = _as_str(value)
    needle = str(_cmp_value(_expect(params)))
    ok = needle not in text
    return AssertionOutcome("passed" if ok else "failed", None if ok else "contains", message="" if ok else "包含不应出现的子串")


def _starts_with(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，前缀比较失败")
    text = _as_str(value)
    prefix = str(_cmp_value(_expect(params)))
    ok = text.startswith(prefix)
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_starts_with", message="" if ok else "不以期望前缀开头")


def _ends_with(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，后缀比较失败")
    text = _as_str(value)
    suffix = str(_cmp_value(_expect(params)))
    ok = text.endswith(suffix)
    return AssertionOutcome("passed" if ok else "failed", None if ok else "not_ends_with", message="" if ok else "不以期望后缀结尾")


# —— 长度 / 数量 ——


def _as_len(value: Any) -> int:
    node = _cmp_value(value)
    if isinstance(node, str):
        return len(node)
    if isinstance(node, (list, dict)):
        return len(node)
    raise AssertionValueTypeError("当前值无长度（需字符串或数组/对象）")


def _as_count(raw: Any, label: str) -> int:
    """长度／数量参数必须是整数，小数或非数字属于配置错误。"""
    number = _cmp_value(_param_literal(raw))
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    if isinstance(number, Decimal) and number == number.to_integral_value():
        return int(number)
    raise AssertionConfigError(f"{label}必须是整数")


def _length_equals(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    # 同区间：参数非法属于配置错误，优先于“字段缺失”，避免掩盖写错的断言。
    if "expected" not in params:
        raise AssertionConfigError("长度等于需要 expected 参数")
    want = _as_count(params["expected"], "长度期望值")
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，长度比较失败")
    actual = _as_len(value)
    ok = actual == want
    return AssertionOutcome("passed" if ok else "failed", None if ok else "length_mismatch", expected=want, actual=actual, message="" if ok else "长度不符")


def _length_range(value: Any, found: bool, params: dict, ctx: AssertionContext) -> AssertionOutcome:
    if "min" not in params or "max" not in params:
        raise AssertionConfigError("长度区间缺少边界参数")
    lo = _as_count(params["min"], "长度下限")
    hi = _as_count(params["max"], "长度上限")
    if not (lo < hi):
        raise AssertionConfigError("长度区间需满足 min < max")
    if not found:
        return AssertionOutcome("failed", "missing", message="字段缺失，长度区间比较失败")
    actual = _as_len(value)
    include_min = bool(params.get("include_min", False))
    include_max = bool(params.get("include_max", False))
    left_ok = actual > lo if not include_min else actual >= lo
    right_ok = actual < hi if not include_max else actual <= hi
    ok = left_ok and right_ok
    return AssertionOutcome("passed" if ok else "failed", None if ok else "length_out_of_range", message="" if ok else "长度不在区间内")


# —— 注册（version=1） ——

for _op in [
    ("equals", _equals),
    ("not_equals", _not_equals),
    ("in_set", _in_set),
    ("not_in_set", _not_in_set),
    ("greater_than", _greater_than),
    ("greater_or_equal", _greater_or_equal),
    ("less_than", _less_than),
    ("less_or_equal", _less_or_equal),
    ("range", _range),
    ("not_range", _not_range),
    ("exists", _exists),
    ("not_exists", _not_exists),
    ("is_null", _is_null),
    ("not_null", _not_null),
    ("is_empty", _is_empty),
    ("not_empty", _not_empty),
    ("is_type", _is_type),
    ("contains", _contains),
    ("not_contains", _not_contains),
    ("starts_with", _starts_with),
    ("ends_with", _ends_with),
    ("length_equals", _length_equals),
    ("length_range", _length_range),
]:
    register(_op[0], 1, _op[1])
