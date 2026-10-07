"""S2 服务目录、环境映射配置 owner 与目标解析。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from ..kernel.environment_url import EnvironmentUrlError, check_base_url, normalize_base_url
from ..models import (
    Environment,
    EnvironmentConfigVersion,
    EnvironmentServiceMapping,
    EnvironmentServiceVersion,
    ProjectService,
)

DEFAULT_SERVICE_KEY = "default"


class ServiceTargetError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        global_basis: bool = False,
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code
        self.global_basis = global_basis


@dataclass(frozen=True)
class SelectedTarget:
    service: ProjectService
    mapping: EnvironmentServiceMapping
    version: EnvironmentServiceVersion

    @property
    def base_url(self) -> str:
        return self.version.base_url

    def target_ref(self, environment_id: uuid.UUID) -> dict:
        return {
            "kind": "default" if self.service.is_default else "service",
            "service_id": str(self.service.id),
            "service_key": self.service.service_key,
            "service_rev": self.service.rev,
            "mapping_id": str(self.mapping.id),
            "mapping_rev": self.mapping.rev,
            "mapping_version_id": str(self.version.id),
            "mapping_version": self.version.version,
            "environment_id": str(environment_id),
        }


def inspect_selected_target(
    session: Session, environment: Environment, request: dict
) -> tuple[dict, SelectedTarget | None, ServiceTargetError | None]:
    key = request.get("service_key") or DEFAULT_SERVICE_KEY
    service_row = aliased(ProjectService)
    mapping_row = aliased(EnvironmentServiceMapping)
    version_row = aliased(EnvironmentServiceVersion)
    default_service_row = aliased(ProjectService)
    default_mapping_row = aliased(EnvironmentServiceMapping)
    default_version_row = aliased(EnvironmentServiceVersion)
    config_row = aliased(EnvironmentConfigVersion)
    facts = session.execute(
        select(
            Environment,
            service_row,
            mapping_row,
            version_row,
            default_service_row,
            default_mapping_row,
            default_version_row,
            config_row,
        )
        .select_from(Environment)
        .outerjoin(
            service_row,
            and_(
                service_row.project_id == Environment.project_id,
                service_row.service_key == key,
            ),
        )
        .outerjoin(
            mapping_row,
            and_(
                mapping_row.environment_id == Environment.id,
                mapping_row.service_id == service_row.id,
            ),
        )
        .outerjoin(
            version_row,
            and_(
                version_row.id == mapping_row.current_version_id,
                version_row.mapping_id == mapping_row.id,
                version_row.environment_id == Environment.id,
                version_row.service_id == service_row.id,
            ),
        )
        .outerjoin(
            default_service_row,
            and_(
                default_service_row.project_id == Environment.project_id,
                default_service_row.is_default.is_(True),
            ),
        )
        .outerjoin(
            default_mapping_row,
            and_(
                default_mapping_row.environment_id == Environment.id,
                default_mapping_row.service_id == default_service_row.id,
            ),
        )
        .outerjoin(
            default_version_row,
            and_(
                default_version_row.id == default_mapping_row.current_version_id,
                default_version_row.mapping_id == default_mapping_row.id,
                default_version_row.environment_id == Environment.id,
                default_version_row.service_id == default_service_row.id,
            ),
        )
        .outerjoin(
            config_row,
            and_(
                config_row.id == Environment.current_config_version_id,
                config_row.environment_id == Environment.id,
            ),
        )
        .where(
            Environment.id == environment.id,
            Environment.project_id == environment.project_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if facts is None:
        raise ServiceTargetError("service_invalid", "环境或服务范围不存在")
    (
        current_environment,
        service,
        mapping,
        version,
        default_service,
        default_mapping,
        default_version,
        config,
    ) = facts
    if service is None or (key == DEFAULT_SERVICE_KEY) != service.is_default:
        raise ServiceTargetError("service_invalid", "服务不存在或不属于本项目")
    common = {
        "kind": "default" if service.is_default else "service",
        "service_id": str(service.id), "service_key": service.service_key,
        "service_name": service.name, "service_rev": service.rev,
        "service_status": service.status,
    }
    if (
        config is None
        or config.schema_version != 2
        or not isinstance(config.snapshot, dict)
    ):
        error = ServiceTargetError(
            "config_inconsistent",
            "环境当前配置版本缺失或结构不受支持",
            status_code=409,
            global_basis=True,
        )
        return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
    refs = config.snapshot.get("service_versions")
    if not isinstance(refs, dict):
        error = ServiceTargetError(
            "config_inconsistent",
            "环境当前配置版本缺少服务映射依据",
            status_code=409,
            global_basis=True,
        )
        return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
    default_error = _default_basis_error(
        current_environment,
        refs,
        default_service=default_service,
        default_mapping=default_mapping,
        default_version=default_version,
        selected_is_default=service.is_default,
    )
    if default_error is not None:
        return (
            {**common, "availability": "config_inconsistent", "mapping": None},
            None,
            default_error,
        )
    if mapping is None:
        error = ServiceTargetError("mapping_missing", "所选环境尚未配置该服务地址", status_code=409)
        return {**common, "availability": "mapping_missing", "mapping": None}, None, error
    if version is None:
        error = ServiceTargetError(
            "config_inconsistent",
            "服务映射当前版本不一致",
            status_code=409,
            global_basis=service.is_default,
        )
        return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
    summary = {
        "id": str(mapping.id), "rev": mapping.rev, "status": mapping.status,
        "base_url": version.base_url, "version_id": str(version.id), "version": version.version,
    }
    selected = SelectedTarget(service, mapping, version)
    if service.is_default and (
        version.base_url != current_environment.base_url
        or mapping.status != ("archived" if current_environment.status == "archived" else "active")
    ):
        # 默认服务仍承载旧 Environment.base_url 契约。历史坏地址可能由旧版本直接
        # 写入环境行，而 S2 镜像仍保留迁移时的旧值；此时先告诉用户地址本身为何
        # 不可用，不能用较泛的镜像不一致遮住既有、可直接修正的错误。
        try:
            check_base_url(current_environment.base_url)
        except EnvironmentUrlError as exc:
            error = ServiceTargetError(
                "environment_url_invalid",
                str(exc),
                status_code=400,
                global_basis=True,
            )
            return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
        error = ServiceTargetError(
            "config_inconsistent",
            "默认服务镜像与环境配置不一致",
            status_code=409,
            global_basis=True,
        )
        return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
    ref = refs.get(str(service.id)) or (refs.get("default") if service.is_default else None)
    if not isinstance(ref, dict) or ref != {
        "mapping_id": str(mapping.id),
        "mapping_version_id": str(version.id),
    }:
        error = ServiceTargetError(
            "config_inconsistent",
            "环境配置版本与服务映射引用不一致",
            status_code=409,
            global_basis=service.is_default,
        )
        return {**common, "availability": "config_inconsistent", "mapping": None}, None, error
    if current_environment.status != "active":
        return {**common, "availability": "environment_archived", "mapping": summary}, selected, ServiceTargetError("service_unavailable", "环境已归档", status_code=409)
    if service.status != "active":
        return {**common, "availability": "service_archived", "mapping": summary}, selected, ServiceTargetError("service_unavailable", "服务已归档", status_code=409)
    if mapping.status != "active":
        return {**common, "availability": "mapping_disabled", "mapping": summary}, selected, ServiceTargetError("service_unavailable", "服务映射已停用", status_code=409)
    return {**common, "availability": "ready", "mapping": summary}, selected, None


def _default_basis_error(
    environment: Environment,
    refs: dict,
    *,
    default_service: ProjectService | None,
    default_mapping: EnvironmentServiceMapping | None,
    default_version: EnvironmentServiceVersion | None,
    selected_is_default: bool,
) -> ServiceTargetError | None:
    """核全部服务共享的default投影关系，不检查未选default的地址词法。"""
    if default_service is None or default_service.status != "active":
        return ServiceTargetError(
            "config_inconsistent", "默认服务目录缺失或状态异常", status_code=409,
            global_basis=True,
        )
    if default_mapping is None:
        return ServiceTargetError(
            "config_inconsistent", "默认服务映射缺失", status_code=409,
            global_basis=True,
        )
    ref = refs.get(str(default_service.id)) or refs.get("default")
    if default_version is None or not isinstance(ref, dict) or ref != {
        "mapping_id": str(default_mapping.id),
        "mapping_version_id": str(default_mapping.current_version_id),
    }:
        return ServiceTargetError(
            "config_inconsistent", "默认服务当前版本或配置引用不一致", status_code=409,
            global_basis=True,
        )
    expected_status = "archived" if environment.status == "archived" else "active"
    if (
        default_version.base_url != environment.base_url
        or default_mapping.status != expected_status
    ):
        # 只有真正选择default时才保留旧地址语法的精确优先级；named只需要知道
        # 全局镜像已损坏，不能被未选择的default词法阻断。
        if selected_is_default:
            try:
                check_base_url(environment.base_url)
            except EnvironmentUrlError as exc:
                return ServiceTargetError(
                    "environment_url_invalid", str(exc), status_code=400,
                    global_basis=True,
                )
        return ServiceTargetError(
            "config_inconsistent", "默认服务镜像与环境配置不一致", status_code=409,
            global_basis=True,
        )
    return None


def normalize_service_name(name: str) -> tuple[str, str]:
    display = name.strip(" ")
    if not display:
        raise ServiceTargetError("service_invalid", "服务名称不能为空")
    if len(display) > 200:
        raise ServiceTargetError("service_invalid", "服务名称最多200个字符")
    return display, display.casefold()


def new_service_key() -> str:
    return "svc_" + uuid.uuid4().hex


def ensure_default_service(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectService:
    service = session.scalar(
        select(ProjectService).where(
            ProjectService.project_id == project_id, ProjectService.is_default.is_(True)
        )
    )
    if service is None:
        service = ProjectService(
            workspace_id=workspace_id,
            project_id=project_id,
            service_key=DEFAULT_SERVICE_KEY,
            name="默认服务",
            normalized_name="默认服务",
            is_default=True,
            status="active",
        )
        session.add(service)
        session.flush()
    return service


def create_default_mapping(
    session: Session,
    *,
    environment: Environment,
    service: ProjectService,
    created_by: uuid.UUID | None,
) -> EnvironmentServiceMapping:
    mapping_id, version_id = uuid.uuid4(), uuid.uuid4()
    status = "archived" if environment.status == "archived" else "active"
    mapping = EnvironmentServiceMapping(
        id=mapping_id,
        workspace_id=environment.workspace_id,
        project_id=environment.project_id,
        environment_id=environment.id,
        service_id=service.id,
        rev=1,
        status=status,
        current_version_id=version_id,
    )
    version = EnvironmentServiceVersion(
        id=version_id,
        workspace_id=environment.workspace_id,
        project_id=environment.project_id,
        environment_id=environment.id,
        service_id=service.id,
        mapping_id=mapping_id,
        version=1,
        base_url=environment.base_url,
        status=status,
        created_by=created_by,
    )
    session.add_all([mapping, version])
    session.flush()
    return mapping


def _current_version(
    session: Session, mapping: EnvironmentServiceMapping
) -> EnvironmentServiceVersion | None:
    return session.scalar(
        select(EnvironmentServiceVersion).where(
            EnvironmentServiceVersion.id == mapping.current_version_id,
            EnvironmentServiceVersion.mapping_id == mapping.id,
            EnvironmentServiceVersion.environment_id == mapping.environment_id,
            EnvironmentServiceVersion.service_id == mapping.service_id,
        )
    )


def resolve_selected_target(
    session: Session, environment: Environment, request: dict, *, require_available: bool = True
) -> SelectedTarget:
    _summary, selected, error = inspect_selected_target(session, environment, request)
    if selected is None:
        assert error is not None
        raise error
    if require_available and error is not None:
        raise error
    if require_available:
        try:
            check_base_url(selected.version.base_url)
        except EnvironmentUrlError as error:
            raise ServiceTargetError("environment_url_invalid", str(error)) from error
    return selected


def append_mapping_version(
    session: Session,
    mapping: EnvironmentServiceMapping,
    *,
    base_url: str,
    status: str,
    created_by: uuid.UUID,
) -> EnvironmentServiceVersion:
    current = _current_version(session, mapping)
    if current is None:
        raise ServiceTargetError(
            "config_inconsistent", "服务映射当前版本不一致", status_code=409
        )
    # 完全相同的历史原文先按词法比较；这样批量保存其它服务时不会强迫旧坏地址
    # 重新通过今天的校验，也不会把空格等旧差异规范化后误判成“没有修复”。
    if base_url == current.base_url and status == mapping.status:
        return current
    try:
        base_url = normalize_base_url(base_url)
    except EnvironmentUrlError as error:
        raise ServiceTargetError("environment_url_invalid", str(error)) from error
    if len(base_url) > 500:
        raise ServiceTargetError("environment_url_invalid", "服务地址最多500个字符")
    if base_url == current.base_url and status == mapping.status:
        return current
    latest = session.scalar(
        select(func.max(EnvironmentServiceVersion.version)).where(
            EnvironmentServiceVersion.mapping_id == mapping.id
        )
    )
    version = EnvironmentServiceVersion(
        workspace_id=mapping.workspace_id,
        project_id=mapping.project_id,
        environment_id=mapping.environment_id,
        service_id=mapping.service_id,
        mapping_id=mapping.id,
        version=(latest or 0) + 1,
        base_url=base_url,
        status=status,
        created_by=created_by,
    )
    session.add(version)
    session.flush()
    mapping.current_version_id = version.id
    mapping.status = status
    mapping.rev += 1
    return version


def append_environment_schema2(
    session: Session, environment: Environment, *, created_by: uuid.UUID
) -> EnvironmentConfigVersion:
    mappings = list(
        session.scalars(
            select(EnvironmentServiceMapping).where(
                EnvironmentServiceMapping.environment_id == environment.id
            )
        )
    )
    refs = {
        str(mapping.service_id): {
            "mapping_id": str(mapping.id),
            "mapping_version_id": str(mapping.current_version_id),
        }
        for mapping in mappings
    }
    latest = session.scalar(
        select(func.max(EnvironmentConfigVersion.version)).where(
            EnvironmentConfigVersion.environment_id == environment.id
        )
    )
    item = EnvironmentConfigVersion(
        workspace_id=environment.workspace_id,
        project_id=environment.project_id,
        environment_id=environment.id,
        version=(latest or 0) + 1,
        schema_version=2,
        snapshot={"schema_version": 2, "variables": dict(environment.variables or {}), "service_versions": refs},
        created_by=created_by,
    )
    session.add(item)
    session.flush()
    environment.current_config_version_id = item.id
    return item


__all__ = [
    "DEFAULT_SERVICE_KEY",
    "SelectedTarget",
    "ServiceTargetError",
    "append_environment_schema2",
    "append_mapping_version",
    "create_default_mapping",
    "ensure_default_service",
    "inspect_selected_target",
    "new_service_key",
    "normalize_service_name",
    "resolve_selected_target",
]
