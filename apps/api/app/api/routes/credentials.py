"""环境级身份凭证管理接口。

本模块是秘密的唯一写入入口，也是凭证配置、集合与用途授权的管理入口。三条边界：

1. **秘密只进不出**：写入接收明文并立即加密，任何读接口都不返回明文或密文，
   只返回名称、版本号与可用状态。
2. **管理动作**：全部挂在 `manage_secrets` 动作上（仅项目管理员），普通编辑者
   不能创建身份、绑定秘密或扩大用途授权。
3. **授权只能收窄**：用途授权可限制目标、槽位、主体和有效期，但不能超出身份
   配置声明过的范围——收窄由服务层校验，不靠界面的自律。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_db
from ...models import (
    CredentialProfile,
    CredentialProfileVersion,
    CredentialSet,
    CredentialSetSecretVersion,
    CredentialUseGrant,
    Environment,
    Secret,
    SecretVersion,
)
from ...services import credentials as credential_service
from ...services.credentials import CredentialError
from .. import deps
from ..errors import bad_request, conflict, not_found
from ..schemas import (
    CredentialGrantCreate,
    CredentialGrantOut,
    CredentialProfileCreate,
    CredentialProfileOut,
    CredentialProfileVersionCreate,
    CredentialProfileVersionOut,
    CredentialSetActivate,
    CredentialSetOut,
    CredentialSetSlotOut,
    SecretCreate,
    SecretOut,
    SecretRotate,
    SecretVersionOut,
)

router = APIRouter(tags=["身份凭证"])

_ADMIN_SCOPE = Depends(deps.admin_scope)


def _credential_error(error: CredentialError) -> Exception:
    """凭证服务错误按边界翻译为中文 API 错误，错误码保持稳定。"""
    if error.code in {"secret_not_in_project", "case_version_not_found"}:
        return not_found(error.message)
    if error.code in {"grant_scope_wider_than_profile", "grant_environment_mismatch"}:
        return conflict(error.code, error.message)
    return bad_request(error.code, error.message)


# —— 秘密 ——


def _get_secret(session: Session, scope: deps.ProjectScope, secret_id: uuid.UUID) -> Secret:
    secret = session.scalar(
        select(Secret).where(Secret.id == secret_id, Secret.project_id == scope.project_id)
    )
    if secret is None:
        raise not_found("秘密不存在")
    return secret


def _latest_version(session: Session, secret_id: uuid.UUID) -> SecretVersion:
    version = session.scalar(
        select(SecretVersion)
        .where(SecretVersion.secret_id == secret_id)
        .order_by(SecretVersion.version.desc())
        .limit(1)
    )
    if version is None:
        raise not_found("秘密没有任何版本")
    return version


def _secret_out(session: Session, secret: Secret) -> SecretOut:
    latest = _latest_version(session, secret.id)
    return SecretOut(
        id=secret.id,
        name=secret.name,
        kind=secret.kind,
        latest_version=latest.version,
        latest_version_id=latest.id,
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/secrets",
    response_model=list[SecretOut],
)
def list_secrets(
    scope: deps.ProjectScope = _ADMIN_SCOPE, session: Session = Depends(get_db)
) -> list[SecretOut]:
    items = session.scalars(
        select(Secret).where(Secret.project_id == scope.project_id).order_by(Secret.name)
    )
    return [_secret_out(session, item) for item in items]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/secrets",
    response_model=SecretOut,
    status_code=201,
)
def create_secret(
    payload: SecretCreate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> SecretOut:
    duplicate = session.scalar(
        select(Secret).where(Secret.project_id == scope.project_id, Secret.name == payload.name)
    )
    if duplicate is not None:
        raise conflict("secret_name_exists", "同名秘密已存在，请改用轮换接口更新值。")
    try:
        secret = credential_service.store_secret(
            session,
            settings,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            name=payload.name,
            value=payload.value,
        )
    except CredentialError as error:
        raise _credential_error(error) from error
    deps.commit(session)
    return _secret_out(session, secret)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/secrets/{secret_id}/versions",
    response_model=SecretVersionOut,
    status_code=201,
)
def rotate_secret(
    secret_id: uuid.UUID,
    payload: SecretRotate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> SecretVersionOut:
    secret = _get_secret(session, scope, secret_id)
    try:
        version = credential_service.rotate_secret(session, settings, secret, payload.value)
    except CredentialError as error:
        raise _credential_error(error) from error
    deps.commit(session)
    return SecretVersionOut(secret_id=secret.id, version_id=version.id, version=version.version)


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/secrets/{secret_id}/versions",
    response_model=list[SecretVersionOut],
)
def list_secret_versions(
    secret_id: uuid.UUID,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> list[SecretVersionOut]:
    """秘密的版本列表：只有版本号与版本 id，没有明文也没有密文。

    绑定凭证集合要选“哪一个版本”，而轮换之后旧版本仍被已发出的运行引用；只给最新
    版本号会让管理员在回退到上一版时无从下手，只能先把值重新轮换回去。
    """
    secret = _get_secret(session, scope, secret_id)
    versions = session.scalars(
        select(SecretVersion)
        .where(SecretVersion.secret_id == secret.id)
        .order_by(SecretVersion.version.desc())
    )
    return [
        SecretVersionOut(secret_id=secret.id, version_id=item.id, version=item.version)
        for item in versions
    ]


# —— 身份配置 ——


def _profile_out(session: Session, profile: CredentialProfile) -> CredentialProfileOut:
    latest = session.scalar(
        select(CredentialProfileVersion)
        .where(CredentialProfileVersion.profile_id == profile.id)
        .order_by(CredentialProfileVersion.version.desc())
        .limit(1)
    )
    slot_count = 0
    if profile.current_set_id is not None:
        slot_count = (
            session.scalar(
                select(func.count())
                .select_from(CredentialSetSecretVersion)
                .where(CredentialSetSecretVersion.credential_set_id == profile.current_set_id)
            )
            or 0
        )
    return CredentialProfileOut(
        id=profile.id,
        environment_id=profile.environment_id,
        name=profile.name,
        provider=profile.provider,
        status=profile.status,
        current_epoch=profile.current_epoch,
        current_set_id=profile.current_set_id,
        profile_version_id=latest.id if latest is not None else None,
        allowed_targets=list(profile.allowed_targets or []),
        allowed_auth_slots=list(profile.allowed_auth_slots or []),
        slot_count=slot_count,
    )


def _get_profile(session: Session, scope: deps.ProjectScope, profile_id: uuid.UUID) -> CredentialProfile:
    profile = session.scalar(
        select(CredentialProfile).where(
            CredentialProfile.id == profile_id, CredentialProfile.project_id == scope.project_id
        )
    )
    if profile is None:
        raise not_found("身份配置不存在")
    return profile


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles",
    response_model=list[CredentialProfileOut],
)
def list_profiles(
    scope: deps.ProjectScope = _ADMIN_SCOPE, session: Session = Depends(get_db)
) -> list[CredentialProfileOut]:
    items = session.scalars(
        select(CredentialProfile)
        .where(CredentialProfile.project_id == scope.project_id)
        .order_by(CredentialProfile.name)
    )
    return [_profile_out(session, item) for item in items]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles",
    response_model=CredentialProfileOut,
    status_code=201,
)
def create_profile(
    payload: CredentialProfileCreate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialProfileOut:
    environment = session.scalar(
        select(Environment).where(
            Environment.id == payload.environment_id, Environment.project_id == scope.project_id
        )
    )
    if environment is None:
        raise bad_request("environment_not_found", "环境不存在或不属于本项目")
    duplicate = session.scalar(
        select(CredentialProfile).where(
            CredentialProfile.environment_id == payload.environment_id,
            CredentialProfile.name == payload.name,
        )
    )
    if duplicate is not None:
        raise conflict("profile_name_exists", "该环境下已有同名身份配置")
    try:
        profile = credential_service.save_profile(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            environment_id=payload.environment_id,
            name=payload.name,
            allowed_targets=payload.allowed_targets,
            allowed_auth_slots=payload.allowed_auth_slots,
        )
        # 认证位置／失效判据的首个版本与身份同事务写入：分成两次请求会出现“身份已建、
        # 配置缺失”的中间状态，而缺配置的身份既不能绑定也不能执行。
        if payload.config is not None:
            credential_service.save_profile_version(session, profile, payload.config)
    except CredentialError as error:
        raise _credential_error(error) from error
    deps.commit(session)
    return _profile_out(session, profile)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles/{profile_id}/versions",
    response_model=CredentialProfileVersionOut,
    status_code=201,
)
def create_profile_version(
    profile_id: uuid.UUID,
    payload: CredentialProfileVersionCreate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialProfileVersionOut:
    profile = _get_profile(session, scope, profile_id)
    version = credential_service.save_profile_version(session, profile, payload.config)
    deps.commit(session)
    return CredentialProfileVersionOut(
        id=version.id, profile_id=profile.id, version=version.version, config=version.config
    )


@router.put(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles/{profile_id}/set",
    response_model=CredentialProfileOut,
)
def activate_profile_set(
    profile_id: uuid.UUID,
    payload: CredentialSetActivate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialProfileOut:
    profile = _get_profile(session, scope, profile_id)
    if not payload.slots:
        raise bad_request("credential_set_empty", "凭证集合至少要绑定一个认证槽位。")
    # 整份替换必须基于表单读取时的那一版集合：否则「打开表单 → 别人换过一次 →
    # 我提交」会把别人刚换上的槽位按我的旧表单整份覆盖掉，而两边都以为保存成功了。
    if payload.expected_epoch is not None and payload.expected_epoch != profile.current_epoch:
        raise conflict(
            "credential_set_epoch_conflict",
            f"当前凭证集合已是第 {profile.current_epoch} 次切换，你的表单基于第 "
            f"{payload.expected_epoch} 次；请刷新后按最新的槽位集合重新提交。",
        )
    try:
        credential_service.activate_set(
            session, profile=profile, slots=payload.slots, expires_at=payload.expires_at
        )
    except CredentialError as error:
        raise _credential_error(error) from error
    deps.commit(session)
    return _profile_out(session, profile)


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles/{profile_id}/set",
    response_model=CredentialSetOut,
)
def get_profile_set(
    profile_id: uuid.UUID,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialSetOut:
    """当前凭证集合的绑定元数据：槽位 → 秘密名称与版本号。

    界面按整份集合提交，因此必须先能读到整份集合。这里只回名称与版本号：秘密的
    明文与密文都不出现在响应里，读接口不需要、也不应该拿到值。
    """
    profile = _get_profile(session, scope, profile_id)
    if profile.current_set_id is None:
        return CredentialSetOut(
            profile_id=profile.id,
            set_id=None,
            epoch=profile.current_epoch,
            status=None,
            expires_at=None,
            slots=[],
        )
    current = session.get(CredentialSet, profile.current_set_id)
    if current is None:
        raise not_found("当前凭证集合不存在")
    rows = session.execute(
        select(CredentialSetSecretVersion, Secret, SecretVersion)
        .join(SecretVersion, SecretVersion.id == CredentialSetSecretVersion.secret_version_id)
        .join(Secret, Secret.id == SecretVersion.secret_id)
        .where(CredentialSetSecretVersion.credential_set_id == current.id)
        .order_by(CredentialSetSecretVersion.auth_slot)
    ).all()
    return CredentialSetOut(
        profile_id=profile.id,
        set_id=current.id,
        epoch=profile.current_epoch,
        status=current.status,
        expires_at=current.expires_at,
        slots=[
            CredentialSetSlotOut(
                auth_slot=binding.auth_slot,
                secret_id=secret.id,
                secret_name=secret.name,
                secret_version_id=secret_version.id,
                secret_version=secret_version.version,
            )
            for binding, secret, secret_version in rows
        ],
    )


# —— 用途授权 ——


def _grant_out(grant: CredentialUseGrant) -> CredentialGrantOut:
    return CredentialGrantOut(
        id=grant.id,
        profile_id=grant.profile_id,
        environment_id=grant.environment_id,
        grant_type=grant.grant_type,
        principal_id=grant.principal_id,
        case_version_id=grant.case_version_id,
        debug_snapshot_hash=grant.debug_snapshot_hash,
        allowed_targets=list(grant.allowed_targets or []),
        allowed_auth_slots=list(grant.allowed_auth_slots or []),
        status=grant.status,
        expires_at=grant.expires_at,
        used_at=grant.used_at,
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/grants",
    response_model=list[CredentialGrantOut],
)
def list_grants(
    scope: deps.ProjectScope = _ADMIN_SCOPE, session: Session = Depends(get_db)
) -> list[CredentialGrantOut]:
    items = session.scalars(
        select(CredentialUseGrant)
        .where(CredentialUseGrant.project_id == scope.project_id)
        .order_by(CredentialUseGrant.created_at.desc())
    )
    return [_grant_out(item) for item in items]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/grants",
    response_model=CredentialGrantOut,
    status_code=201,
)
def create_grant(
    payload: CredentialGrantCreate,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialGrantOut:
    profile = _get_profile(session, scope, payload.profile_id)
    try:
        grant = credential_service.create_use_grant(
            session,
            profile=profile,
            environment_id=profile.environment_id,
            principal_id=payload.principal_id,
            grant_type=payload.grant_type,
            case_version_id=payload.case_version_id,
            debug_snapshot_hash=payload.debug_snapshot_hash,
            allowed_targets=payload.allowed_targets,
            allowed_auth_slots=payload.allowed_auth_slots,
            allowed_inputs=payload.allowed_inputs,
            expires_at=payload.expires_at,
        )
    except CredentialError as error:
        raise _credential_error(error) from error
    deps.commit(session)
    return _grant_out(grant)


@router.delete(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/grants/{grant_id}",
    status_code=204,
)
def revoke_grant(
    grant_id: uuid.UUID,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> Response:
    grant = session.scalar(
        select(CredentialUseGrant).where(
            CredentialUseGrant.id == grant_id, CredentialUseGrant.project_id == scope.project_id
        )
    )
    if grant is None:
        raise not_found("用途授权不存在")
    credential_service.revoke_use_grant(session, grant)
    deps.commit(session)
    return Response(status_code=204)


# —— 状态 ——


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/credentials/profiles/{profile_id}",
    response_model=CredentialProfileOut,
)
def get_profile(
    profile_id: uuid.UUID,
    scope: deps.ProjectScope = _ADMIN_SCOPE,
    session: Session = Depends(get_db),
) -> CredentialProfileOut:
    """身份配置状态：只回状态与槽位数，不返回值。"""
    profile = _get_profile(session, scope, profile_id)
    if profile.current_set_id is not None:
        # 集合存在性检查走真实查询，避免把已删除集合的 id 当作可用凭证。
        current = session.get(CredentialSet, profile.current_set_id)
        if current is None:
            raise not_found("当前凭证集合不存在")
    return _profile_out(session, profile)
