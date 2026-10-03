"""S2 资产选择、幂等操作与统一项目写门闩。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..api import deps
from ..api.request_contract import is_v2, require_v2_capability
from ..api.schemas import (
    AssetOperationCreate,
    AssetOperationOut,
    AssetSelectionCreate,
    AssetSelectionOut,
)
from ..kernel.assertion_spec import AssertionSpecError, validate_assertions
from ..kernel.request_spec import RequestSpecError, validate_request
from ..models import (
    AssetArchiveMember,
    AssetOperation,
    AssetSelection,
    AuditEvent,
    Case,
    Folder,
    Project,
    WorkspaceMembership,
)
from .archive_source import resolve_archive_source
from .folder_graph import (
    begin_consistent_read,
    blocked_folder_ids,
    folder_descendants,
    invalid_folder_ids,
)
from .permissions import require

_SELECTION_TTL = timedelta(minutes=10)
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class AssetLifecycleError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message
        self.details = details or {}


def lock_project_and_reauthorize(
    session: Session, scope: deps.ProjectScope, action: str
) -> deps.ProjectScope:
    project = session.scalar(
        select(Project)
        .where(Project.workspace_id == scope.workspace_id, Project.id == scope.project_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise AssetLifecycleError(404, "not_found", "项目不存在或无权访问")
    session.expire_all()
    principal = deps.reauthenticate_principal(session, scope.principal)
    role = session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == scope.workspace_id,
            WorkspaceMembership.user_id == principal.user_id,
        )
    )
    if role is None:
        raise AssetLifecycleError(404, "not_found", "项目不存在或无权访问")
    require(role, action)
    return deps.ProjectScope(scope.workspace_id, scope.project_id, role, principal)


def _parameters(action: str, raw: dict, fields: set[str] | None = None) -> dict:
    fields = fields or set(raw)
    allowed: set[str]
    if action == "move":
        allowed = {"target_folder_id"}
        if "target_folder_id" not in raw:
            raise AssetLifecycleError(422, "invalid_request", "move 必须提供 target_folder_id")
    elif action in {"archive", "folder_archive"}:
        allowed = set()
    elif action == "restore":
        allowed = {"target_folder_id"}
    elif action == "folder_restore":
        allowed = {"target_parent_id", "root_name"}
    else:
        allowed = set()
    if set(raw) - allowed:
        raise AssetLifecycleError(422, "invalid_request", "资产动作包含未知参数")
    result = dict(raw)
    for key in ("target_folder_id", "target_parent_id"):
        if key in result and result[key] is not None:
            try:
                result[key] = str(uuid.UUID(str(result[key])))
            except ValueError as error:
                raise AssetLifecycleError(422, "invalid_request", f"{key} 不是合法 UUID") from error
    if "root_name" in result:
        if not isinstance(result["root_name"], str):
            raise AssetLifecycleError(422, "invalid_request", "root_name 必须是字符串")
        result["root_name"] = result["root_name"].strip()
        if not 1 <= len(result["root_name"]) <= 200:
            raise AssetLifecycleError(422, "invalid_request", "root_name 长度必须为1到200")
    return result


def resolve_available_folder(session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID | None) -> Folder | None:
    if folder_id is None:
        return None
    folder = session.scalar(
        select(Folder).where(
            Folder.workspace_id == scope.workspace_id,
            Folder.project_id == scope.project_id,
            Folder.id == folder_id,
        ).execution_options(populate_existing=True)
    )
    if folder is None:
        raise AssetLifecycleError(404, "not_found", "目标目录不存在或无权访问")
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    unavailable = session.scalar(
        select(Folder.id).where(
            Folder.id == folder_id,
            (Folder.id.in_(select(blocked.c.id)) | Folder.id.in_(select(invalid.c.id))),
        )
    )
    if unavailable is not None:
        raise AssetLifecycleError(409, "folder_unavailable", "目标目录不可用")
    return folder


def _item(resource_type: str, obj, outcome: str, code: str | None = None) -> dict:
    return {
        "resource_type": resource_type,
        "id": str(obj.id),
        "rev": obj.rev,
        "state": "archived" if (obj.status == "archived" if resource_type == "case" else obj.archived_at is not None) else "active",
        "name": obj.name,
        "parent_id": str(obj.parent_id) if resource_type == "folder" and obj.parent_id else None,
        "folder_id": str(obj.folder_id) if resource_type == "case" and obj.folder_id else None,
        "archive_operation_id": str(obj.archive_operation_id) if obj.archive_operation_id else None,
        "outcome": outcome,
        "code": code,
    }


def _asset_metadata(resource_type: str, obj) -> dict:
    return {
        "id": str(obj.id),
        "resource_type": resource_type,
        "name": obj.name,
        "rev": obj.rev,
        "state": "archived" if (obj.status == "archived" if resource_type == "case" else obj.archived_at is not None) else "active",
        "folder_id": str(obj.folder_id) if resource_type == "case" and obj.folder_id else None,
        "parent_id": str(obj.parent_id) if resource_type == "folder" and obj.parent_id else None,
        "archived_at": obj.archived_at.isoformat() if resource_type == "folder" and obj.archived_at else None,
    }


def _compute_selection(
    session: Session, scope: deps.ProjectScope, payload: AssetSelectionCreate
) -> tuple[list[dict], list[dict], dict | None, dict]:
    params = _parameters(payload.action, payload.parameters)
    eligible: list[dict] = []
    excluded: list[dict] = []
    root_out = None
    if payload.mode == "explicit":
        requested = payload.items[0]
        case = session.scalar(
            select(Case).where(
                Case.workspace_id == scope.workspace_id,
                Case.project_id == scope.project_id,
                Case.id == requested.id,
            )
        )
        if case is None:
            excluded.append({"resource_type": "case", "id": str(requested.id), "rev": None, "state": None, "name": None, "parent_id": None, "folder_id": None, "outcome": "excluded", "code": "not_found_or_inaccessible"})
        else:
            state = "archived" if case.status == "archived" else "active"
            valid = case.rev == requested.expected_rev
            if payload.action in {"move", "archive"}:
                valid = valid and state == "active"
            if payload.action == "restore":
                valid = valid and state == "archived"
            target = params.get("target_folder_id") if "target_folder_id" in params else case.folder_id
            if target is not None:
                resolve_available_folder(session, scope, uuid.UUID(str(target)))
            (eligible if valid else excluded).append(
                _item("case", case, "eligible" if valid else "excluded", None if valid else "revision_or_state_conflict")
            )
    else:
        requested = payload.root
        root = session.scalar(
            select(Folder).where(
                Folder.workspace_id == scope.workspace_id,
                Folder.project_id == scope.project_id,
                Folder.id == requested.id,
            )
        )
        if root is None:
            raise AssetLifecycleError(404, "not_found", "目录不存在或无权访问")
        root_out = {"resource_type": "folder", "id": str(root.id), "expected_rev": requested.expected_rev}
        if root.rev != requested.expected_rev:
            excluded.append(_item("folder", root, "excluded", "revision_conflict"))
        elif payload.action == "folder_archive":
            if root.archived_at is not None:
                excluded.append(_item("folder", root, "excluded", "already_archived"))
            else:
                descendants = folder_descendants(scope, root.id)
                folders = list(session.scalars(select(Folder).where(Folder.id.in_(select(descendants.c.id))).order_by(Folder.id)))
                folder_ids = [item.id for item in folders]
                cases = list(session.scalars(select(Case).where(Case.project_id == scope.project_id, Case.folder_id.in_(folder_ids)).order_by(Case.id)))
                for item in folders:
                    (eligible if item.archived_at is None else excluded).append(_item("folder", item, "eligible" if item.archived_at is None else "excluded", None if item.archived_at is None else "already_archived"))
                for item in cases:
                    (eligible if item.status != "archived" else excluded).append(_item("case", item, "eligible" if item.status != "archived" else "excluded", None if item.status != "archived" else "already_archived"))
        else:
            if root.archived_at is None:
                raise AssetLifecycleError(409, "asset_state_conflict", "活动目录不能执行恢复")
            operation_id = root.archive_operation_id
            if operation_id is None:
                eligible.append(_item("folder", root, "eligible"))
            else:
                source = resolve_archive_source(session, scope, operation_id)
                if source is None:
                    raise AssetLifecycleError(409, "archive_source_unavailable", "归档批次来源不可用")
                if root.id not in source.folder_ids:
                    raise AssetLifecycleError(
                        409,
                        "archive_source_unavailable",
                        "当前目录不属于归档批次的真实成员",
                    )
                if source.root_id != root.id:
                    raise AssetLifecycleError(
                        409,
                        "archive_root_required",
                        "请选择原归档批次根目录",
                        {"archive_root_id": str(source.root_id)},
                    )
                members = list(session.scalars(select(AssetArchiveMember).where(
                    AssetArchiveMember.workspace_id == scope.workspace_id,
                    AssetArchiveMember.project_id == scope.project_id,
                    AssetArchiveMember.operation_id == operation_id,
                ).order_by(AssetArchiveMember.id)))
                for member in members:
                    model = Folder if member.folder_id else Case
                    obj = session.scalar(select(model).where(
                        model.workspace_id == scope.workspace_id,
                        model.project_id == scope.project_id,
                        model.id == (member.folder_id or member.case_id),
                    ).execution_options(populate_existing=True))
                    if obj is None:
                        continue
                    controlled = obj.archive_operation_id == operation_id and obj.rev == member.archived_rev
                    (eligible if controlled else excluded).append(_item("folder" if member.folder_id else "case", obj, "eligible" if controlled else "excluded", None if controlled else "no_longer_controlled"))
            target = params.get("target_parent_id") if "target_parent_id" in params else root.parent_id
            if target is not None and uuid.UUID(str(target)) == root.id:
                raise AssetLifecycleError(409, "target_invalid", "目录不能恢复到自身下")
            if target is not None:
                resolve_available_folder(session, scope, uuid.UUID(str(target)))
                descendants = folder_descendants(scope, root.id)
                if session.scalar(
                    select(descendants.c.id).where(descendants.c.id == uuid.UUID(str(target)))
                ):
                    raise AssetLifecycleError(409, "target_invalid", "目录不能恢复到自己的后代下")
            desired_name = params.get("root_name", root.name).strip().casefold()
            target_id = uuid.UUID(str(target)) if target else None
            sibling_query = select(Folder.id).where(
                Folder.project_id == scope.project_id,
                Folder.normalized_name == desired_name,
                Folder.id != root.id,
            )
            sibling_query = (
                sibling_query.where(Folder.parent_id.is_(None))
                if target_id is None
                else sibling_query.where(Folder.parent_id == target_id)
            )
            if session.scalar(sibling_query) is not None:
                raise AssetLifecycleError(409, "folder_name_conflict", "目标位置已存在同名目录")
    if len(eligible) > 500:
        raise AssetLifecycleError(413, "selection_too_large", "实际待变更对象超过500，请缩小范围")
    members = sorted([*eligible, *excluded], key=lambda value: (value["resource_type"], value["id"]))
    counts = {
        "selected": len(members), "eligible": len(eligible), "excluded": len(excluded),
        "cases": sum(item["resource_type"] == "case" for item in members),
        "folders": sum(item["resource_type"] == "folder" for item in members),
    }
    return eligible, excluded, root_out, {"members": members, "parameters": params, "counts": counts}


def create_selection(
    session: Session, scope: deps.ProjectScope, payload: AssetSelectionCreate
) -> AssetSelectionOut:
    scope = begin_consistent_read(session, scope)
    require(scope.role, "edit")
    _cleanup_expired_selections(session, scope)
    eligible, excluded, root, frozen = _compute_selection(session, scope, payload)
    dependencies = _preview_dependencies(session, scope, payload, eligible, root)
    now = datetime.now(UTC)
    selection = AssetSelection(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, action=payload.action,
        selector={
            "schema_version": 1,
            "mode": payload.mode,
            "root": root,
            "items": [item.model_dump(mode="json") for item in (payload.items or [])],
            "dependencies": dependencies,
        },
        members=frozen["members"], target=frozen["parameters"] or None,
        expires_at=now + _SELECTION_TTL,
    )
    session.add(selection)
    session.commit()
    return AssetSelectionOut(
        selection_id=selection.id, action=payload.action, mode=payload.mode,
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, created_at=selection.created_at,
        expires_at=selection.expires_at, counts=frozen["counts"], root=root,
        preview_items=eligible, excluded_items=excluded,
    )


def _intent(scope: deps.ProjectScope, payload: AssetOperationCreate) -> dict:
    base = {
        "schema_version": 1, "workspace_id": str(scope.workspace_id),
        "project_id": str(scope.project_id), "principal_id": str(scope.principal.user_id),
        "action": payload.action,
    }
    if payload.action == "case_copy":
        base.update({
            "source_id": str(payload.source_id), "expected_rev": payload.expected_rev,
            "name": {"present": "name" in payload.model_fields_set, "value": payload.name if "name" in payload.model_fields_set else None},
            "folder_id": {"present": "folder_id" in payload.model_fields_set, "value": str(payload.folder_id) if payload.folder_id else None},
        })
    else:
        base.update({"selection_id": str(payload.selection_id), "parameters": _parameters(payload.action, payload.parameters)})
    return base


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _operation_out(operation: AssetOperation) -> AssetOperationOut:
    return AssetOperationOut(
        operation_id=operation.id, operation_key=operation.operation_key,
        action=operation.action, workspace_id=operation.workspace_id,
        project_id=operation.project_id, principal_id=operation.principal_id,
        result_schema_version=operation.result_schema_version,
        created_at=operation.created_at, result=operation.result,
    )


def get_operation_by_key(session: Session, scope: deps.ProjectScope, key: str) -> AssetOperationOut:
    _cleanup_expired_selections(session, scope)
    session.commit()
    operation = session.scalar(select(AssetOperation).where(
        AssetOperation.workspace_id == scope.workspace_id,
        AssetOperation.project_id == scope.project_id,
        AssetOperation.principal_id == scope.principal.user_id,
        AssetOperation.operation_key == key,
    ))
    if operation is None:
        raise AssetLifecycleError(404, "not_found", "操作回执不存在或无权访问")
    return _operation_out(operation)


def _cleanup_expired_selections(session: Session, scope: deps.ProjectScope) -> None:
    expired = select(AssetSelection.id).where(
        AssetSelection.workspace_id == scope.workspace_id,
        AssetSelection.project_id == scope.project_id,
        AssetSelection.principal_id == scope.principal.user_id,
        AssetSelection.expires_at <= datetime.now(UTC),
    ).order_by(AssetSelection.expires_at).limit(100)
    session.execute(delete(AssetSelection).where(AssetSelection.id.in_(expired)))


def _persist_operation(session: Session, scope: deps.ProjectScope, key: str, action: str, request_hash: str, result: dict) -> AssetOperation:
    operation = AssetOperation(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, operation_key=key, action=action,
        request_hash=request_hash, result_schema_version=1, result=result,
    )
    session.add(operation)
    session.flush()
    return operation


def _rejected(session: Session, scope: deps.ProjectScope, key: str, action: str, request_hash: str, code: str, message: str, selection_id=None) -> AssetOperation:
    return _persist_operation(session, scope, key, action, request_hash, {
        "result_kind": "rejected", "selection_id": str(selection_id) if selection_id else None,
        "code": code, "message": message, "no_asset_changes": True, "conflicts": [],
    })


def execute_operation(
    session: Session, scope: deps.ProjectScope, payload: AssetOperationCreate, key: str,
    request_contract: str | None = None,
) -> tuple[AssetOperationOut, bool, bool]:
    key = key.strip()
    if not _KEY.fullmatch(key):
        raise AssetLifecycleError(400, "invalid_idempotency_key", "幂等键格式无效")
    request_hash = _hash(_intent(scope, payload))
    existing = session.scalar(select(AssetOperation).where(
        AssetOperation.workspace_id == scope.workspace_id, AssetOperation.project_id == scope.project_id,
        AssetOperation.principal_id == scope.principal.user_id, AssetOperation.operation_key == key,
    ))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AssetLifecycleError(409, "idempotency_key_conflict", "幂等键已用于不同操作")
        return _operation_out(existing), True, False
    scope = lock_project_and_reauthorize(session, scope, "edit")
    existing = session.scalar(select(AssetOperation).where(
        AssetOperation.workspace_id == scope.workspace_id, AssetOperation.project_id == scope.project_id,
        AssetOperation.principal_id == scope.principal.user_id, AssetOperation.operation_key == key,
    ))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AssetLifecycleError(409, "idempotency_key_conflict", "幂等键已用于不同操作")
        return _operation_out(existing), True, False
    created_asset = False
    if payload.action == "case_copy":
        source = session.scalar(
            select(Case).where(
                Case.workspace_id == scope.workspace_id,
                Case.project_id == scope.project_id,
                Case.id == payload.source_id,
            ).with_for_update().execution_options(populate_existing=True)
        )
        if source is None:
            operation = _rejected(session, scope, key, payload.action, request_hash, "not_found_or_inaccessible", "源用例不存在或无权访问")
        elif source.rev != payload.expected_rev:
            operation = _rejected(session, scope, key, payload.action, request_hash, "revision_conflict", "源用例已更新")
        elif source.status == "archived":
            operation = _rejected(session, scope, key, payload.action, request_hash, "asset_state_conflict", "归档用例不能复制")
        else:
            require_v2_capability(request_contract, needed=is_v2(source.request))
            try:
                request = validate_request(copy.deepcopy(source.request))
                assertions = validate_assertions(copy.deepcopy(source.assertions), request)
            except (RequestSpecError, AssertionSpecError):
                operation = _rejected(session, scope, key, payload.action, request_hash, "source_invalid", "源草稿配置无效")
            else:
                folder_id = payload.folder_id if "folder_id" in payload.model_fields_set else source.folder_id
                try:
                    resolve_available_folder(session, scope, source.folder_id)
                except AssetLifecycleError as error:
                    operation = _rejected(
                        session, scope, key, payload.action, request_hash,
                        "asset_state_conflict", error.message,
                    )
                else:
                    try:
                        resolve_available_folder(session, scope, folder_id)
                    except AssetLifecycleError as error:
                        operation = _rejected(
                            session, scope, key, payload.action, request_hash,
                            "target_invalid", error.message,
                        )
                    else:
                        name = payload.name if payload.name is not None else f"{source.name[:197]} 副本"
                        copied = Case(
                            workspace_id=scope.workspace_id, project_id=scope.project_id,
                            folder_id=folder_id, name=name, request=request, assertions=assertions,
                        )
                        session.add(copied)
                        session.flush()
                        operation = _persist_operation(session, scope, key, payload.action, request_hash, {
                            "result_kind": "completed", "selection_id": None, "root": None,
                            "counts": {"input": 1, "succeeded": 1, "no_change": 0, "conflict": 0, "failed": 0},
                            "items": [{"resource_type": "case", "id": str(copied.id), "outcome": "succeeded", "code": None, "message": "已复制", "new_rev": 1, "asset": _asset_metadata("case", copied)}],
                            "members": [],
                        })
                        created_asset = True
    else:
        selection = session.scalar(select(AssetSelection).where(
            AssetSelection.id == payload.selection_id,
            AssetSelection.workspace_id == scope.workspace_id,
            AssetSelection.project_id == scope.project_id,
            AssetSelection.principal_id == scope.principal.user_id,
        ).with_for_update())
        if selection is None:
            operation = _rejected(session, scope, key, payload.action, request_hash, "selection_missing", "预览不存在，请重新预览", payload.selection_id)
        elif selection.expires_at <= datetime.now(UTC):
            operation = _rejected(session, scope, key, payload.action, request_hash, "selection_expired", "预览已过期，请重新预览", selection.id)
        elif selection.action != payload.action or (selection.target or {}) != _parameters(payload.action, payload.parameters):
            operation = _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "操作与预览不一致", selection.id)
        else:
            try:
                operation = _execute_selection(session, scope, selection, payload, key, request_hash)
            except AssetLifecycleError as error:
                if error.status_code not in {404, 409}:
                    raise
                operation = _rejected(
                    session, scope, key, payload.action, request_hash,
                    error.code, error.message, selection.id,
                )
    session.add(AuditEvent(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, action=payload.action,
        object_type="asset_operation", object_id=str(operation.id),
        diff={"result_kind": operation.result.get("result_kind"), "code": operation.result.get("code")},
    ))
    session.commit()
    return _operation_out(operation), False, created_asset


def _execute_selection(session: Session, scope: deps.ProjectScope, selection: AssetSelection, payload: AssetOperationCreate, key: str, request_hash: str) -> AssetOperation:
    members = selection.members
    if payload.action == "folder_archive":
        root_id = uuid.UUID(selection.selector["root"]["id"])
        descendants = folder_descendants(scope, root_id)
        current_folders = list(session.scalars(select(Folder.id).where(Folder.id.in_(select(descendants.c.id)))))
        current_cases = list(session.scalars(select(Case.id).where(Case.project_id == scope.project_id, Case.folder_id.in_(current_folders))))
        current_keys = {("folder", str(item)) for item in current_folders} | {("case", str(item)) for item in current_cases}
        frozen_keys = {(item["resource_type"], item["id"]) for item in members}
        if current_keys != frozen_keys:
            return _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "目录结构已变化，请重新预览", selection.id)
    locked_folders = list(session.scalars(select(Folder).where(
        Folder.workspace_id == scope.workspace_id,
        Folder.project_id == scope.project_id,
        Folder.id.in_([uuid.UUID(item["id"]) for item in members if item["resource_type"] == "folder"]),
    ).order_by(Folder.id).with_for_update().execution_options(populate_existing=True)))
    locked_cases = list(session.scalars(select(Case).where(
        Case.workspace_id == scope.workspace_id,
        Case.project_id == scope.project_id,
        Case.id.in_([uuid.UUID(item["id"]) for item in members if item["resource_type"] == "case"]),
    ).order_by(Case.id).with_for_update().execution_options(populate_existing=True)))
    objects = {("folder", str(item.id)): item for item in locked_folders} | {("case", str(item.id)): item for item in locked_cases}
    eligible = [item for item in members if item["outcome"] == "eligible"]
    if selection.selector.get("dependencies", []) != _dependency_snapshot(
        session,
        scope,
        [uuid.UUID(item["id"]) for item in selection.selector.get("dependencies", [])],
    ):
        return _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "依赖目录已变化，请重新预览", selection.id)
    for frozen in members:
        obj = objects.get((frozen["resource_type"], frozen["id"]))
        # 预览时不可见的对象不能因同工作空间按ID命中而变成可见；scope查询未命中才是正确结果。
        if frozen.get("rev") is None:
            if obj is not None:
                return _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "资源可见性已变化，请重新预览", selection.id)
            continue
        if obj is None:
            return _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "资源已变化，请重新预览", selection.id)
        current_state = "archived" if (
            obj.status == "archived" if frozen["resource_type"] == "case" else obj.archived_at is not None
        ) else "active"
        current_parent = str(obj.parent_id) if frozen["resource_type"] == "folder" and obj.parent_id else None
        current_folder = str(obj.folder_id) if frozen["resource_type"] == "case" and obj.folder_id else None
        if (
            obj.rev != frozen["rev"]
            or current_state != frozen.get("state")
            or current_parent != frozen.get("parent_id")
            or current_folder != frozen.get("folder_id")
            or (str(obj.archive_operation_id) if obj.archive_operation_id else None) != frozen.get("archive_operation_id")
        ):
            return _rejected(session, scope, key, payload.action, request_hash, "selection_changed", "资源已变化，请重新预览", selection.id)
    _validate_final_dependencies(session, scope, selection, payload, eligible, objects)
    operation = _persist_operation(session, scope, key, payload.action, request_hash, {})
    result_items, archive_members = [], []
    now = datetime.now(UTC)
    params = _parameters(payload.action, payload.parameters)
    for frozen in eligible:
        obj = objects[(frozen["resource_type"], frozen["id"])]
        before_rev = obj.rev
        before_state = "archived" if (obj.status == "archived" if frozen["resource_type"] == "case" else obj.archived_at is not None) else "active"
        original_parent = obj.folder_id if frozen["resource_type"] == "case" else obj.parent_id
        if payload.action == "move":
            target = params["target_folder_id"]
            target_id = uuid.UUID(target) if target else None
            resolve_available_folder(session, scope, target_id)
            if obj.folder_id == target_id:
                result_items.append({"resource_type": "case", "id": str(obj.id), "outcome": "no_change", "code": None, "message": "目录未变化", "new_rev": None, "asset": _asset_metadata("case", obj)})
                continue
            obj.folder_id = target_id
        elif payload.action in {"archive", "folder_archive"}:
            if frozen["resource_type"] == "case":
                obj.status = "archived"
            else:
                obj.archived_at = now
            obj.archive_operation_id = operation.id
        elif payload.action in {"restore", "folder_restore"}:
            if frozen["resource_type"] == "case":
                obj.status = "draft"
                if payload.action == "restore" and "target_folder_id" in params:
                    obj.folder_id = uuid.UUID(params["target_folder_id"]) if params["target_folder_id"] else None
            else:
                obj.archived_at = None
                if str(obj.id) == selection.selector.get("root", {}).get("id"):
                    if "target_parent_id" in params:
                        obj.parent_id = uuid.UUID(params["target_parent_id"]) if params["target_parent_id"] else None
                    if "root_name" in params:
                        obj.name = params["root_name"]
                        obj.normalized_name = obj.name.strip().casefold()
            obj.archive_operation_id = None
        obj.rev += 1
        result_items.append({"resource_type": frozen["resource_type"], "id": str(obj.id), "outcome": "succeeded", "code": None, "message": "操作成功", "new_rev": obj.rev, "asset": _asset_metadata(frozen["resource_type"], obj)})
        if payload.action in {"archive", "folder_archive"}:
            member = AssetArchiveMember(
                workspace_id=scope.workspace_id, project_id=scope.project_id,
                operation_id=operation.id,
                folder_id=obj.id if frozen["resource_type"] == "folder" else None,
                case_id=obj.id if frozen["resource_type"] == "case" else None,
                before_rev=before_rev, archived_rev=obj.rev,
                original_parent_id=original_parent, before_state=before_state,
            )
            session.add(member)
            archive_members.append({"resource_type": frozen["resource_type"], "id": str(obj.id), "before_rev": before_rev, "after_rev": obj.rev, "original_parent_id": str(original_parent) if original_parent else None, "before_state": before_state})
        elif payload.action == "folder_restore":
            archive_members.append({
                "resource_type": frozen["resource_type"],
                "id": str(obj.id),
                "before_rev": before_rev,
                "after_rev": obj.rev,
                "original_parent_id": str(original_parent) if original_parent else None,
                "before_state": before_state,
            })
    for frozen in (item for item in members if item["outcome"] != "eligible"):
        obj = objects.get((frozen["resource_type"], frozen["id"]))
        code = frozen.get("code") or "asset_state_conflict"
        if code == "not_found_or_inaccessible":
            outcome = "not_found_or_inaccessible"
        elif code in {"revision_conflict", "revision_or_state_conflict"}:
            outcome = "conflict"
        else:
            outcome = "no_change"
        result_items.append({
            "resource_type": frozen["resource_type"], "id": frozen["id"],
            "outcome": outcome, "code": code, "message": "未改变",
            "new_rev": None,
            "asset": (
                None
                if frozen.get("rev") is None or frozen.get("code") == "not_found_or_inaccessible"
                else (_asset_metadata(frozen["resource_type"], obj) if obj is not None else None)
            ),
        })
    selected_root = selection.selector.get("root")
    root = (
        {"resource_type": "folder", "id": selected_root["id"]}
        if payload.action in {"folder_archive", "folder_restore"} and selected_root
        else None
    )
    operation.result = {
        "result_kind": "completed", "selection_id": str(selection.id), "root": root,
        "counts": {
            "input": len(result_items),
            "succeeded": sum(i["outcome"] == "succeeded" for i in result_items),
            "no_change": sum(i["outcome"] == "no_change" for i in result_items),
            "conflict": sum(i["outcome"] == "conflict" for i in result_items),
            "failed": sum(i["outcome"] in {"not_found_or_inaccessible", "invalid_target"} for i in result_items),
        },
        "items": result_items, "members": archive_members,
    }
    return operation


def _preview_dependencies(
    session: Session,
    scope: deps.ProjectScope,
    payload: AssetSelectionCreate,
    eligible: list[dict],
    root: dict | None,
) -> list[dict]:
    params = _parameters(payload.action, payload.parameters)
    eligible_folders = {
        uuid.UUID(item["id"]) for item in eligible if item["resource_type"] == "folder"
    }
    dependency_ids: set[uuid.UUID] = set()
    if payload.action == "move":
        target = params.get("target_folder_id")
        if target:
            dependency_ids.add(uuid.UUID(target))
    elif payload.action == "restore":
        target = params.get("target_folder_id") if "target_folder_id" in params else (
            eligible[0].get("folder_id") if eligible else None
        )
        if target:
            dependency_ids.add(uuid.UUID(str(target)))
    elif payload.action == "folder_restore":
        root_id = uuid.UUID(root["id"])
        root_item = next((item for item in eligible if item["resource_type"] == "folder" and item["id"] == str(root_id)), None)
        target = params.get("target_parent_id") if "target_parent_id" in params else (
            root_item.get("parent_id") if root_item else None
        )
        if target:
            dependency_ids.add(uuid.UUID(str(target)))
        for item in eligible:
            if (
                item["resource_type"] == "folder"
                and item["id"] == str(root_id)
                and "target_parent_id" in params
            ):
                # 显式 null/新父已经替换根的旧 parent；不能再把旧父当最终依赖。
                continue
            value = item.get("parent_id") if item["resource_type"] == "folder" else item.get("folder_id")
            if value and uuid.UUID(str(value)) not in eligible_folders:
                dependency_ids.add(uuid.UUID(str(value)))
    elif payload.action == "folder_archive":
        root_id = uuid.UUID(root["id"])
        root_item = next(
            (item for item in eligible if item["resource_type"] == "folder" and item["id"] == str(root_id)),
            None,
        )
        if root_item and root_item.get("parent_id"):
            dependency_ids.add(uuid.UUID(root_item["parent_id"]))
    for folder_id in dependency_ids:
        resolve_available_folder(session, scope, folder_id)
    return _dependency_snapshot(session, scope, sorted(dependency_ids, key=str))


def _dependency_snapshot(
    session: Session, scope: deps.ProjectScope, folder_ids: list[uuid.UUID]
) -> list[dict]:
    result: dict[uuid.UUID, dict] = {}
    pending = list(folder_ids)
    seen: set[uuid.UUID] = set()
    while pending:
        folder_id = pending.pop()
        if folder_id in result:
            continue
        if folder_id in seen:
            raise AssetLifecycleError(409, "folder_unavailable", "依赖目录父链存在循环")
        seen.add(folder_id)
        folder = session.scalar(
            select(Folder).where(
                Folder.workspace_id == scope.workspace_id,
                Folder.project_id == scope.project_id,
                Folder.id == folder_id,
            ).execution_options(populate_existing=True)
        )
        if folder is None:
            raise AssetLifecycleError(409, "folder_unavailable", "依赖目录不存在或不可见")
        result[folder.id] = {
            "id": str(folder.id),
            "rev": folder.rev,
            "parent_id": str(folder.parent_id) if folder.parent_id else None,
            "archived": folder.archived_at is not None,
        }
        if folder.parent_id is not None and folder.parent_id not in result:
            pending.append(folder.parent_id)
    return [result[key] for key in sorted(result, key=str)]


def _validate_final_dependencies(
    session: Session,
    scope: deps.ProjectScope,
    selection: AssetSelection,
    payload: AssetOperationCreate,
    eligible: list[dict],
    objects: dict,
) -> None:
    if not eligible:
        return
    params = _parameters(payload.action, payload.parameters)
    eligible_folder_ids = {
        uuid.UUID(item["id"]) for item in eligible if item["resource_type"] == "folder"
    }
    root_data = selection.selector.get("root") or {}
    root_id = uuid.UUID(root_data["id"]) if root_data.get("id") else None

    if payload.action == "move":
        target = params["target_folder_id"]
        resolve_available_folder(session, scope, uuid.UUID(target) if target else None)
    if payload.action == "restore":
        case_item = next(item for item in eligible if item["resource_type"] == "case")
        case = objects[("case", case_item["id"])]
        target = params.get("target_folder_id") if "target_folder_id" in params else case.folder_id
        resolve_available_folder(session, scope, uuid.UUID(str(target)) if target else None)
    if payload.action != "folder_restore":
        return

    target = params.get("target_parent_id") if "target_parent_id" in params else (
        objects[("folder", str(root_id))].parent_id if root_id else None
    )
    target_id = uuid.UUID(str(target)) if target else None
    if target_id == root_id:
        raise AssetLifecycleError(409, "target_invalid", "目录不能恢复到自身下")
    if target_id is not None:
        descendants = folder_descendants(scope, root_id)
        if session.scalar(select(descendants.c.id).where(descendants.c.id == target_id)):
            raise AssetLifecycleError(409, "target_invalid", "目录不能恢复到自己的后代下")
        resolve_available_folder(session, scope, target_id)

    for item in eligible:
        obj = objects[(item["resource_type"], item["id"])]
        if item["resource_type"] == "folder":
            final_parent = target_id if obj.id == root_id else obj.parent_id
            if final_parent is not None and final_parent not in eligible_folder_ids:
                resolve_available_folder(session, scope, final_parent)
        else:
            final_folder = obj.folder_id
            if final_folder is not None and final_folder not in eligible_folder_ids:
                resolve_available_folder(session, scope, final_folder)

    root = objects[("folder", str(root_id))]
    normalized_name = params.get("root_name", root.name).strip().casefold()
    duplicate = select(Folder.id).where(
        Folder.workspace_id == scope.workspace_id,
        Folder.project_id == scope.project_id,
        Folder.normalized_name == normalized_name,
        Folder.id != root.id,
    )
    duplicate = duplicate.where(Folder.parent_id == target_id) if target_id else duplicate.where(Folder.parent_id.is_(None))
    if session.scalar(duplicate) is not None:
        raise AssetLifecycleError(409, "folder_name_conflict", "目标位置已存在同名目录")
