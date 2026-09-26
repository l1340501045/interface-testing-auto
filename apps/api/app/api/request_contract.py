"""请求 v2 的客户端能力门禁。

声明只证明客户端会完整保留 v2 字段；权限、修订号和执行准入仍由原入口负责。
"""
from __future__ import annotations

from typing import Any

from .errors import conflict


def is_v2(request: Any) -> bool:
    return isinstance(request, dict) and request.get("schema_version") == 2


def has_row_locator(assertions: Any) -> bool:
    if not isinstance(assertions, list):
        return False
    for item in assertions:
        if not isinstance(item, dict):
            continue
        selector = item.get("selector")
        if not isinstance(selector, list):
            continue
        if any(
            isinstance(step, dict) and step.get("kind") == "row"
            for step in selector
        ):
            return True
    return False


def require_v2_capability(contract: str | None, *, needed: bool) -> None:
    if needed and contract != "2":
        raise conflict(
            "client_contract_required",
            "这份内容使用请求格式 v2，请刷新页面后再读取或修改，避免丢失停用行、说明或字段条件。",
        )


__all__ = ["has_row_locator", "is_v2", "require_v2_capability"]
