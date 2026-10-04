"""目录图的有界递归与一致读取公共入口。"""
from __future__ import annotations

import uuid

from sqlalchemy import any_, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, aliased

from ..api import deps
from ..api.errors import not_found
from ..models import Folder, Project, WorkspaceMembership
from .permissions import require


def begin_consistent_read(session: Session, scope: deps.ProjectScope) -> deps.ProjectScope:
    """结束鉴权读取事务，并以 REPEATABLE READ 开始业务查询快照。

    项目范围依赖已经完成认证、CSRF 与项目可见性校验。READ COMMITTED 下 count、
    当前页、祖先和归档根的多条 SELECT 会各取一个快照，因此这里在任何业务查询前
    结束只读鉴权事务，并让后续所有读取共享一个 PostgreSQL 一致快照。tenant 绑定
    由 db.py 的 after_begin 自动重放。
    """
    session.commit()
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.expire_all()
    # 鉴权事务与业务快照之间可能发生账号停用、会话撤销或权限变化；在新快照
    # 首次读取中从原 session_id/user_id 重建主体，再核 membership 和项目。
    fresh_principal = deps.reauthenticate_principal(session, scope.principal)
    role = session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == scope.workspace_id,
            WorkspaceMembership.user_id == fresh_principal.user_id,
        )
    )
    project_exists = session.scalar(
        select(Project.id).where(
            Project.workspace_id == scope.workspace_id,
            Project.id == scope.project_id,
        )
    )
    if role is None or project_exists is None:
        raise not_found("项目不存在或无权访问")
    # 平台 is_admin 不绕过工作空间成员角色；角色和 is_admin 都来自当前快照，
    # 不使用等待前 scope 中的平面权限事实。
    require(role, "view")
    return deps.ProjectScope(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        role=role,
        principal=fresh_principal,
    )


def blocked_folder_ids(scope: deps.ProjectScope):
    """归档目录及其全部后代；只投影 id 并用 UNION 去重，目录环也会终止。"""
    blocked = (
        select(Folder.id.label("id"))
        .where(Folder.project_id == scope.project_id, Folder.archived_at.is_not(None))
        .cte("blocked_folders", recursive=True)
    )
    child = aliased(Folder)
    return blocked.union(
        select(child.id).join(blocked, child.parent_id == blocked.c.id).where(
            child.project_id == scope.project_id
        )
    )


def folder_descendants(scope: deps.ProjectScope, folder_id: uuid.UUID):
    """目录自身及全部后代；只投影 id 并用 UNION 去重，目录环也会终止。"""
    descendants = (
        select(Folder.id.label("id"))
        .where(Folder.project_id == scope.project_id, Folder.id == folder_id)
        .cte("folder_descendants", recursive=True)
    )
    child = aliased(Folder)
    return descendants.union(
        select(child.id).join(descendants, child.parent_id == descendants.c.id).where(
            child.project_id == scope.project_id
        )
    )


def ancestor_walk(scope: deps.ProjectScope, folder_ids: list[uuid.UUID] | None = None):
    """从每个目录向上读取祖先，visited 检测到循环后立即停止该分支。"""
    child = aliased(Folder)
    parent = aliased(Folder)
    base = (
        select(
            child.id.label("node_id"),
            parent.id.label("ancestor_id"),
            parent.name.label("ancestor_name"),
            parent.archived_at.label("ancestor_archived_at"),
            parent.parent_id.label("next_parent_id"),
            literal(1).label("depth"),
            postgresql.array([child.id, parent.id]).label("visited"),
            (parent.id == child.id).label("cycle"),
        )
        .join(parent, parent.id == child.parent_id)
        .where(child.project_id == scope.project_id, parent.project_id == scope.project_id)
    )
    if folder_ids is not None:
        base = base.where(child.id.in_(folder_ids))
    walk = base.cte("folder_ancestor_walk", recursive=True)
    next_parent = aliased(Folder)
    return walk.union_all(
        select(
            walk.c.node_id,
            next_parent.id,
            next_parent.name,
            next_parent.archived_at,
            next_parent.parent_id,
            walk.c.depth + 1,
            walk.c.visited + postgresql.array([next_parent.id]),
            next_parent.id == any_(walk.c.visited),
        )
        .join(next_parent, next_parent.id == walk.c.next_parent_id)
        .where(next_parent.project_id == scope.project_id, ~walk.c.cycle)
    )


def invalid_folder_ids(scope: deps.ProjectScope):
    """返回父链进入循环的目录；从每个节点起步，后代也会被标作失效。"""
    walk = ancestor_walk(scope)
    return select(walk.c.node_id.label("id")).where(walk.c.cycle).distinct().subquery(
        "invalid_folders"
    )


def case_folder_paths(
    session: Session,
    scope: deps.ProjectScope,
    folder_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]] | None]:
    """一次批量读取当前页目录的根到自身路径；异常链返回 None。"""
    unique_ids = list(dict.fromkeys(folder_ids))
    if not unique_ids:
        return {}
    if len(unique_ids) > 100:
        raise ValueError("当前页目录数量超过用例库页大小上限")

    folder = aliased(Folder)
    walk = (
        select(
            folder.id.label("node_id"),
            folder.id.label("path_id"),
            folder.name.label("path_name"),
            folder.parent_id.label("next_parent_id"),
            literal(0).label("depth"),
            postgresql.array([folder.id]).label("visited"),
            literal(False).label("cycle"),
        )
        .where(folder.project_id == scope.project_id, folder.id.in_(unique_ids))
        .cte("case_folder_paths", recursive=True)
    )
    parent = aliased(Folder)
    walk = walk.union_all(
        select(
            walk.c.node_id,
            parent.id,
            parent.name,
            parent.parent_id,
            walk.c.depth + 1,
            walk.c.visited + postgresql.array([parent.id]),
            parent.id == any_(walk.c.visited),
        )
        .join(parent, parent.id == walk.c.next_parent_id)
        .where(parent.project_id == scope.project_id, ~walk.c.cycle)
    )
    rows = session.execute(
        select(walk).order_by(walk.c.node_id, walk.c.depth.desc())
    ).all()
    grouped: dict[uuid.UUID, list] = {folder_id: [] for folder_id in unique_ids}
    for row in rows:
        grouped.setdefault(row.node_id, []).append(row)

    result: dict[uuid.UUID, list[tuple[uuid.UUID, str]] | None] = {}
    for folder_id in unique_ids:
        path_rows = grouped.get(folder_id, [])
        if (
            not path_rows
            or any(row.cycle for row in path_rows)
            or path_rows[0].next_parent_id is not None
        ):
            result[folder_id] = None
            continue
        result[folder_id] = [
            (row.path_id, row.path_name) for row in path_rows if not row.cycle
        ]
    return result
