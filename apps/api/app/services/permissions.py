"""项目角色与动作授权。

RLS 是租户数据防线；本模块负责项目内“谁能做什么”的动作授权，两者并行。
角色等级沿用执行契约第 9 节：管理员 / 编辑者 / 查看者。
"""
from __future__ import annotations

from ..api.errors import forbidden

_ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}

ACTION_MIN_RANK = {
    "view": 1,
    "edit": 2,
    "execute": 2,
    "manage_secrets": 3,
    "manage_pool_grants": 3,
    "classify_production": 3,
    "administer": 3,
}


def rank(role: str) -> int:
    return _ROLE_RANK.get(role, 0)


def can(role: str, action: str) -> bool:
    return rank(role) >= ACTION_MIN_RANK.get(action, 99)


def require(role: str, action: str) -> None:
    """动作不满足时抛 403，信息说明所需角色，不泄露其他租户信息。"""
    if not can(role, action):
        raise forbidden(
            "insufficient_role",
            f"当前项目角色（{_ROLE_LABEL.get(role, role)}）无权执行该操作。",
        )


_ROLE_LABEL = {"admin": "管理员", "editor": "编辑者", "viewer": "查看者"}
