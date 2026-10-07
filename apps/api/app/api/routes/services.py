"""S2 项目服务目录与环境映射配置。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...kernel.environment_url import EnvironmentUrlError, normalize_base_url
from ...models import (
    CredentialSet,
    Environment,
    EnvironmentConfigVersion,
    EnvironmentServiceMapping,
    EnvironmentServiceVersion,
    ProjectService,
    RunnerPool,
    RunnerPoolProjectGrant,
)
from ...services import asset_lifecycle
from ...services.credentials import available_profiles
from ...services.folder_graph import begin_consistent_read
from ...services.permissions import can
from ...services.service_targets import (
    ServiceTargetError,
    append_environment_schema2,
    append_mapping_version,
    inspect_selected_target,
    new_service_key,
    normalize_service_name,
)
from .. import deps
from ..errors import ApiError, conflict, not_found
from ..schemas import (
    EnvironmentConfigSummaryOut,
    ProjectServiceCreate,
    ProjectServiceListOut,
    ProjectServiceOut,
    ProjectServiceUpdate,
    ServiceConfigItemOut,
    ServiceConfigOut,
    ServiceConfigUpdate,
    ServiceMappingSummaryOut,
)

router = APIRouter(tags=["服务配置"])
_VIEW = Depends(deps.view_scope)
_EDIT = Depends(deps.edit_scope)


def _gate(session: Session, scope: deps.ProjectScope) -> deps.ProjectScope:
    try:
        return asset_lifecycle.lock_project_and_reauthorize(session, scope, "edit")
    except asset_lifecycle.AssetLifecycleError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


def _if_match(value: str | None, current: int) -> None:
    if value is None:
        raise conflict("config_revision_required", "请先读取最新修订再保存。")
    raw = value.strip().strip('"')
    if not raw.isdecimal() or int(raw) != current:
        raise conflict("config_revision_conflict", "配置已被其他操作更新，请刷新后重试。")


def _service_out(item: ProjectService) -> ProjectServiceOut:
    return ProjectServiceOut(
        id=item.id, service_key=item.service_key, name=item.name,
        is_default=item.is_default, status=item.status, rev=item.rev,
        created_at=item.created_at, updated_at=item.updated_at,
    )


@router.get("/workspaces/{workspace_id}/projects/{project_id}/services", response_model=ProjectServiceListOut)
def list_services(scope: deps.ProjectScope = _VIEW, session: Session = Depends(get_db)) -> ProjectServiceListOut:
    items = session.scalars(select(ProjectService).where(ProjectService.project_id == scope.project_id).order_by(ProjectService.is_default.desc(), ProjectService.created_at))
    return ProjectServiceListOut(items=[_service_out(item) for item in items])


@router.post("/workspaces/{workspace_id}/projects/{project_id}/services", response_model=ProjectServiceOut, status_code=201)
def create_service(payload: ProjectServiceCreate, scope: deps.ProjectScope = _EDIT, session: Session = Depends(get_db)) -> ProjectServiceOut:
    scope = _gate(session, scope)
    try:
        name, normalized = normalize_service_name(payload.name)
    except ServiceTargetError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    if session.scalar(select(ProjectService.id).where(ProjectService.project_id == scope.project_id, ProjectService.normalized_name == normalized)):
        raise conflict("service_name_exists", "服务名称已存在")
    item = ProjectService(workspace_id=scope.workspace_id, project_id=scope.project_id, service_key=new_service_key(), name=name, normalized_name=normalized)
    session.add(item)
    deps.commit(session)
    return _service_out(item)


@router.patch("/workspaces/{workspace_id}/projects/{project_id}/services/{service_id}", response_model=ProjectServiceOut)
def update_service(service_id: uuid.UUID, payload: ProjectServiceUpdate, if_match: str | None = Header(default=None, alias="If-Match"), scope: deps.ProjectScope = _EDIT, session: Session = Depends(get_db)) -> ProjectServiceOut:
    scope = _gate(session, scope)
    item = session.scalar(select(ProjectService).where(ProjectService.id == service_id, ProjectService.project_id == scope.project_id).with_for_update().execution_options(populate_existing=True))
    if item is None:
        raise not_found("服务不存在")
    _if_match(if_match, item.rev)
    if payload.status is not None:
        if item.is_default:
            raise conflict("service_unavailable", "默认服务不能停用")
        item.status = payload.status
    if payload.name is not None:
        try:
            name, normalized = normalize_service_name(payload.name)
        except ServiceTargetError as error:
            raise ApiError(error.status_code, error.code, error.message) from error
        duplicate = session.scalar(select(ProjectService.id).where(ProjectService.project_id == scope.project_id, ProjectService.normalized_name == normalized, ProjectService.id != item.id))
        if duplicate:
            raise conflict("service_name_exists", "服务名称已存在")
        item.name, item.normalized_name = name, normalized
    item.rev += 1
    deps.commit(session)
    return _service_out(item)


def _mapping_summary(session: Session, mapping: EnvironmentServiceMapping | None) -> ServiceMappingSummaryOut | None:
    if mapping is None:
        return None
    version = session.scalar(select(EnvironmentServiceVersion).where(EnvironmentServiceVersion.id == mapping.current_version_id, EnvironmentServiceVersion.mapping_id == mapping.id))
    if version is None:
        return None
    return ServiceMappingSummaryOut(id=mapping.id, rev=mapping.rev, status=mapping.status, base_url=version.base_url, version=version.version, version_id=version.id)


def _inheritance(session: Session, environment: Environment, scope: deps.ProjectScope) -> dict:
    if not can(scope.role, "execute"):
        identity_state = "unchecked"
    else:
        profiles = available_profiles(session, environment.id)
        if not profiles:
            identity_state = "none"
        elif len(profiles) > 1:
            identity_state = "ambiguous"
        else:
            profile = profiles[0]
            credential_set = (
                session.get(CredentialSet, profile.current_set_id)
                if profile.current_set_id is not None
                else None
            )
            identity_state = (
                "ready"
                if profile.status == "available"
                and credential_set is not None
                and credential_set.status == "active"
                and (
                    credential_set.expires_at is None
                    or credential_set.expires_at > datetime.now(UTC)
                )
                else "unavailable"
            )
    identity = {"mode": "shared_environment", "state": identity_state}
    pool = session.get(RunnerPool, environment.pool_id) if environment.pool_id else None
    if pool is None and environment.pool_id is None:
        pool = session.scalar(
            select(RunnerPool)
            .join(RunnerPoolProjectGrant, RunnerPoolProjectGrant.pool_id == RunnerPool.id)
            .where(
                RunnerPoolProjectGrant.project_id == scope.project_id,
                RunnerPoolProjectGrant.status == "active",
                RunnerPool.status == "active",
            )
            .order_by(RunnerPoolProjectGrant.created_at)
            .limit(1)
        )
    if pool is None:
        pool_out = {"state": "missing"}
    elif pool.status != "active":
        pool_out = {"state": "disabled", "id": str(pool.id), "name": pool.name, "status": pool.status}
    elif not session.scalar(select(RunnerPoolProjectGrant.id).where(RunnerPoolProjectGrant.project_id == scope.project_id, RunnerPoolProjectGrant.pool_id == pool.id, RunnerPoolProjectGrant.status == "active")):
        pool_out = {"state": "not_granted", "id": str(pool.id), "name": pool.name, "status": pool.status}
    else:
        pool_out = {"state": "ready", "id": str(pool.id), "name": pool.name, "status": pool.status}
    return {"identity": identity, "pool": pool_out}


def _config_out(session: Session, environment: Environment, scope: deps.ProjectScope) -> ServiceConfigOut:
    config = session.scalar(select(EnvironmentConfigVersion).where(EnvironmentConfigVersion.id == environment.current_config_version_id, EnvironmentConfigVersion.environment_id == environment.id))
    config_version = (
        config.version
        if config is not None
        and config.schema_version == 2
        and isinstance(config.snapshot, dict)
        and isinstance(config.snapshot.get("service_versions"), dict)
        else None
    )
    services = list(session.scalars(select(ProjectService).where(ProjectService.project_id == scope.project_id).order_by(ProjectService.is_default.desc(), ProjectService.created_at)))
    items = []
    for service in services:
        if config_version is None:
            summary = None
            availability = "config_inconsistent"
        else:
            request = (
                {}
                if service.is_default
                else {"service_contract": 1, "service_key": service.service_key}
            )
            selected_summary, _selected, _error = inspect_selected_target(
                session, environment, request
            )
            raw_mapping = selected_summary["mapping"]
            summary = (
                ServiceMappingSummaryOut(**raw_mapping)
                if isinstance(raw_mapping, dict)
                else None
            )
            availability = selected_summary["availability"]
        items.append(ServiceConfigItemOut(service_id=service.id, service_key=service.service_key, service_name=service.name, is_default=service.is_default, service_status=service.status, service_rev=service.rev, mapping=summary, availability=availability))
    return ServiceConfigOut(environment=EnvironmentConfigSummaryOut(id=environment.id, name=environment.name, kind=environment.kind, status=environment.status, rev=environment.rev, config_version=config_version), inheritance=_inheritance(session, environment, scope), items=items)


@router.get("/workspaces/{workspace_id}/projects/{project_id}/environments/{environment_id}/service-config", response_model=ServiceConfigOut)
def get_service_config(environment_id: uuid.UUID, scope: deps.ProjectScope = _VIEW, session: Session = Depends(get_db)) -> ServiceConfigOut:
    scope = begin_consistent_read(session, scope)
    environment = session.scalar(select(Environment).where(Environment.id == environment_id, Environment.project_id == scope.project_id))
    if environment is None:
        raise not_found("环境不存在")
    return _config_out(session, environment, scope)


@router.patch("/workspaces/{workspace_id}/projects/{project_id}/environments/{environment_id}/service-config", response_model=ServiceConfigOut)
def update_service_config(environment_id: uuid.UUID, payload: ServiceConfigUpdate, if_match: str | None = Header(default=None, alias="If-Match"), scope: deps.ProjectScope = _EDIT, session: Session = Depends(get_db)) -> ServiceConfigOut:
    scope = _gate(session, scope)
    environment = session.scalar(select(Environment).where(Environment.id == environment_id, Environment.project_id == scope.project_id).with_for_update().execution_options(populate_existing=True))
    if environment is None:
        raise not_found("环境不存在")
    _if_match(if_match, environment.rev)
    for change in sorted(payload.items, key=lambda item: item.service_key):
        service = session.scalar(select(ProjectService).where(ProjectService.project_id == scope.project_id, ProjectService.service_key == change.service_key).with_for_update())
        if service is None:
            raise ApiError(400, "service_invalid", "服务不存在或不属于本项目")
        mapping = session.scalar(select(EnvironmentServiceMapping).where(EnvironmentServiceMapping.environment_id == environment.id, EnvironmentServiceMapping.service_id == service.id).with_for_update())
        if service.is_default:
            if change.base_url is None or change.status is not None or mapping is None:
                raise ApiError(422, "invalid_request", "默认服务必须提交地址且不能独立改状态")
            _if_match(str(change.expected_mapping_rev) if change.expected_mapping_rev else None, mapping.rev)
            try:
                version = append_mapping_version(session, mapping, base_url=change.base_url, status=environment.status, created_by=scope.principal.user_id)
            except ServiceTargetError as error:
                raise ApiError(error.status_code, error.code, error.message) from error
            environment.base_url = version.base_url
        elif mapping is None:
            if service.status != "active" or change.base_url is None or change.expected_mapping_rev is not None:
                raise ApiError(422, "invalid_request", "新映射必须为活动服务提供地址且不带旧修订")
            try:
                new_base_url = normalize_base_url(change.base_url)
            except EnvironmentUrlError as error:
                raise ApiError(400, "environment_url_invalid", str(error)) from error
            mapping_id, version_id = uuid.uuid4(), uuid.uuid4()
            mapping = EnvironmentServiceMapping(id=mapping_id, workspace_id=scope.workspace_id, project_id=scope.project_id, environment_id=environment.id, service_id=service.id, rev=1, status=change.status or "active", current_version_id=version_id)
            version = EnvironmentServiceVersion(id=version_id, workspace_id=scope.workspace_id, project_id=scope.project_id, environment_id=environment.id, service_id=service.id, mapping_id=mapping_id, version=1, base_url=new_base_url, status=change.status or "active", created_by=scope.principal.user_id)
            session.add_all([mapping, version])
            session.flush()
        else:
            if service.status != "active" and (
                change.base_url is not None or change.status != "disabled"
            ):
                raise conflict(
                    "service_unavailable", "已归档服务只能保留或停用既有映射"
                )
            _if_match(str(change.expected_mapping_rev) if change.expected_mapping_rev else None, mapping.rev)
            current = _mapping_summary(session, mapping)
            base_url = change.base_url or (current.base_url if current else None)
            if base_url is None:
                raise conflict("config_inconsistent", "映射当前版本缺失")
            try:
                append_mapping_version(session, mapping, base_url=base_url, status=change.status or mapping.status, created_by=scope.principal.user_id)
            except ServiceTargetError as error:
                raise ApiError(error.status_code, error.code, error.message) from error
    environment.rev += 1
    append_environment_schema2(session, environment, created_by=scope.principal.user_id)
    deps.commit(session)
    return _config_out(session, environment, scope)
