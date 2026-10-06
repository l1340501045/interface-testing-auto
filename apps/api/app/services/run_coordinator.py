"""运行协调器：创建运行与工作项，固定快照、身份与截止时间。

创建运行的事务负责：确认资源版本与当前权限 → 解析执行池与项目授权 → 固定
配置与身份版本 → 同事务写入 run / job。API 返回 202 只表示已入队，不表示
已经发出 HTTP；配置拒绝、入队、领取、发送意图与结果分别可追踪。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api import deps
from ..config import Settings
from ..kernel.assertion_spec import AssertionSpecError, validate_assertions
from ..kernel.environment_url import EnvironmentUrlError, check_base_url
from ..kernel.request_spec import RequestSpecError, validate_request
from ..kernel.target_policy import TargetGuard, TargetPolicyError
from ..models import (
    Case,
    CaseVersion,
    CredentialSet,
    Environment,
    IdempotencyRecord,
    Job,
    Run,
    RunnerPool,
    RunnerPoolProjectGrant,
)
from . import asset_lifecycle
from .credentials import CredentialError, available_profiles, parse_auth_slot, set_auth_slots
from .debug_context import build_context_binding
from .folder_graph import blocked_folder_ids, invalid_folder_ids
from .permissions import require
from .resolution import (
    ResolutionError,
    build_resolution,
    verify_resolution_context,
)
from .variable_inputs import merged_variables

_IDEMPOTENCY_TTL_HOURS = 24


class RunRejected(Exception):
    """运行创建被策略或配置拒绝，附带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RunRequest:
    environment_id: uuid.UUID
    case_version_id: uuid.UUID | None = None
    debug_snapshot: dict | None = None
    idempotency_key: str | None = None
    source_case_id: uuid.UUID | None = None
    resolution_context: str | None = None


@dataclass
class ResolvedPool:
    pool: RunnerPool
    guard: TargetGuard


def _request_hash(payload: RunRequest) -> str:
    body = {
        "environment_id": str(payload.environment_id),
        "case_version_id": str(payload.case_version_id) if payload.case_version_id else None,
        "debug_snapshot": payload.debug_snapshot,
    }
    if payload.source_case_id is not None:
        body["source_case_id"] = str(payload.source_case_id)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def debug_snapshot_digest(request: dict, assertions: list[dict]) -> str:
    """调试快照摘要：对规范化后的请求与断言取稳定摘要。

    摘要必须由服务端按规范化后的内容计算，不能由调用方传入，否则“这份一次性
    授权绑定的是哪份快照”就退化成自证——谁都能挑一个与自己请求无关的摘要去
    换取授权。规范化在前，摘要在后，管理员看到的摘要与实际执行的内容同源。
    """
    body = {"request": request, "assertions": assertions}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def digest_for_debug_snapshot(payload: RunRequest) -> str:
    """按与创建运行完全相同的规范化步骤算出调试快照摘要。

    管理端需要在创建授权时拿到这个摘要，因此这条路径不能自己实现一套解析，
    必须复用 `_load_source` 里同样的校验，避免出现两个不同来源的摘要。
    """
    snapshot = payload.debug_snapshot or {}
    try:
        request = validate_request(snapshot.get("request", {}))
        assertions = list(snapshot.get("assertions", []))
        validate_assertions(assertions, request)
    except (RequestSpecError, AssertionSpecError) as error:
        raise RunRejected("case_invalid", f"调试请求定义无效：{error}") from error
    return debug_snapshot_digest(request, assertions)


def resolve_pool(session: Session, settings: Settings, environment: Environment) -> ResolvedPool:
    """环境固定执行池；执行池必须显式授权给本项目。"""
    pools = list(
        session.scalars(
            select(RunnerPool).where(
                RunnerPool.workspace_id == environment.workspace_id,
                RunnerPool.status == "active",
            ).order_by(RunnerPool.created_at)
        )
    )
    grants = set(
        session.scalars(
            select(RunnerPoolProjectGrant.pool_id).where(
                RunnerPoolProjectGrant.project_id == environment.project_id,
                RunnerPoolProjectGrant.status == "active",
            )
        )
    )
    if environment.pool_id is not None:
        pool = next((item for item in pools if item.id == environment.pool_id), None)
        if pool is None:
            raise RunRejected("pool_unavailable", "环境绑定的执行池不可用，请检查池状态。")
        if pool.id not in grants:
            raise RunRejected("pool_not_granted", "该执行池未授权给本项目，已阻止执行。")
    else:
        candidates = [item for item in pools if item.id in grants]
        if not candidates:
            raise RunRejected(
                "pool_not_granted",
                "本项目没有已授权的可用执行池，请联系执行池管理者授权后再执行。",
            )
        pool = candidates[0]

    allowed = list(pool.allowed_targets or [])
    if not allowed:
        allowed = [item.strip() for item in settings.controlled_targets.split(",") if item.strip()]
    try:
        guard = settings.target_guard(allowed)
    except TargetPolicyError as error:
        raise RunRejected("pool_config_invalid", str(error)) from error
    return ResolvedPool(pool=pool, guard=guard)


