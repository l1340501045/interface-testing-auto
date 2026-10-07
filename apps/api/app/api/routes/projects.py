"""工作空间、项目、环境、普通变量与目录的最小管理接口。

环境固定绑定执行池，创建运行时不接受客户端传入 pool_id；普通变量按
不可变版本新增，环境级普通变量不接收秘密。
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_db
from ...kernel.environment_url import EnvironmentUrlError, normalize_base_url
from ...kernel.target_policy import TargetPolicyError, normalize_origin
from ...kernel.valueliteral import ValueLiteral, ValueLiteralError
from ...models import (
    Environment,
    EnvironmentConfigVersion,
    EnvironmentServiceMapping,
    EnvironmentServiceVersion,
    Folder,
    Project,
    ProjectConfigVersion,
    ProjectService,
    RunnerPool,
    RunnerPoolProjectGrant,
    WorkspaceMembership,
)
from ...services import asset_lifecycle
from ...services.folder_graph import (
    begin_consistent_read,
    blocked_folder_ids,
    folder_descendants,
    invalid_folder_ids,
)
from ...services.permissions import require
from ...services.resolution import ResolutionError, variable_context
from ...services.service_targets import (
    ServiceTargetError,
    append_environment_schema2,
    append_mapping_version,
    ensure_default_service,
    inspect_selected_target,
)
from .. import deps
from ..errors import ApiError, bad_request, conflict, not_found
from ..request_contract import require_service_capability
from ..schemas import (
    EnvironmentCreate,
    EnvironmentOut,
    EnvironmentUpdate,
    FolderCreate,
    FolderOut,
    FolderUpdate,
    ProjectCreate,
    ProjectOut,
    RunnerPoolOut,
    RunnerPoolTargetsUpdate,
    VariableContextOut,
    VariableItem,
    VariablesOut,
    VariablesUpdate,
    WorkspaceMemberOut,
    WorkspaceOut,
)

router = APIRouter(tags=["项目配置"])

_EDIT_SCOPE = Depends(deps.edit_scope)
_VIEW_SCOPE = Depends(deps.view_scope)
_POOL_ADMIN_SCOPE = Depends(deps.pool_admin_scope)


def _asset_gate(session: Session, scope: deps.ProjectScope) -> deps.ProjectScope:
    try:
        return asset_lifecycle.lock_project_and_reauthorize(session, scope, "edit")
    except asset_lifecycle.AssetLifecycleError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


def _folder_precondition(if_match: str | None, rev: int) -> None:
    if if_match is None:
        raise ApiError(428, "precondition_required", "请先读取最新目录修订再修改。")
    if if_match.strip() not in {"*", f'"{rev}"'}:
        raise conflict("revision_conflict", "目录已被其他操作更新，请刷新后重试。")


def _available_folder(
    session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID
) -> Folder:
    try:
        folder = asset_lifecycle.resolve_available_folder(session, scope, folder_id)
    except asset_lifecycle.AssetLifecycleError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    assert folder is not None
    return folder


# —— 工作空间 ——


@router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(
    session: Session = Depends(get_db),
    principal: deps.Principal = Depends(deps.get_principal),
) -> list[WorkspaceOut]:
    memberships = {
        membership.workspace_id: membership.role
        for membership in session.scalars(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == principal.user_id)
        )
    }
    return [
        WorkspaceOut(id=item.id, name=item.name, role=memberships.get(item.id, "viewer"))
        for item in deps.visible_workspaces(session, principal)
    ]


# —— 项目 ——


@router.get("/workspaces/{workspace_id}/members", response_model=list[WorkspaceMemberOut])
def list_workspace_members(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: deps.Principal = Depends(deps.get_principal),
) -> list[WorkspaceMemberOut]:
    """工作空间成员名单：只给姓名与角色，供签发授权时按姓名选主体。

    成员表自身的 RLS 只放行本人那一行，跨成员读取收敛在
    `app.list_workspace_members`（SECURITY DEFINER，属主为不可登录的 broker），
    且调用者必须是该工作空间的成员。非成员在这里先按 404 处理，与成员表策略一致，
    不泄露“这个工作空间存不存在”。
    """
    role = deps.workspace_role(session, principal, workspace_id)
    require(role, "view")
    rows = session.execute(
        text(
            "SELECT user_id, username, display_name, role "
            "FROM app.list_workspace_members(:workspace_id, :caller_id)"
        ),
        {"workspace_id": str(workspace_id), "caller_id": str(principal.user_id)},
    ).all()
    return [
        WorkspaceMemberOut(
            user_id=row.user_id,
            username=row.username,
            display_name=row.display_name,
            role=row.role,
        )
        for row in rows
    ]


@router.get("/workspaces/{workspace_id}/projects", response_model=list[ProjectOut])
def list_projects(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: deps.Principal = Depends(deps.get_principal),
) -> list[ProjectOut]:
    role = deps.workspace_role(session, principal, workspace_id)
    deps.apply_tenant(session, workspace_id, principal.user_id)
    projects = list(session.scalars(select(Project).order_by(Project.created_at)))
    # PostgreSQL 没有 min(uuid) 聚合，按授权创建顺序在应用侧取首个有效池。
    pool_ids: dict[uuid.UUID, uuid.UUID] = {}
    for project_id, pool_id in session.execute(
        select(RunnerPoolProjectGrant.project_id, RunnerPoolProjectGrant.pool_id)
        .where(RunnerPoolProjectGrant.status == "active")
        .order_by(RunnerPoolProjectGrant.project_id, RunnerPoolProjectGrant.created_at)
    ).all():
        pool_ids.setdefault(project_id, pool_id)
    return [
        ProjectOut(
            id=item.id,
            workspace_id=item.workspace_id,
            key=item.key,
            name=item.name,
            status=item.status,
            role=role,
            pool_id=pool_ids.get(item.id),
        )
        for item in projects
    ]


_DEFAULT_POOL_NAME = "默认执行池"


def _default_allowlist(settings: Settings) -> list[str]:
    entries = [item.strip() for item in settings.controlled_targets.split(",") if item.strip()]
    try:
        return [normalize_origin(item) for item in entries]
    except TargetPolicyError as error:
        raise bad_request("controlled_target_invalid", f"受控目标配置无效：{error}") from error


def _ensure_default_pool(
    session: Session, settings: Settings, workspace_id: uuid.UUID, project_id: uuid.UUID, granted_by: uuid.UUID
) -> RunnerPool:
    """为项目建立默认执行池并显式授权。

    创建项目是管理员动作，同事务写入池与授权记录；运行接口不接受客户端传入
    pool_id，环境在服务端绑定该池，因此普通编辑者无法自选网络出口。
    """
    pool = RunnerPool(
        workspace_id=workspace_id,
        name=_DEFAULT_POOL_NAME,
        allowed_targets=_default_allowlist(settings),
    )
    session.add(pool)
    session.flush()
    session.add(
        RunnerPoolProjectGrant(
            workspace_id=workspace_id,
            project_id=project_id,
            pool_id=pool.id,
            granted_by=granted_by,
        )
    )
    session.flush()
    return pool


@router.post("/workspaces/{workspace_id}/projects", response_model=ProjectOut, status_code=201)
def create_project(
    workspace_id: uuid.UUID,
    payload: ProjectCreate,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
    principal: deps.Principal = Depends(deps.get_principal),
) -> ProjectOut:
    role = deps.workspace_role(session, principal, workspace_id)
    require(role, "administer")
    deps.apply_tenant(session, workspace_id, principal.user_id)
    existing = session.scalar(
        select(Project).where(Project.workspace_id == workspace_id, Project.key == payload.key)
    )
    if existing is not None:
        raise conflict("project_key_exists", "项目键已存在，请更换")
    project = Project(workspace_id=workspace_id, key=payload.key, name=payload.name)
    session.add(project)
    session.flush()
    pool = _ensure_default_pool(session, settings, workspace_id, project.id, principal.user_id)
    ensure_default_service(session, workspace_id=workspace_id, project_id=project.id)
    deps.commit(session)
    return ProjectOut(
        id=project.id,
        workspace_id=project.workspace_id,
        key=project.key,
        name=project.name,
        status=project.status,
        role=role,
        pool_id=pool.id,
    )


# —— 环境 ——


def _environment_out(session: Session, item: Environment) -> EnvironmentOut:
    config_version = session.scalar(
        select(EnvironmentConfigVersion.version).where(
            EnvironmentConfigVersion.id == item.current_config_version_id,
            EnvironmentConfigVersion.environment_id == item.id,
        )
    )
    return EnvironmentOut(
        id=item.id,
        name=item.name,
        kind=item.kind,
        base_url=item.base_url,
        pool_id=item.pool_id,
        variables=item.variables,
        status=item.status,
        rev=item.rev,
        config_version=config_version,
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/environments",
    response_model=list[EnvironmentOut],
)
def list_environments(
    scope: deps.ProjectScope = _VIEW_SCOPE, session: Session = Depends(get_db)
) -> list[EnvironmentOut]:
    items = session.scalars(
        select(Environment).where(Environment.project_id == scope.project_id).order_by(Environment.name)
    )
    return [_environment_out(session, item) for item in items]


def _config_precondition(if_match: str | None) -> int:
    """解析配置修订条件；S1 明确拒绝无条件覆盖及通配符。"""
    if if_match is None:
        raise conflict("config_revision_required", "请先读取最新配置修订再保存。")
    value = if_match.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value.isascii() or not value.isdecimal():
        raise conflict("config_revision_conflict", "配置修订条件无效，请刷新后重试。")
    return int(value)


def _append_environment_config_version(
    session: Session,
    environment: Environment,
    *,
    created_by: uuid.UUID | None,
) -> EnvironmentConfigVersion:
    current = session.scalar(
        select(func.max(EnvironmentConfigVersion.version)).where(
            EnvironmentConfigVersion.environment_id == environment.id
        )
    )
    version = EnvironmentConfigVersion(
        workspace_id=environment.workspace_id,
        project_id=environment.project_id,
        environment_id=environment.id,
        version=(current or 0) + 1,
        schema_version=1,
        snapshot={"schema_version": 1, "variables": dict(environment.variables or {})},
        created_by=created_by,
    )
    session.add(version)
    session.flush()
    environment.current_config_version_id = version.id
    return version


def _checked_base_url(raw: str) -> str:
    """环境地址在写库前做语法校验，返回将与请求一起提交的同一份文本。

    过去这里只检查了字符串长度，环境表里因此可以存下裸主机名或“主机:端口”这类缺协议
    的地址；它们拼不出可解析的目标，直到执行时才失败，并且被归到“目标不在白名单”，把
    用户引向去修改本来正确的用例路径。地址本身的问题必须在保存时就报出来。

    校验只做语法判断：不解析 DNS、不建立连接，也不回显原始地址（粘贴来的原文里可能带
    凭证）。错误码固定为 `environment_url_invalid`，界面据此把错误指到地址输入框。
    """
    try:
        return normalize_base_url(raw)
    except EnvironmentUrlError as error:
        raise bad_request("environment_url_invalid", str(error)) from error


def _granted_pool_id(session: Session, scope: deps.ProjectScope) -> uuid.UUID:
    """项目已授权的活动执行池；没有可用池时直接拒绝创建环境。"""
    pool_id = session.scalar(
        select(RunnerPoolProjectGrant.pool_id)
        .join(RunnerPool, RunnerPool.id == RunnerPoolProjectGrant.pool_id)
        .where(
            RunnerPoolProjectGrant.project_id == scope.project_id,
            RunnerPoolProjectGrant.status == "active",
            RunnerPool.status == "active",
        )
        .order_by(RunnerPoolProjectGrant.created_at)
        .limit(1)
    )
    if pool_id is None:
        raise conflict("pool_not_granted", "本项目没有已授权的可用执行池，请联系执行池管理者。")
    return pool_id


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/environments",
    response_model=EnvironmentOut,
    status_code=201,
)
def create_environment(
    payload: EnvironmentCreate,
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> EnvironmentOut:
    scope = _asset_gate(session, scope)
    if payload.kind == "production":
        raise bad_request("production_not_enabled", "本阶段不启用生产环境执行，请先创建测试环境。")
    base_url = _checked_base_url(payload.base_url)
    duplicate = session.scalar(
        select(Environment).where(
            Environment.project_id == scope.project_id, Environment.name == payload.name
        )
    )
    if duplicate is not None:
        raise conflict("environment_name_exists", "环境名称已存在")
    # 环境在服务端绑定执行池；调用方不能指定池，避免自选网络出口。
    pool_id = _granted_pool_id(session, scope)
    variables = _validate_variables(list((payload.variables or {}).items()))
    environment_id = uuid.uuid4()
    config_version_id = uuid.uuid4()
    mapping_id, mapping_version_id = uuid.uuid4(), uuid.uuid4()
    default_service = ensure_default_service(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id
    )
    environment = Environment(
        id=environment_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        name=payload.name,
        kind=payload.kind,
        base_url=base_url,
        pool_id=pool_id,
        variables=variables,
        current_config_version_id=config_version_id,
    )
    mapping = EnvironmentServiceMapping(
        id=mapping_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        environment_id=environment_id,
        service_id=default_service.id,
        rev=1,
        status="active",
        current_version_id=mapping_version_id,
    )
    mapping_version = EnvironmentServiceVersion(
        id=mapping_version_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        environment_id=environment_id,
        service_id=default_service.id,
        mapping_id=mapping_id,
        version=1,
        base_url=base_url,
        status="active",
        created_by=scope.principal.user_id,
    )
    config_version = EnvironmentConfigVersion(
        id=config_version_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        environment_id=environment_id,
        version=1,
        schema_version=2,
        snapshot={
            "schema_version": 2,
            "variables": dict(variables),
            "service_versions": {
                str(default_service.id): {
                    "mapping_id": str(mapping_id),
                    "mapping_version_id": str(mapping_version_id),
                }
            },
        },
        created_by=scope.principal.user_id,
    )
    session.add_all([environment, mapping, mapping_version, config_version])
    deps.commit(session)
    return _environment_out(session, environment)


@router.patch(
    "/workspaces/{workspace_id}/projects/{project_id}/environments/{environment_id}",
    response_model=EnvironmentOut,
)
def update_environment(
    environment_id: uuid.UUID,
    payload: EnvironmentUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> EnvironmentOut:
    expected_rev = _config_precondition(if_match)
    scope = _asset_gate(session, scope)
    environment = session.scalar(
        select(Environment)
        .where(Environment.id == environment_id, Environment.project_id == scope.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if environment is None:
        raise not_found("环境不存在")
    if environment.rev != expected_rev:
        raise conflict("config_revision_conflict", "环境配置已被其他操作更新，请刷新后重试。")
    # 地址先校验再改任何字段：非法地址必须整条 PATCH 失败，不能留下“地址没改、变量改了”
    # 这种改到一半的记录，也不能推进 rev。
    base_url = _checked_base_url(payload.base_url) if payload.base_url is not None else None
    if payload.name is not None:
        environment.name = payload.name
    default_mapping = session.scalar(
        select(EnvironmentServiceMapping)
        .where(EnvironmentServiceMapping.environment_id == environment.id)
        .join(
            ProjectService,
            ProjectService.id == EnvironmentServiceMapping.service_id,
        )
        .where(ProjectService.is_default.is_(True))
        .with_for_update()
    )
    if default_mapping is None:
        raise conflict("config_inconsistent", "默认服务映射缺失")
    current_default = session.get(EnvironmentServiceVersion, default_mapping.current_version_id)
    if payload.variables is not None:
        # 环境级普通变量与项目级走同一校验：非法字面量与秘密都不能从这条入口进来。
        environment.variables = _validate_variables(list(payload.variables.items()))
    if payload.status is not None:
        environment.status = payload.status
    if base_url is not None or payload.status is not None:
        if current_default is None:
            raise conflict("config_inconsistent", "默认服务映射当前版本缺失")
        try:
            version = append_mapping_version(
                session,
                default_mapping,
                base_url=base_url or current_default.base_url,
                status="archived" if environment.status == "archived" else "active",
                created_by=scope.principal.user_id,
            )
        except ServiceTargetError as error:
            raise ApiError(error.status_code, error.code, error.message) from error
        environment.base_url = version.base_url
    environment.rev += 1
    append_environment_schema2(session, environment, created_by=scope.principal.user_id)
    deps.commit(session)
    return _environment_out(session, environment)


def _get_environment(session: Session, scope: deps.ProjectScope, environment_id: uuid.UUID) -> Environment:
    environment = session.scalar(
        select(Environment).where(
            Environment.id == environment_id, Environment.project_id == scope.project_id
        )
    )
    if environment is None:
        raise not_found("环境不存在")
    return environment


# —— 执行池授权与目标白名单 ——
#
# 白名单决定运行器“能访问哪些目标”，属于出网边界的维护入口，与凭证（决定“以谁的
# 身份访问”）分开授权、分开审计。这里只读写配置：保存白名单不解析 DNS、不建立
# 连接，也不试发任何请求，因此停掉全部目标服务也能把配置改完。
# 运行接口依旧不接受客户端传入 pool_id，普通编辑者无法借此自选网络出口。


def _pool_out(
    grant: RunnerPoolProjectGrant, pool: RunnerPool, environment_ids: list[uuid.UUID]
) -> RunnerPoolOut:
    return RunnerPoolOut(
        id=pool.id,
        name=pool.name,
        status=pool.status,
        network_zone=pool.network_zone,
        allowed_targets=list(pool.allowed_targets or []),
        grant_id=grant.id,
        grant_status=grant.status,
        granted_at=grant.created_at,
        environment_ids=environment_ids,
    )


def _bound_environment_ids(
    session: Session, scope: deps.ProjectScope, pool_id: uuid.UUID
) -> list[uuid.UUID]:
    """绑定到该池的环境 id；用于说明“改这一条会影响到哪些环境”。"""
    return list(
        session.scalars(
            select(Environment.id)
            .where(Environment.project_id == scope.project_id, Environment.pool_id == pool_id)
            .order_by(Environment.name)
        )
    )


def _get_granted_pool(
    session: Session, scope: deps.ProjectScope, pool_id: uuid.UUID
) -> tuple[RunnerPoolProjectGrant, RunnerPool]:
    """只认本项目有效授权下的池；其他项目的池按不存在处理，不泄露其存在性。"""
    row = session.execute(
        select(RunnerPoolProjectGrant, RunnerPool)
        .join(RunnerPool, RunnerPool.id == RunnerPoolProjectGrant.pool_id)
        .where(
            RunnerPoolProjectGrant.project_id == scope.project_id,
            RunnerPoolProjectGrant.pool_id == pool_id,
            RunnerPoolProjectGrant.status == "active",
        )
    ).one_or_none()
    if row is None:
        raise not_found("执行池不存在或未授权给本项目")
    return row[0], row[1]


def _normalize_targets(entries: list[str]) -> list[str]:
    """逐条规范化白名单；非法条目报配置错误，不静默丢弃。"""
    targets: list[str] = []
    for raw in entries:
        text = raw.strip()
        if not text:
            raise bad_request("target_invalid", "白名单条目不能为空")
        try:
            origin = normalize_origin(text)
        except TargetPolicyError as error:
            raise bad_request("target_invalid", f"白名单条目无效：{error}") from error
        if origin not in targets:
            # 同一条目写成 host 与 http://host:80 是同一个来源，去重而不是并存。
            targets.append(origin)
    if not targets:
        raise bad_request("target_invalid", "白名单至少要保留一个目标")
    return targets


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/pools", response_model=list[RunnerPoolOut]
)
def list_project_pools(
    scope: deps.ProjectScope = _POOL_ADMIN_SCOPE, session: Session = Depends(get_db)
) -> list[RunnerPoolOut]:
    rows = session.execute(
        select(RunnerPoolProjectGrant, RunnerPool)
        .join(RunnerPool, RunnerPool.id == RunnerPoolProjectGrant.pool_id)
        .where(
            RunnerPoolProjectGrant.project_id == scope.project_id,
            RunnerPoolProjectGrant.status == "active",
        )
        .order_by(RunnerPoolProjectGrant.created_at)
    ).all()
    environment_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for environment_id, pool_id in session.execute(
        select(Environment.id, Environment.pool_id).where(
            Environment.project_id == scope.project_id, Environment.pool_id.is_not(None)
        )
    ).all():
        environment_ids.setdefault(pool_id, []).append(environment_id)
    return [_pool_out(grant, pool, environment_ids.get(pool.id, [])) for grant, pool in rows]


@router.put(
    "/workspaces/{workspace_id}/projects/{project_id}/pools/{pool_id}/targets",
    response_model=RunnerPoolOut,
)
def update_pool_targets(
    pool_id: uuid.UUID,
    payload: RunnerPoolTargetsUpdate,
    scope: deps.ProjectScope = _POOL_ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> RunnerPoolOut:
    """替换白名单整份内容；保存只落配置，不访问任何目标。"""
    grant, pool = _get_granted_pool(session, scope, pool_id)
    pool.allowed_targets = _normalize_targets(payload.allowed_targets)
    deps.commit(session)
    return _pool_out(grant, pool, _bound_environment_ids(session, scope, pool_id))


# —— 普通变量 ——


def _variables_payload(variables: dict) -> list[VariableItem]:
    return [VariableItem(name=name, value=value) for name, value in variables.items()]


def _reject_variable(name: str, raw: object) -> None:
    """普通变量只接受合法 ValueLiteral；秘密必须走身份凭证配置。

    秘密单独一个错误码：调用方据此提示“改用身份凭证”，与“值写错了”是两种处理。
    """
    if not name or len(name) > 100:
        raise bad_request("invalid_variable_name", "变量名必须是 1 到 100 个字符")
    if not isinstance(raw, dict):
        raise bad_request("invalid_variable", f"变量 {name} 的值必须是字面量对象")
    if raw.get("type") == "secret":
        raise bad_request("secret_not_allowed", f"变量 {name} 不能保存秘密，请使用身份凭证配置。")
    try:
        ValueLiteral.from_dict(raw)
    except ValueLiteralError as error:
        raise bad_request("invalid_variable", f"变量 {name} 的值无效：{error}") from error


def _validate_variables(pairs: list[tuple[str, object]]) -> dict:
    """环境与项目共用同一份变量校验，避免两条入口对秘密和字面量各有一套判断。"""
    table: dict = {}
    for name, raw in pairs:
        if name in table:
            raise bad_request("duplicate_variable", f"变量名重复：{name}")
        _reject_variable(name, raw)
        table[name] = raw
    return table


def _validate_variable_items(items: list[VariableItem]) -> dict:
    return _validate_variables([(item.name, item.value) for item in items])


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/variables", response_model=VariablesOut
)
def get_variables(
    scope: deps.ProjectScope = _VIEW_SCOPE, session: Session = Depends(get_db)
) -> VariablesOut:
    latest = session.scalar(
        select(ProjectConfigVersion)
        .where(ProjectConfigVersion.project_id == scope.project_id)
        .order_by(ProjectConfigVersion.version.desc())
        .limit(1)
    )
    if latest is None:
        return VariablesOut(version=0, variables=[])
    return VariablesOut(version=latest.version, variables=_variables_payload(latest.variables))


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/variable-context",
    response_model=VariableContextOut,
)
def get_variable_context(
    environment_id: uuid.UUID,
    service_key: str | None = None,
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> VariableContextOut:
    scope = begin_consistent_read(session, scope)
    environment = session.scalar(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.project_id == scope.project_id,
            Environment.status == "active",
        )
    )
    if environment is None:
        raise not_found("环境不存在或已归档")
    request: dict = {}
    if service_key is not None:
        if re.fullmatch(r"svc_[0-9a-f]{32}", service_key) is None:
            raise bad_request("service_invalid", "service_key不是合法命名服务标识")
        request = {"service_contract": 1, "service_key": service_key}
    capable = require_service_capability(service_contract, needed=service_key is not None)
    try:
        value = variable_context(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            environment=environment,
        )
        summary, _selected, target_error = inspect_selected_target(
            session, environment, request
        )
        if target_error is not None and target_error.global_basis:
            raise ApiError(
                target_error.status_code, target_error.code, target_error.message
            )
        if capable:
            value["schema_version"] = 2
            value["selected_target"] = summary
    except ResolutionError as error:
        raise conflict(error.code, error.message) from error
    except ServiceTargetError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    return VariableContextOut(**value)


@router.put(
    "/workspaces/{workspace_id}/projects/{project_id}/variables", response_model=VariablesOut
)
def put_variables(
    payload: VariablesUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> VariablesOut:
    expected_version = _config_precondition(if_match)
    table = _validate_variable_items(payload.variables)
    scope = _asset_gate(session, scope)
    latest = session.scalar(
        select(ProjectConfigVersion)
        .where(ProjectConfigVersion.project_id == scope.project_id)
        .order_by(ProjectConfigVersion.version.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current_version = latest.version if latest is not None else 0
    if current_version != expected_version:
        raise conflict("config_revision_conflict", "项目变量已被其他操作更新，请刷新后重试。")
    version = current_version + 1
    session.add(
        ProjectConfigVersion(
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            version=version,
            variables=table,
        )
    )
    deps.commit(session)
    return VariablesOut(version=version, variables=_variables_payload(table))


# —— 目录 ——


def _folder_out(item: Folder, availability: str | None = None) -> FolderOut:
    return FolderOut(
        id=item.id,
        parent_id=item.parent_id,
        name=item.name,
        archived_at=item.archived_at,
        rev=item.rev,
        availability=availability or ("archived" if item.archived_at is not None else "available"),
    )


def _normalize(value: str) -> str:
    return value.strip().casefold()


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/folders", response_model=list[FolderOut]
)
def list_folders(
    scope: deps.ProjectScope = _VIEW_SCOPE, session: Session = Depends(get_db)
) -> list[FolderOut]:
    scope = begin_consistent_read(session, scope)
    items = list(session.scalars(
        select(Folder)
        .where(Folder.project_id == scope.project_id, Folder.archived_at.is_(None))
        .order_by(Folder.name)
    ))
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    blocked_ids = set(session.scalars(select(blocked.c.id)))
    invalid_ids = set(session.scalars(select(invalid.c.id)))
    return [
        _folder_out(
            item,
            "invalid_parent_chain"
            if item.id in invalid_ids
            else ("ancestor_archived" if item.id in blocked_ids else "available"),
        )
        for item in items
    ]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/folders",
    response_model=FolderOut,
    status_code=201,
)
def create_folder(
    payload: FolderCreate,
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> FolderOut:
    scope = _asset_gate(session, scope)
    if payload.parent_id is not None:
        _available_folder(session, scope, payload.parent_id)
    normalized = _normalize(payload.name)
    duplicate = session.scalar(
        select(Folder).where(
            Folder.project_id == scope.project_id,
            Folder.parent_id == payload.parent_id,
            Folder.normalized_name == normalized,
            Folder.archived_at.is_(None),
        )
    )
    if duplicate is not None:
        raise conflict("folder_name_exists", "同级目录下已存在同名目录")
    folder = Folder(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        parent_id=payload.parent_id,
        name=payload.name,
        normalized_name=normalized,
    )
    session.add(folder)
    deps.commit(session)
    return _folder_out(folder)


@router.patch(
    "/workspaces/{workspace_id}/projects/{project_id}/folders/{folder_id}",
    response_model=FolderOut,
)
def update_folder(
    folder_id: uuid.UUID,
    payload: FolderUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> FolderOut:
    scope = _asset_gate(session, scope)
    folder = session.scalar(
        select(Folder)
        .where(
            Folder.workspace_id == scope.workspace_id,
            Folder.id == folder_id,
            Folder.project_id == scope.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if folder is None:
        raise not_found("目录不存在")
    _folder_precondition(if_match, folder.rev)
    if folder.archived_at is not None:
        raise conflict("asset_unavailable", "归档目录必须恢复后才能修改。")
    if payload.name is not None:
        folder.name = payload.name
        folder.normalized_name = _normalize(payload.name)
    if "parent_id" in payload.model_fields_set:
        if payload.parent_id == folder_id:
            raise bad_request("folder_cycle", "目录不能作为自己的父目录")
        if payload.parent_id is not None:
            _available_folder(session, scope, payload.parent_id)
            descendants = folder_descendants(scope, folder_id)
            if session.scalar(select(descendants.c.id).where(descendants.c.id == payload.parent_id)):
                raise bad_request("folder_cycle", "目录不能移动到自己的后代目录中。")
        folder.parent_id = payload.parent_id
    folder.rev += 1
    deps.commit(session)
    return _folder_out(folder)


@router.delete(
    "/workspaces/{workspace_id}/projects/{project_id}/folders/{folder_id}", status_code=204
)
def archive_folder(
    folder_id: uuid.UUID,
    scope: deps.ProjectScope = _EDIT_SCOPE,
    session: Session = Depends(get_db),
) -> Response:
    raise conflict(
        "asset_selection_required",
        "目录归档必须先预览完整范围并通过资产操作确认。",
    )


def _get_folder(session: Session, scope: deps.ProjectScope, folder_id: uuid.UUID) -> Folder:
    folder = session.scalar(
        select(Folder).where(
            Folder.workspace_id == scope.workspace_id,
            Folder.id == folder_id,
            Folder.project_id == scope.project_id,
        )
    )
    if folder is None:
        raise not_found("目录不存在")
    return folder
