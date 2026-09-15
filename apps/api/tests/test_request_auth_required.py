"""请求定义的 auth_required 约束：严格布尔、缺省省略、执行保护。

`auth_required=true` 表示“这份请求必须使用当前环境的登录态”。它是导入认证头／
Cookie 时留下的硬约束，会随请求进入版本、调试摘要与运行快照。

两条容易写错的地方在这里锁死：

- **缺省与 false 都必须省略这个键。** 请求规范化结果参与用例版本摘要与调试快照
  摘要，多写一个 `"auth_required": false` 会让所有既有版本与既有授权在新代码下
  算出另一份摘要，旧记录与旧授权全部对不上。兼容不是“能跑通”，是摘要逐字节相同。
- **只接受真正的布尔值。** `"false"`、`0`、`[]` 都是 falsy 但并非 false；按 truthy
  收下会把一份匿名可用的请求变成“缺身份就拒绝”，用户写的内容明明是“不需要认证”。
"""
from __future__ import annotations

import pytest

from app.kernel.request_spec import RequestSpecError, validate_request

_MINIMAL = {"method": "GET", "path": "/echo", "body_type": "none", "body": ""}


def test_absent_auth_required_is_omitted_from_normalization() -> None:
    """缺省时规范化结果里根本没有这个键，既有摘要因此逐字节不变。"""
    normalized = validate_request(dict(_MINIMAL))
    assert "auth_required" not in normalized
    assert set(normalized) == {"method", "path", "query_params", "headers", "body_type", "body"}


def test_false_auth_required_is_also_omitted() -> None:
    """显式 false 与缺省等价：再写一次 false 也会改变旧版本与旧授权的摘要。"""
    normalized = validate_request({**_MINIMAL, "auth_required": False})
    assert "auth_required" not in normalized


def test_true_auth_required_is_preserved() -> None:
    normalized = validate_request({**_MINIMAL, "auth_required": True})
    assert normalized["auth_required"] is True


@pytest.mark.parametrize("value", ["false", "true", 0, 1, "", [], {}, None])
def test_non_boolean_auth_required_is_rejected(value: object) -> None:
    """非布尔值一律拒绝（None 表示缺省，按缺省处理，不进这一支）。"""
    spec = {**_MINIMAL, "auth_required": value}
    if value is None:
        assert "auth_required" not in validate_request(spec)
        return
    with pytest.raises(RequestSpecError, match="auth_required"):
        validate_request(spec)