def _merged_variables(session: Session, environment: Environment) -> dict:
    """项目普通变量与环境普通变量合并；环境优先，秘密不参与。

    合并规则只有一份实现（`variable_inputs`），因为它同时决定凭证授权摘要；
    两处各写一遍会让授权绑定的是一个值、执行用的是另一个值。
    """
    return merged_variables(session, environment)


def _auth_slot_metadata(session: Session, environment: Environment) -> list[dict]:
    """只读投影当前唯一身份的槽位；不读秘密、不检查/消费用途授权。"""
    profiles = available_profiles(session, environment.id)
    if len(profiles) != 1 or profiles[0].current_set_id is None:
        return []
    credential_set = session.get(CredentialSet, profiles[0].current_set_id)
    if credential_set is None or credential_set.status != "active":
        return []
    result: list[dict] = []
    for slot in sorted(set_auth_slots(session, credential_set)):
        try:
            kind, name = parse_auth_slot(slot)
        except CredentialError:
            # 持久认证配置的具体错误仍由凭证责任层在 worker 给出；普通解析只跳过
            # 无法安全投影的槽位，不能泄露底层数据或伪造成用户行冲突。
            continue
        result.append(
            {"kind": kind, "name": name, "status": "pending_worker_verification"}
        )
    return result


def resolve_target(
    request: dict, environment: Environment, guard: TargetGuard
) -> tuple[dict, str]:
    """校验环境类型与目标来源，返回（已校验请求, 目标 origin）。

    这里只做不依赖网络的策略校验（环境地址语法、环境类型、来源白名单、元数据地址）。
    解析地址会随 DNS 可用性波动，创建运行不应因解析失败被拒绝；真正的解析与地址固定
    必须发生在发送前一刻，否则既挡不住 DNS 改绑，也分不清“网络失败”与“策略拒绝”。
    worker 在 executor 发送前重新解析并固定地址。
    """
    try:
        guard.check_environment(environment.kind)
    except TargetPolicyError as error:
        raise RunRejected("production_blocked", str(error)) from error

    # 地址本身缺协议／缺主机时，后面拼出来的目标必然解析不了；那种失败会被白名单检查
    # 报成 target_not_allowed，把用户引向去改本来正确的用例路径。地址问题在这里单独报出，
    # 并归到“环境配置”。按库里的**原文**校验：既不猜协议，也不用 trim 后的地址替换目标。
    try:
        check_base_url(environment.base_url)
    except EnvironmentUrlError as error:
        raise RunRejected("environment_url_invalid", str(error)) from error

    base = environment.base_url.rstrip("/")
    probe = f"{base}{request.get('path', '/')}"
    try:
        target = guard.authorize_url(probe)
    except TargetPolicyError as error:
        raise RunRejected("target_not_allowed", str(error)) from error
    return request, target.origin


def _load_source(
    session: Session, scope: deps.ProjectScope, payload: RunRequest
) -> tuple[str, uuid.UUID | None, uuid.UUID | None, dict, list[dict]]:
    """确定运行目标：已发布版本或临时调试快照，两者互斥。"""
    if (payload.case_version_id is None) == (payload.debug_snapshot is None):
        raise RunRejected(
            "target_required", "请选择已发布用例版本或提供调试请求快照，二者只能有一个。"
        )

    if payload.case_version_id is not None:
        version = session.scalar(
            select(CaseVersion).where(
                CaseVersion.workspace_id == scope.workspace_id,
                CaseVersion.id == payload.case_version_id,
                CaseVersion.project_id == scope.project_id,
            ).execution_options(populate_existing=True)
        )
        if version is None:
            raise RunRejected("case_version_missing", "用例版本不存在或不属于本项目。")
        _require_case_available(session, scope, version.case_id)
        try:
            request = validate_request(version.request)
        except RequestSpecError as error:
            raise RunRejected("case_invalid", f"用例版本请求定义无效：{error}") from error
        try:
            assertions = validate_assertions(_load_version_assertions(session, version.id), request)
        except AssertionSpecError as error:
            raise RunRejected("case_invalid", f"用例版本断言配置无效：{error}") from error
        return "case_version", version.id, None, request, assertions

    snapshot = payload.debug_snapshot or {}
    try:
        request = validate_request(snapshot.get("request", {}))
    except RequestSpecError as error:
        raise RunRejected("case_invalid", f"调试请求定义无效：{error}") from error
    try:
        assertions = list(snapshot.get("assertions", []))
        validate_assertions(assertions, request)
    except AssertionSpecError as error:
        raise RunRejected("case_invalid", f"调试断言配置无效：{error}") from error
    if payload.source_case_id is not None:
        _require_case_available(session, scope, payload.source_case_id)
    return "debug_snapshot", None, payload.source_case_id, request, assertions


