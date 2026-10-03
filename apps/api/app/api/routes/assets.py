"""用例资产库、目录发现与当前用户个人习惯 API。"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_db
from ...services import case_assets
from .. import deps
from ..schemas import (
    AssetFolderPageOut,
    CaseFavoriteUpdate,
    CaseLibraryPageOut,
    CasePreferenceOut,
    CaseSavedViewCreate,
    CaseSavedViewOut,
    CaseSavedViewUpdate,
)

router = APIRouter(tags=["用例资产"])
_VIEW_SCOPE = Depends(deps.view_scope)


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/case-library",
    response_model=CaseLibraryPageOut,
)
def get_case_library(
    q: str | None = Query(default=None, max_length=200),
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] | None = None,
    state: Literal["active", "archived", "all"] = "active",
    folder: Literal["all", "unfiled", "exact"] = "all",
    folder_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    collection: Literal["all", "favorites", "recent"] = "all",
    sort: Literal[
        "updated_desc", "name_asc", "name_desc", "method_asc", "method_desc", "recent_desc"
    ] = "updated_desc",
    limit: int = 20,
    cursor: str | None = None,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
    settings: Settings = Depends(deps.get_settings_dep),
) -> CaseLibraryPageOut:
    return case_assets.list_case_library(
        session, settings, scope,
        case_assets.CaseLibraryQuery(
            q=q, method=method, state=state,
            folder=folder, folder_id=folder_id, include_descendants=include_descendants,
            collection=collection, sort=sort, limit=limit, cursor=cursor,
        ),
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/asset-folders",
    response_model=AssetFolderPageOut,
)
def get_asset_folders(
    parent_mode: Literal["root", "exact", "all"] = "root",
    parent_id: uuid.UUID | None = None,
    state: Literal["active", "archived", "all"] = "active",
    q: str | None = Query(default=None, max_length=200),
    limit: int = 100,
    cursor: str | None = None,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
    settings: Settings = Depends(deps.get_settings_dep),
) -> AssetFolderPageOut:
    return case_assets.list_asset_folders(
        session, settings, scope,
        case_assets.AssetFolderQuery(
            parent_mode=parent_mode, parent_id=parent_id, state=state,
            q=q, limit=limit, cursor=cursor,
        ),
    )


@router.put(
    "/workspaces/{workspace_id}/projects/{project_id}/case-preferences/{case_id}/favorite",
    response_model=CasePreferenceOut,
)
def put_case_favorite(
    case_id: uuid.UUID,
    payload: CaseFavoriteUpdate,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> CasePreferenceOut:
    return case_assets.set_favorite(session, scope, case_id, payload.favorite)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/case-preferences/{case_id}/opened",
    response_model=CasePreferenceOut,
)
def post_case_opened(
    case_id: uuid.UUID,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> CasePreferenceOut:
    return case_assets.mark_opened(session, scope, case_id)


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/case-views",
    response_model=list[CaseSavedViewOut],
)
def get_case_views(
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> list[CaseSavedViewOut]:
    return case_assets.list_saved_views(session, scope)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/case-views",
    response_model=CaseSavedViewOut,
    status_code=201,
)
def post_case_view(
    payload: CaseSavedViewCreate,
    response: Response,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> CaseSavedViewOut:
    item = case_assets.create_saved_view(session, scope, payload)
    response.headers["ETag"] = f'"{item.rev}"'
    return item


@router.patch(
    "/workspaces/{workspace_id}/projects/{project_id}/case-views/{view_id}",
    response_model=CaseSavedViewOut,
)
def patch_case_view(
    view_id: uuid.UUID,
    payload: CaseSavedViewUpdate,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> CaseSavedViewOut:
    item = case_assets.update_saved_view(session, scope, view_id, payload, if_match)
    response.headers["ETag"] = f'"{item.rev}"'
    return item


@router.delete(
    "/workspaces/{workspace_id}/projects/{project_id}/case-views/{view_id}",
    status_code=204,
)
def delete_case_view(
    view_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> Response:
    case_assets.delete_saved_view(session, scope, view_id, if_match)
    return Response(status_code=204)
