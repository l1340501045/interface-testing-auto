"""S1 用例资产查询、真实游标与个人偏好服务。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, exists, func, literal, or_, select, text, update
from sqlalchemy import case as sql_case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from ..api import deps
from ..api.errors import ApiError, bad_request, conflict, not_found
from ..api.schemas import (
    AssetFolderOut,
    AssetFolderPageOut,
    CaseLibraryItemOut,
    CaseLibraryPageOut,
    CasePreferenceOut,
    CaseSavedViewCreate,
    CaseSavedViewOut,
    CaseSavedViewUpdate,
    FolderAncestorOut,
)
from ..config import Settings
from ..models import (
    Case,
    CasePreference,
    CaseSavedView,
    CaseVersion,
    Folder,
)
from .archive_source import resolve_archive_sources
from .folder_graph import (
    ancestor_walk,
    begin_consistent_read,
    blocked_folder_ids,
    case_folder_paths,
    folder_descendants,
    invalid_folder_ids,
)

_CURSOR_VERSION = 1
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_CASE_SORTS = {
    "updated_desc", "name_asc", "name_desc", "method_asc", "method_desc", "recent_desc"
}


@dataclass(frozen=True)
class CaseLibraryQuery:
    q: str | None
    method: str | None
    state: Literal["active", "archived", "all"]
    folder: Literal["all", "unfiled", "exact"]
    folder_id: uuid.UUID | None
    include_descendants: bool
    collection: Literal["all", "favorites", "recent"]
    sort: str
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class AssetFolderQuery:
    parent_mode: Literal["root", "exact", "all"]
    parent_id: uuid.UUID | None
    state: Literal["active", "archived", "all"]
    q: str | None
    limit: int
    cursor: str | None


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _filter_digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _cursor_key(settings: Settings) -> bytes:
    return settings.load_secret_key()


def _encode_cursor(settings: Settings, payload: dict) -> str:
    body = base64.urlsafe_b64encode(_canonical(payload)).decode().rstrip("=")
    signature = hmac.new(_cursor_key(settings), body.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{body}.{encoded_signature}"


def _decode_cursor(
    settings: Settings,
    token: str,
    *,
    kind: str,
    scope: deps.ProjectScope,
    filter_digest: str,
    sort: str,
) -> list[str]:
    try:
        if len(token) > 4096:
            raise ValueError
        body, encoded_signature = token.split(".", 1)
        expected = hmac.new(_cursor_key(settings), body.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        # urlsafe base64 最后一个字符可能只有部分有效位；若只比较解码后的字节，
        # 改动未使用位仍会得到同一签名，形成多个不同文本表示。游标要求不透明且严格，
        # 因此同时拒绝非规范编码。
        if base64.urlsafe_b64encode(supplied).decode().rstrip("=") != encoded_signature:
            raise ValueError
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
        expected_binding = {
            "v": _CURSOR_VERSION,
            "kind": kind,
            "workspace_id": str(scope.workspace_id),
            "project_id": str(scope.project_id),
            "principal_id": str(scope.principal.user_id),
            "filter": filter_digest,
            "sort": sort,
        }
        if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected_binding.items()):
            raise ValueError
        position = payload.get("position")
        if not isinstance(position, list) or len(position) != 2 or not all(isinstance(v, str) for v in position):
            raise ValueError
        uuid.UUID(position[1])
        return position
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise bad_request("invalid_cursor", "分页游标无效或已不属于当前查询，请从第一页重新加载。") from error


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _case_filter_payload(query: CaseLibraryQuery) -> dict:
    return {
        "q": query.q or None,
        "method": query.method,
        "state": query.state,
        "folder": query.folder,
        "folder_id": str(query.folder_id) if query.folder_id else None,
        "include_descendants": query.include_descendants,
        "collection": query.collection,
    }


def build_case_library_filter_statement(
    scope: deps.ProjectScope, query: CaseLibraryQuery
):
    """构造无事务副作用、无分页的授权筛选查询，供列表与S3冻结共用。"""
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    blocked_exists = func.coalesce(
        or_(Case.folder_id.in_(select(blocked.c.id)), Case.folder_id.in_(select(invalid.c.id))),
        False,
    )
    preference_join = and_(
        CasePreference.workspace_id == scope.workspace_id,
        CasePreference.project_id == scope.project_id,
        CasePreference.principal_id == scope.principal.user_id,
        CasePreference.case_id == Case.id,
    )
    latest = (
        select(CaseVersion.case_id.label("case_id"), func.max(CaseVersion.version).label("latest_version"))
        .where(CaseVersion.workspace_id == scope.workspace_id, CaseVersion.project_id == scope.project_id)
        .group_by(CaseVersion.case_id)
        .subquery()
    )
    method_expr = func.upper(Case.request["method"].as_string())
    path_expr = func.coalesce(Case.request["path"].as_string(), "")
    normalized_name_expr = func.lower(Case.name)
    availability = sql_case(
        (Case.status == "archived", literal("case_archived")),
        (blocked_exists, literal("folder_unavailable")),
        else_=literal("available"),
    ).label("availability")
    asset_status = sql_case(
        (Case.status == "archived", literal("archived")), else_=literal("active")
    ).label("asset_status")
    favorite = func.coalesce(CasePreference.favorite, False).label("favorite")
    statement = (
        select(
            Case.id, Case.name, normalized_name_expr.label("normalized_name"),
            method_expr.label("method"), path_expr.label("path"),
            Case.folder_id, asset_status, availability, Case.rev.label("draft_rev"),
            Case.updated_at, latest.c.latest_version, favorite, CasePreference.last_opened_at,
        )
        .outerjoin(CasePreference, preference_join)
        .outerjoin(latest, latest.c.case_id == Case.id)
        .where(Case.workspace_id == scope.workspace_id, Case.project_id == scope.project_id)
    )
    if query.q:
        pattern = f"%{_escape_like(query.q)}%"
        statement = statement.where(
            or_(Case.name.ilike(pattern, escape="\\"), path_expr.ilike(pattern, escape="\\"))
        )
    if query.method:
        statement = statement.where(method_expr == query.method)
    if query.state == "active":
        statement = statement.where(Case.status != "archived", ~blocked_exists)
    elif query.state == "archived":
        statement = statement.where(or_(Case.status == "archived", blocked_exists))
    if query.folder == "unfiled":
        statement = statement.where(Case.folder_id.is_(None))
    elif query.folder == "exact" and query.folder_id is not None:
        descendants = folder_descendants(scope, query.folder_id)
        statement = statement.where(
            Case.folder_id.in_(select(descendants.c.id))
            if query.include_descendants
            else Case.folder_id == query.folder_id
        )
    if query.collection == "favorites":
        statement = statement.where(CasePreference.favorite.is_(True))
    elif query.collection == "recent":
        statement = statement.where(CasePreference.last_opened_at.is_not(None))
    return statement, normalized_name_expr, method_expr


def list_case_library(
    session: Session,
    settings: Settings,
    scope: deps.ProjectScope,
    query: CaseLibraryQuery,
) -> CaseLibraryPageOut:
    if query.method is not None and query.method not in _METHODS:
        raise bad_request("invalid_case_library_query", "请求方法筛选值无效。")
    if query.sort not in _CASE_SORTS:
        raise bad_request("invalid_case_library_query", "排序方式无效。")
    if query.limit not in {20, 50, 100}:
        raise bad_request("invalid_case_library_query", "每页数量只能是 20、50 或 100。")
    if query.folder == "exact" and query.folder_id is None:
        raise bad_request("invalid_case_library_query", "folder=exact 时必须提供 folder_id。")
    if query.folder != "exact" and (query.folder_id is not None or query.include_descendants):
        raise bad_request("invalid_case_library_query", "folder_id/include_descendants 只允许用于 folder=exact。")
    if query.sort == "recent_desc" and query.collection != "recent":
        raise bad_request("invalid_case_library_query", "recent_desc 只允许用于最近打开集合。")
    scope = begin_consistent_read(session, scope)
    if query.folder_id is not None:
        folder_exists = session.scalar(
            select(Folder.id).where(Folder.project_id == scope.project_id, Folder.id == query.folder_id)
        )
        if folder_exists is None:
            raise not_found("目录不存在或无权访问")

    statement, normalized_name_expr, method_expr = build_case_library_filter_statement(
        scope, query
    )

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    filters = _filter_digest(_case_filter_payload(query))
    position = (
        _decode_cursor(
            settings, query.cursor, kind="cases", scope=scope,
            filter_digest=filters, sort=query.sort,
        )
        if query.cursor else None
    )
    id_value = uuid.UUID(position[1]) if position else None
    if query.sort == "updated_desc":
        primary = Case.updated_at
        if position and id_value:
            cursor_time = datetime.fromisoformat(position[0])
            statement = statement.where(or_(primary < cursor_time, and_(primary == cursor_time, Case.id < id_value)))
        statement = statement.order_by(primary.desc(), Case.id.desc())
    elif query.sort in {"name_asc", "name_desc"}:
        primary = normalized_name_expr
        if position and id_value:
            comparison = primary > position[0] if query.sort == "name_asc" else primary < position[0]
            id_comparison = Case.id > id_value if query.sort == "name_asc" else Case.id < id_value
            statement = statement.where(or_(comparison, and_(primary == position[0], id_comparison)))
        direction = primary.asc() if query.sort == "name_asc" else primary.desc()
        id_direction = Case.id.asc() if query.sort == "name_asc" else Case.id.desc()
        statement = statement.order_by(direction, id_direction)
    elif query.sort in {"method_asc", "method_desc"}:
        primary = method_expr
        if position and id_value:
            comparison = primary > position[0] if query.sort == "method_asc" else primary < position[0]
            id_comparison = Case.id > id_value if query.sort == "method_asc" else Case.id < id_value
            statement = statement.where(or_(comparison, and_(primary == position[0], id_comparison)))
        direction = primary.asc() if query.sort == "method_asc" else primary.desc()
        id_direction = Case.id.asc() if query.sort == "method_asc" else Case.id.desc()
        statement = statement.order_by(direction, id_direction)
    else:
        primary = CasePreference.last_opened_at
        if position and id_value:
            cursor_time = datetime.fromisoformat(position[0])
            statement = statement.where(or_(primary < cursor_time, and_(primary == cursor_time, Case.id < id_value)))
        statement = statement.order_by(primary.desc(), Case.id.desc())

    rows = session.execute(statement.limit(query.limit + 1)).all()
    page_rows = rows[:query.limit]
    path_map = case_folder_paths(
        session,
        scope,
        [row.folder_id for row in page_rows if row.folder_id is not None],
    )
    items = [
        CaseLibraryItemOut(
            id=row.id, name=row.name, method=row.method or "GET", path=row.path,
            folder_id=row.folder_id,
            folder_path=(
                []
                if row.folder_id is None
                else (
                    [{"id": item_id, "name": name} for item_id, name in path_map[row.folder_id]]
                    if path_map.get(row.folder_id) is not None
                    else None
                )
            ),
            asset_status=row.asset_status, availability=row.availability,
            draft_rev=row.draft_rev, updated_at=row.updated_at,
            latest_version=row.latest_version, favorite=row.favorite,
            last_opened_at=row.last_opened_at,
        )
        for row in page_rows
    ]
    next_cursor = None
    if len(rows) > query.limit and page_rows:
        last = page_rows[-1]
        if query.sort == "updated_desc":
            primary_value = last.updated_at.isoformat()
        elif query.sort.startswith("name_"):
            primary_value = last.normalized_name
        elif query.sort.startswith("method_"):
            primary_value = (last.method or "GET").upper()
        else:
            primary_value = last.last_opened_at.isoformat()
        next_cursor = _encode_cursor(
            settings,
            {
                "v": _CURSOR_VERSION, "kind": "cases",
                "workspace_id": str(scope.workspace_id), "project_id": str(scope.project_id),
                "principal_id": str(scope.principal.user_id), "filter": filters,
                "sort": query.sort, "position": [primary_value, str(last.id)],
            },
        )
    return CaseLibraryPageOut(items=items, total=total, next_cursor=next_cursor)


def _folder_filter_payload(query: AssetFolderQuery) -> dict:
    return {
        "parent_mode": query.parent_mode,
        "parent_id": str(query.parent_id) if query.parent_id else None,
        "state": query.state,
        "q": query.q or None,
    }


def _asset_folder_statement(scope: deps.ProjectScope):
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    blocked_exists = Folder.id.in_(select(blocked.c.id))
    invalid_exists = Folder.id.in_(select(invalid.c.id))
    child = aliased(Folder)
    has_children = exists(
        select(child.id).where(
            child.workspace_id == scope.workspace_id,
            child.project_id == scope.project_id,
            child.parent_id == Folder.id,
        )
    ).label("has_children")
    availability = sql_case(
        (invalid_exists, literal("invalid_parent_chain")),
        (Folder.archived_at.is_not(None), literal("archived")),
        (blocked_exists, literal("ancestor_archived")),
        else_=literal("available"),
    ).label("availability")
    return select(
        Folder.id, Folder.name, Folder.normalized_name, Folder.parent_id, Folder.rev,
        Folder.archived_at, availability, has_children, Folder.archive_operation_id,
    ).where(
        Folder.workspace_id == scope.workspace_id,
        Folder.project_id == scope.project_id,
    )


def _asset_folder_items(
    session: Session, scope: deps.ProjectScope, rows: list
) -> list[AssetFolderOut]:
    paths, invalid_path_ids = _ancestor_paths(session, scope, [row.id for row in rows])
    archive_roots = _archive_roots(
        session, scope, [row.archive_operation_id for row in rows if row.archive_operation_id]
    )
    items: list[AssetFolderOut] = []
    for row in rows:
        archive_root_id = archive_roots.get((row.archive_operation_id, row.id)) if row.archive_operation_id else None
        restore_mode = None
        if row.archived_at is not None:
            if row.archive_operation_id is None:
                restore_mode = "legacy_single"
            elif archive_root_id is None:
                restore_mode = "unavailable"
            elif archive_root_id == row.id:
                restore_mode = "batch_root"
            else:
                restore_mode = "locate_root"
        items.append(AssetFolderOut(
            id=row.id, name=row.name, parent_id=row.parent_id, rev=row.rev,
            archived_at=row.archived_at,
            availability=("invalid_parent_chain" if row.id in invalid_path_ids else row.availability),
            has_children=row.has_children, archive_operation_id=row.archive_operation_id,
            archive_root_id=archive_root_id, restore_mode=restore_mode,
            ancestor_path=paths.get(row.id, []),
        ))
    return items


def get_asset_folder(
    session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID
) -> AssetFolderOut:
    scope = begin_consistent_read(session, scope)
    row = session.execute(
        _asset_folder_statement(scope).where(Folder.id == folder_id)
    ).one_or_none()
    if row is None:
        raise not_found("目录不存在或无权访问")
    return _asset_folder_items(session, scope, [row])[0]


def list_asset_folders(
    session: Session,
    settings: Settings,
    scope: deps.ProjectScope,
    query: AssetFolderQuery,
) -> AssetFolderPageOut:
    if not 1 <= query.limit <= 100:
        raise bad_request("invalid_asset_folder_query", "目录每页数量必须在 1 到 100 之间。")
    if query.parent_mode == "exact" and query.parent_id is None:
        raise bad_request("invalid_asset_folder_query", "parent_mode=exact 时必须提供 parent_id。")
    if query.parent_mode != "exact" and query.parent_id is not None:
        raise bad_request("invalid_asset_folder_query", "parent_id 只允许用于 parent_mode=exact。")
    scope = begin_consistent_read(session, scope)
    if query.parent_id is not None:
        parent_exists = session.scalar(
            select(Folder.id).where(
                Folder.workspace_id == scope.workspace_id,
                Folder.project_id == scope.project_id,
                Folder.id == query.parent_id,
            )
        )
        if parent_exists is None:
            raise not_found("目录不存在或无权访问")

    statement = _asset_folder_statement(scope)
    if query.parent_mode == "root":
        statement = statement.where(Folder.parent_id.is_(None))
    elif query.parent_mode == "exact":
        statement = statement.where(Folder.parent_id == query.parent_id)
    if query.state == "active":
        statement = statement.where(Folder.archived_at.is_(None))
    elif query.state == "archived":
        statement = statement.where(Folder.archived_at.is_not(None))
    if query.q:
        statement = statement.where(
            Folder.name.ilike(f"%{_escape_like(query.q)}%", escape="\\")
        )

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    filters = _filter_digest(_folder_filter_payload(query))
    position = (
        _decode_cursor(
            settings, query.cursor, kind="folders", scope=scope,
            filter_digest=filters, sort="name_asc",
        )
        if query.cursor else None
    )
    if position:
        cursor_id = uuid.UUID(position[1])
        statement = statement.where(
            or_(Folder.normalized_name > position[0], and_(Folder.normalized_name == position[0], Folder.id > cursor_id))
        )
    rows = session.execute(statement.order_by(Folder.normalized_name.asc(), Folder.id.asc()).limit(query.limit + 1)).all()
    page_rows = rows[:query.limit]
    items = _asset_folder_items(session, scope, page_rows)
    next_cursor = None
    if len(rows) > query.limit and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(
            settings,
            {
                "v": _CURSOR_VERSION, "kind": "folders",
                "workspace_id": str(scope.workspace_id), "project_id": str(scope.project_id),
                "principal_id": str(scope.principal.user_id), "filter": filters,
                "sort": "name_asc", "position": [last.normalized_name, str(last.id)],
            },
        )
    return AssetFolderPageOut(items=items, total=total, next_cursor=next_cursor)


def _ancestor_paths(
    session: Session, scope: deps.ProjectScope, folder_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[FolderAncestorOut]], set[uuid.UUID]]:
    if not folder_ids:
        return {}, set()
    walk = ancestor_walk(scope, folder_ids)
    rows = session.execute(select(walk).order_by(walk.c.node_id, walk.c.depth.desc())).all()
    result: dict[uuid.UUID, list[FolderAncestorOut]] = {folder_id: [] for folder_id in folder_ids}
    invalid: set[uuid.UUID] = set()
    for row in rows:
        if row.cycle:
            invalid.add(row.node_id)
            continue
        result[row.node_id].append(
            FolderAncestorOut(
                id=row.ancestor_id, name=row.ancestor_name,
                archived=row.ancestor_archived_at is not None,
            )
        )
    return result, invalid


def _archive_roots(
    session: Session,
    scope: deps.ProjectScope,
    operation_ids: list[uuid.UUID],
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID]:
    result: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
    for operation_id, source in resolve_archive_sources(
        session, scope, operation_ids
    ).items():
        for folder_id in source.folder_ids:
            result[(operation_id, folder_id)] = source.root_id
    return result


def _visible_case(session: Session, scope: deps.ProjectScope, case_id: uuid.UUID) -> Case:
    item = session.scalar(select(Case).where(Case.project_id == scope.project_id, Case.id == case_id))
    if item is None:
        raise not_found("用例不存在或无权访问")
    return item


def _preference_lock(session: Session, scope: deps.ProjectScope) -> None:
    key = f"case-preferences:{scope.workspace_id}:{scope.project_id}:{scope.principal.user_id}"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def set_favorite(
    session: Session, scope: deps.ProjectScope, case_id: uuid.UUID, favorite: bool
) -> CasePreferenceOut:
    _visible_case(session, scope, case_id)
    _preference_lock(session, scope)
    statement = insert(CasePreference).values(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, case_id=case_id, favorite=favorite,
    ).on_conflict_do_update(
        constraint="uq_case_preferences_principal_case",
        set_={"favorite": favorite, "updated_at": func.now()},
    ).returning(CasePreference.favorite, CasePreference.last_opened_at)
    row = session.execute(statement).one()
    session.commit()
    return CasePreferenceOut(case_id=case_id, favorite=row.favorite, last_opened_at=row.last_opened_at)


def mark_opened(session: Session, scope: deps.ProjectScope, case_id: uuid.UUID) -> CasePreferenceOut:
    _visible_case(session, scope, case_id)
    _preference_lock(session, scope)
    now = datetime.now(UTC)
    statement = insert(CasePreference).values(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, case_id=case_id, last_opened_at=now,
    ).on_conflict_do_update(
        constraint="uq_case_preferences_principal_case",
        set_={"last_opened_at": now, "updated_at": func.now()},
    ).returning(CasePreference.favorite, CasePreference.last_opened_at)
    row = session.execute(statement).one()
    ranked = (
        select(
            CasePreference.id,
            func.row_number().over(
                order_by=(CasePreference.last_opened_at.desc(), CasePreference.case_id.desc())
            ).label("position"),
        )
        .where(
            CasePreference.workspace_id == scope.workspace_id,
            CasePreference.project_id == scope.project_id,
            CasePreference.principal_id == scope.principal.user_id,
            CasePreference.last_opened_at.is_not(None),
        )
        .subquery()
    )
    session.execute(
        update(CasePreference)
        .where(CasePreference.id.in_(select(ranked.c.id).where(ranked.c.position > 50)))
        .values(last_opened_at=None, updated_at=func.now())
    )
    session.commit()
    return CasePreferenceOut(case_id=case_id, favorite=row.favorite, last_opened_at=row.last_opened_at)


def _saved_view_out(item: CaseSavedView) -> CaseSavedViewOut:
    return CaseSavedViewOut(
        id=item.id, name=item.name, filters=item.filters, rev=item.rev,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def list_saved_views(session: Session, scope: deps.ProjectScope) -> list[CaseSavedViewOut]:
    items = session.scalars(
        select(CaseSavedView).where(
            CaseSavedView.project_id == scope.project_id,
            CaseSavedView.principal_id == scope.principal.user_id,
        ).order_by(CaseSavedView.updated_at.desc(), CaseSavedView.id.desc())
    )
    return [_saved_view_out(item) for item in items]


def create_saved_view(
    session: Session, scope: deps.ProjectScope, payload: CaseSavedViewCreate
) -> CaseSavedViewOut:
    _preference_lock(session, scope)
    count = session.scalar(
        select(func.count()).select_from(CaseSavedView).where(
            CaseSavedView.project_id == scope.project_id,
            CaseSavedView.principal_id == scope.principal.user_id,
        )
    ) or 0
    if count >= 20:
        raise conflict("saved_view_limit_reached", "每个项目最多保存 20 个个人筛选视图。")
    item = CaseSavedView(
        workspace_id=scope.workspace_id, project_id=scope.project_id,
        principal_id=scope.principal.user_id, name=payload.name,
        filters=payload.filters.model_dump(mode="json"),
    )
    session.add(item)
    session.commit()
    return _saved_view_out(item)


def _owned_saved_view_locked(
    session: Session, scope: deps.ProjectScope, view_id: uuid.UUID
) -> CaseSavedView | None:
    return session.scalar(
        select(CaseSavedView).where(
            CaseSavedView.project_id == scope.project_id,
            CaseSavedView.principal_id == scope.principal.user_id,
            CaseSavedView.id == view_id,
        ).with_for_update()
    )


def _check_view_precondition(if_match: str | None, rev: int) -> None:
    if if_match is None:
        raise ApiError(428, "precondition_required", "请先读取最新视图修订再修改。")
    if if_match.strip() not in {"*", f'"{rev}"'}:
        raise ApiError(412, "precondition_failed", "保存视图已在其他位置更新，请刷新后重试。")


def update_saved_view(
    session: Session,
    scope: deps.ProjectScope,
    view_id: uuid.UUID,
    payload: CaseSavedViewUpdate,
    if_match: str | None,
) -> CaseSavedViewOut:
    item = _owned_saved_view_locked(session, scope, view_id)
    if item is None:
        raise not_found("保存视图不存在")
    _check_view_precondition(if_match, item.rev)
    if payload.name is not None:
        item.name = payload.name
    if payload.filters is not None:
        item.filters = payload.filters.model_dump(mode="json")
    item.rev += 1
    session.commit()
    return _saved_view_out(item)


def delete_saved_view(
    session: Session,
    scope: deps.ProjectScope,
    view_id: uuid.UUID,
    if_match: str | None,
) -> None:
    item = _owned_saved_view_locked(session, scope, view_id)
    if item is None:
        return
    _check_view_precondition(if_match, item.rev)
    session.delete(item)
    session.commit()