def _require_case_available(
    session: Session, scope: deps.ProjectScope, case_id: uuid.UUID
) -> Case:
    case = session.scalar(
        select(Case).where(
            Case.workspace_id == scope.workspace_id,
            Case.id == case_id,
            Case.project_id == scope.project_id,
        ).execution_options(populate_existing=True)
    )
    if case is None:
        raise RunRejected("case_missing", "来源用例不存在或不属于本项目。")
    if case.status == "archived":
        raise RunRejected("case_archived", "来源用例已归档，请恢复后再执行。")
    blocked = blocked_folder_ids(scope)
    invalid = invalid_folder_ids(scope)
    if case.folder_id is not None and session.scalar(
        select(Case.id).where(
            Case.id == case.id,
            (Case.folder_id.in_(select(blocked.c.id)) | Case.folder_id.in_(select(invalid.c.id))),
        )
    ):
        raise RunRejected("folder_unavailable", "来源用例所在目录不可用，请先整理目录。")
    return case


def _load_version_assertions(session: Session, version_id: uuid.UUID) -> list[dict]:
    from ..models import CaseAssertion

    rows = session.scalars(
        select(CaseAssertion)
        .where(CaseAssertion.case_version_id == version_id)
        .order_by(CaseAssertion.sort_order, CaseAssertion.assertion_id)
    )
    return [
        {
            "id": row.assertion_id,
            "target_source": row.target_source,
            "selector": row.selector,
            "type": row.type,
            "parameters": row.parameters,
            "compare_as": row.compare_as,
            "severity": row.severity,
            "enabled": row.enabled,
            "sort_order": row.sort_order,
        }
        for row in rows
    ]


