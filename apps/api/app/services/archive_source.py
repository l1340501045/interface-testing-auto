"""归档批次显式根与真实成员的纯读取解析；不控制事务生命周期。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api import deps
from ..models import AssetArchiveMember, AssetOperation


@dataclass(frozen=True)
class ArchiveSource:
    operation_id: uuid.UUID
    root_id: uuid.UUID
    folder_ids: frozenset[uuid.UUID]
    case_ids: frozenset[uuid.UUID]


def resolve_archive_source(
    session: Session, scope: deps.ProjectScope, operation_id: uuid.UUID
) -> ArchiveSource | None:
    return resolve_archive_sources(session, scope, [operation_id]).get(operation_id)


def resolve_archive_sources(
    session: Session, scope: deps.ProjectScope, operation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ArchiveSource]:
    unique_ids = set(operation_ids)
    if not unique_ids:
        return {}
    operations = list(
        session.scalars(
            select(AssetOperation).where(
                AssetOperation.id.in_(unique_ids),
                AssetOperation.workspace_id == scope.workspace_id,
                AssetOperation.project_id == scope.project_id,
                AssetOperation.action == "folder_archive",
                AssetOperation.result_schema_version == 1,
            )
        )
    )
    members = list(session.scalars(select(AssetArchiveMember).where(
        AssetArchiveMember.workspace_id == scope.workspace_id,
        AssetArchiveMember.project_id == scope.project_id,
        AssetArchiveMember.operation_id.in_(unique_ids),
    )))
    by_operation: dict[uuid.UUID, list[AssetArchiveMember]] = {}
    for member in members:
        by_operation.setdefault(member.operation_id, []).append(member)
    result: dict[uuid.UUID, ArchiveSource] = {}
    for operation in operations:
        if not isinstance(operation.result, dict):
            continue
        root = operation.result.get("root")
        if not isinstance(root, dict) or root.get("resource_type") != "folder":
            continue
        raw_id = root.get("id")
        if not isinstance(raw_id, str):
            continue
        try:
            root_id = uuid.UUID(raw_id)
        except ValueError:
            continue
        operation_members = by_operation.get(operation.id, [])
        folder_ids = frozenset(
            item.folder_id for item in operation_members if item.folder_id is not None
        )
        if root_id not in folder_ids:
            continue
        result[operation.id] = ArchiveSource(
            operation_id=operation.id,
            root_id=root_id,
            folder_ids=folder_ids,
            case_ids=frozenset(
                item.case_id for item in operation_members if item.case_id is not None
            ),
        )
    return result
