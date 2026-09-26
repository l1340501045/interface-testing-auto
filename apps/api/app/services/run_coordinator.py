"""运行协调器：创建运行与工作项，固定快照、身份与截止时间。

创建运行的事务负责：确认资源版本与当前权限 → 解析执行池与项目授权 → 固定
配置与身份版本 → 同事务写入 run / job。API 返回 202 只表示已入队，不表示
已经发出 HTTP；配置拒绝、入队、领取、发送意图与结果分别可追踪。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..kernel.environment_url import EnvironmentUrlError, check_base_url
from ..kernel.request_spec import RequestSpecError, validate_request
from ..kernel.target_policy import TargetGuard, TargetPolicyError
from ..models import (
    CaseVersion,
    Environment,
    IdempotencyRecord,
    Job,
    Run,
    RunnerPool,
    RunnerPoolProjectGrant,
)
from .debug_context import build_context_binding
from .permissions import require
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
    except RequestSpecError as error:
        raise RunRejected("case_invalid", f"调试请求定义无效：{error}") from error
    return debug_snapshot_digest(request, list(snapshot.get("assertions", [])))


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
    session: Session, scope_project_id: uuid.UUID, payload: RunRequest
) -> tuple[str, uuid.UUID | None, dict, list[dict]]:
    """确定运行目标：已发布版本或临时调试快照，两者互斥。"""
    if (payload.case_version_id is None) == (payload.debug_snapshot is None):
        raise RunRejected(
            "target_required", "请选择已发布用例版本或提供调试请求快照，二者只能有一个。"
        )

    if payload.case_version_id is not None:
        version = session.scalar(
            select(CaseVersion).where(
                CaseVersion.id == payload.case_version_id,
                CaseVersion.project_id == scope_project_id,
            )
        )
        if version is None:
            raise RunRejected("case_version_missing", "用例版本不存在或不属于本项目。")
        try:
            request = validate_request(version.request)
        except RequestSpecError as error:
            raise RunRejected("case_invalid", f"用例版本请求定义无效：{error}") from error
        assertions = _load_version_assertions(session, version.id)
        return "case_version", version.id, request, assertions

    snapshot = payload.debug_snapshot or {}
    try:
        request = validate_request(snapshot.get("request", {}))
    except RequestSpecError as error:
        raise RunRejected("case_invalid", f"调试请求定义无效：{error}") from error
    assertions = list(snapshot.get("assertions", []))
    return "debug_snapshot", None, request, assertions


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
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    principal_id: uuid.UUID,
    payload: RunRequest,
) -> Run:
    require(role, "execute")

    if payload.idempotency_key:
        existing = _idempotent_run(session, workspace_id, principal_id, payload)
        if existing is not None:
            return existing

    environment = session.scalar(
        select(Environment).where(
            Environment.id == payload.environment_id,
            Environment.project_id == project_id,
            Environment.status == "active",
        )
    )
    if environment is None:
        raise RunRejected("environment_missing", "环境不存在或已归档。")

    resolved = resolve_pool(session, settings, environment)
    target_type, case_version_id, request, assertions = _load_source(session, project_id, payload)
    _, target_origin = resolve_target(request, environment, resolved.guard)

    # 调试快照没有版本号可固定，只能靠内容摘要绑定授权；摘要必须在创建事务里
    # 冻结进运行快照，执行时按同一份内容复核，避免执行阶段重新计算得到不同结果。
    snapshot_digest = (
        debug_snapshot_digest(request, assertions) if target_type == "debug_snapshot" else None
    )

    now = datetime.now(UTC)
    business_deadline = now + timedelta(seconds=settings.business_deadline_seconds)
    frozen_variables = _merged_variables(session, environment)
    # 来源关联标记与运行快照同事务冻结：报告按它回答“这份结果是按哪份内容与输入
    # 产生的”。标记用受保护主密钥做 HMAC，不落内容摘要，详见 debug_context。
    context_binding = build_context_binding(
        settings.load_secret_key(),
        workspace_id=workspace_id,
        project_id=project_id,
        principal_id=principal_id,
        request=request,
        assertions=assertions,
        variables=frozen_variables,
    )
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        target_type=target_type,
        case_version_id=case_version_id,
        debug_snapshot=payload.debug_snapshot if target_type == "debug_snapshot" else None,
        environment_id=environment.id,
        trigger="manual",
        state="queued",
        pool_id=resolved.pool.id,
        queue_deadline_at=now + timedelta(seconds=settings.queue_deadline_seconds),
        business_deadline_at=business_deadline,
        hard_deadline_at=business_deadline + timedelta(milliseconds=settings.cleanup_budget_ms),
        cleanup_budget_ms=settings.cleanup_budget_ms,
        created_by=principal_id,
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
        },
    )
    session.add(run)
    session.flush()

    session.add(
        Job(
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=run.id,
            pool_id=resolved.pool.id,
            state="queued",
            available_at=now,
        )
    )
    if payload.idempotency_key:
        session.add(
            IdempotencyRecord(
                workspace_id=workspace_id,
                project_id=project_id,
                principal_id=principal_id,
                action="run:create",
                idempotency_key=payload.idempotency_key,
                request_hash=_request_hash(payload),
                result_ref=str(run.id),
                expires_at=now + timedelta(hours=_IDEMPOTENCY_TTL_HOURS),
            )
        )
    session.commit()
    return run


def _idempotent_run(
    session: Session, workspace_id: uuid.UUID, principal_id: uuid.UUID, payload: RunRequest
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
    if record.result_ref is None:
        return None
    return session.get(Run, uuid.UUID(record.result_ref))