def create_run(
    session: Session,
    settings: Settings,
    *,
    scope: deps.ProjectScope,
    payload: RunRequest,
) -> Run:
    require(scope.role, "execute")

    if payload.idempotency_key:
        existing = _idempotent_run(session, scope.workspace_id, scope.project_id, scope.principal.user_id, payload)
        if existing is not None:
            return existing

    scope = asset_lifecycle.lock_project_and_reauthorize(session, scope, "execute")
    if payload.idempotency_key:
        existing = _idempotent_run(session, scope.workspace_id, scope.project_id, scope.principal.user_id, payload)
        if existing is not None:
            return existing

    environment = session.scalar(
        select(Environment).where(
            Environment.id == payload.environment_id,
            Environment.project_id == scope.project_id,
            Environment.status == "active",
        )
    )
    if environment is None:
        raise RunRejected("environment_missing", "环境不存在或已归档。")

    resolved = resolve_pool(session, settings, environment)
    target_type, case_version_id, debug_source_case_id, request, assertions = _load_source(session, scope, payload)
    _, target_origin = resolve_target(request, environment, resolved.guard)

    # 调试快照没有版本号可固定，只能靠内容摘要绑定授权；摘要必须在创建事务里
    # 冻结进运行快照，执行时按同一份内容复核，避免执行阶段重新计算得到不同结果。
    snapshot_digest = (
        debug_snapshot_digest(request, assertions) if target_type == "debug_snapshot" else None
    )

    try:
        resolution = build_resolution(
            session,
            settings,
            environment=environment,
            principal_id=scope.principal.user_id,
            request=request,
            assertions=assertions,
            source_kind=target_type,
            source_id=case_version_id or debug_source_case_id,
            # 首次受理这里只做普通绑定与槽位占用早拒；身份/用途授权仍沿既有 worker
            # 权威链处理，不能因客户端未先预览就改变旧合法调用的受理边界。
            auth={
                "required": bool(request.get("auth_required")),
                "status": "none",
                "injection_slots": _auth_slot_metadata(session, environment),
                "requires_worker_verification": True,
            },
        )
    except ResolutionError as error:
        raise RunRejected(error.code, error.message) from error
    blocking_issue = next(
        (
            item
            for item in resolution["issues"]
            if item["code"]
            in {"variable_undefined", "binding_invalid", "credential_slot_conflict"}
        ),
        None,
    )
    if blocking_issue is not None:
        raise RunRejected(blocking_issue["code"], blocking_issue["message"])
    if payload.resolution_context is not None:
        try:
            verified_fingerprint = verify_resolution_context(
                payload.resolution_context,
                settings,
                environment=environment,
                principal_id=scope.principal.user_id,
                request=request,
                assertions=assertions,
                source_kind=target_type,
                source_id=case_version_id or debug_source_case_id,
                basis=resolution["config_basis"],
            )
            if not hmac.compare_digest(
                verified_fingerprint, resolution["context_fingerprint"]
            ):
                raise ResolutionError(
                    "resolution_context_changed",
                    "解析来源或绑定依据已变化，请重新预览后发送。",
                )
        except ResolutionError as error:
            raise RunRejected(error.code, error.message) from error

    now = datetime.now(UTC)
    business_deadline = now + timedelta(seconds=settings.business_deadline_seconds)
    frozen_variables = resolution["_merged_variables"]
    # 来源关联标记与运行快照同事务冻结：报告按它回答“这份结果是按哪份内容与输入
    # 产生的”。标记用受保护主密钥做 HMAC，不落内容摘要，详见 debug_context。
    context_binding = build_context_binding(
        settings.load_secret_key(),
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        principal_id=scope.principal.user_id,
        request=request,
        assertions=assertions,
        variables=frozen_variables,
    )
    run = Run(
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        target_type=target_type,
        case_version_id=case_version_id,
        debug_snapshot=payload.debug_snapshot if target_type == "debug_snapshot" else None,
        debug_source_case_id=debug_source_case_id,
        environment_id=environment.id,
        trigger="manual",
        state="queued",
        pool_id=resolved.pool.id,
        queue_deadline_at=now + timedelta(seconds=settings.queue_deadline_seconds),
        business_deadline_at=business_deadline,
        hard_deadline_at=business_deadline + timedelta(milliseconds=settings.cleanup_budget_ms),
        cleanup_budget_ms=settings.cleanup_budget_ms,
        created_by=scope.principal.user_id,
        snapshot={
            "environment": {
                "id": str(environment.id),
                "name": environment.name,
                "kind": environment.kind,
                "base_url": environment.base_url,
            },
            "target_origin": target_origin,
            "pool": {"id": str(resolved.pool.id), "name": resolved.pool.name},
            "request": request,
            "variables": frozen_variables,
            "assertions": assertions,
            "debug_snapshot_hash": snapshot_digest,
            "context_binding": context_binding.as_dict(),
            "resolution": resolution["_snapshot"],
        },
    )
    session.add(run)
    session.flush()

    session.add(
        Job(
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run.id,
            pool_id=resolved.pool.id,
            state="queued",
            available_at=now,
        )
    )
    if payload.idempotency_key:
        session.add(
            IdempotencyRecord(
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                principal_id=scope.principal.user_id,
                action="run:create",
                idempotency_key=payload.idempotency_key,
                request_hash=_request_hash(payload),
                result_ref=str(run.id),
                expires_at=now + timedelta(hours=_IDEMPOTENCY_TTL_HOURS),
            )
        )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if not payload.idempotency_key:
            raise
        existing = _idempotent_run(
            session,
            scope.workspace_id,
            scope.project_id,
            scope.principal.user_id,
            payload,
        )
        if existing is not None:
            return existing
        raise RunRejected(
            "idempotency_conflict",
            "幂等键并发用于另一项运行，已回滚本次受理。",
        ) from error
    return run


def _idempotent_run(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID,
    principal_id: uuid.UUID, payload: RunRequest
) -> Run | None:
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == workspace_id,
            IdempotencyRecord.principal_id == principal_id,
            IdempotencyRecord.action == "run:create",
            IdempotencyRecord.idempotency_key == payload.idempotency_key,
        )
    )
    if record is None:
        return None
    if record.request_hash != _request_hash(payload):
        raise RunRejected("idempotency_conflict", "幂等键已用于不同的请求内容，已拒绝。")
    if record.project_id != project_id or record.result_ref is None:
        raise RunRejected("idempotency_result_unavailable", "原运行结果不可用，已拒绝重新执行。")
    try:
        run_id = uuid.UUID(record.result_ref)
    except ValueError as error:
        raise RunRejected("idempotency_result_unavailable", "原运行结果不可用，已拒绝重新执行。") from error
    run = session.scalar(select(Run).where(
        Run.id == run_id, Run.workspace_id == workspace_id, Run.project_id == project_id,
    ))
    if run is None:
        raise RunRejected("idempotency_result_unavailable", "原运行结果不可用，已拒绝重新执行。")
    return run
