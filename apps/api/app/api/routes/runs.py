"""运行创建、状态查询、脱敏报告与取消。

创建运行只做鉴权、快照固化与入队，返回 202 表示“已受理”，**不表示已经发出
HTTP**。真正的发送由独立 worker 在同一内核里完成，页面轮询这里读取状态。
取消会立即把运行置为终态，worker 在发出请求前重新校验运行状态，因此取消后
不会产生新的目标副作用。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_db
from ...kernel.assertion_spec import AssertionSpecError, validate_assertions
from ...kernel.request_spec import RequestSpecError, ServiceSpecError, validate_request
from ...models import AssertionResult, Environment, Run, RunStepAttempt
from ...services.debug_context import (
    binding_is_current,
    has_guard_semantics,
    read_context_binding,
    read_resolution_proof,
    strip_guard_semantics,
)
from ...services.debug_preflight import PreflightIssue, inspect_auth_metadata, preflight
from ...services.folder_graph import begin_consistent_read
from ...services.permissions import can, require
from ...services.resolution import (
    ResolutionError,
    build_resolution,
    public_resolution,
)
from ...services.run_coordinator import (
    RunRejected,
    RunRequest,
    _load_source,
    _require_case_available,
    create_run,
    digest_for_debug_snapshot,
    resolve_pool,
    resolve_target,
)
from ...services.run_coordinator import (
    debug_snapshot_digest as compute_debug_snapshot_digest,
)
from ...services.service_targets import (
    ServiceTargetError,
    inspect_selected_target,
)
from .. import deps
from ..errors import ApiError, bad_request, conflict, forbidden, not_found
from ..request_contract import (
    has_row_locator,
    is_named_service,
    is_v2,
    require_service_capability,
    require_v2_capability,
)
from ..schemas import (
    AssertionResultOut,
    DebugPreflightOut,
    DebugPreflightRequest,
    DebugSnapshot,
    DebugSnapshotDigestOut,
    PreflightAuthOut,
    PreflightIssueOut,
    ResolutionPreviewOut,
    ResolutionPreviewRequest,
    RunContextOut,
    RunCreate,
    RunFrozenResolutionOut,
    RunOut,
    RunReportOut,
    RunResolutionContextOut,
    RunSourceEnvironment,
    RunStepInterpretationOut,
    RunStepOut,
)

router = APIRouter(tags=["运行"])

_VIEW_SCOPE = Depends(deps.view_scope)
_EXECUTE_SCOPE = Depends(deps.execute_scope)


def _run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        target_type=run.target_type,
        case_version_id=run.case_version_id,
        debug_source_case_id=run.debug_source_case_id,
        environment_id=run.environment_id,
        state=run.state,
        outcome=run.outcome,
        reason_category=run.reason_category,
        pool_id=run.pool_id,
        created_at=run.created_at,
    )


def _get_run(session: Session, scope: deps.ProjectScope, run_id: uuid.UUID) -> Run:
    run = session.scalar(select(Run).where(Run.id == run_id, Run.project_id == scope.project_id))
    if run is None:
        raise not_found("运行记录不存在")
    return run


def _legacy_unchecked_attempt_id(
    run: Run, attempts: list[RunStepAttempt]
) -> uuid.UUID | None:
    """识别旧 worker 有损映射出的最终 main 尝试；证据不足时不解释。"""
    if (
        run.state != "finished"
        or run.outcome != "completed_unchecked"
        or run.target_type not in {"case_version", "debug_snapshot"}
        or not attempts
    ):
        return None
    attempt_numbers = [item.attempt_no for item in attempts]
    if (
        any(item.step_key != "main" for item in attempts)
        or any(number <= 0 for number in attempt_numbers)
        or len(set(attempt_numbers)) != len(attempt_numbers)
    ):
        return None
    latest = max(attempts, key=lambda item: item.attempt_no)
    if (
        latest.state != "finished"
        or latest.outcome != "error"
        or latest.send_intent_at is None
        or latest.started_at is None
        or latest.finished_at is None
    ):
        return None
    response = latest.response
    status = response.get("status") if isinstance(response, dict) else None
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None
    reason_pair = (run.reason_category, latest.error_code)
    if reason_pair not in {
        (None, None),
        ("assertion", "assertion_not_evaluated"),
    }:
        return None
    return latest.id


def _run_steps_out(run: Run, attempts: list[RunStepAttempt]) -> list[RunStepOut]:
    """两个报告出口共用的只读步骤投影；不修改 ORM，也不重算断言。"""
    interpreted_id = _legacy_unchecked_attempt_id(run, attempts)
    return [
        RunStepOut(
            step_key=item.step_key,
            attempt_no=item.attempt_no,
            state=item.state,
            outcome=item.outcome,
            elapsed_ms=item.elapsed_ms,
            error_code=item.error_code,
            interpretation=(
                RunStepInterpretationOut(
                    outcome="completed_unchecked",
                    reason_code="legacy_unchecked_mapping",
                )
                if item.id == interpreted_id
                else None
            ),
        )
        for item in attempts
    ]


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/runs",
    response_model=RunOut,
    status_code=202,
)
def start_run(
    payload: RunCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> RunOut:
    if (payload.case_version_id is None) == (payload.debug_snapshot is None):
        raise bad_request(
            "target_required", "请选择已发布用例版本或提供调试快照，二者只能有一个。"
        )
    if payload.debug_snapshot is not None:
        require_v2_capability(
            request_contract, needed=is_v2(payload.debug_snapshot.request)
        )
    # 这里只校验头本身的字面能力；named首次准入必须在幂等原结果查询之后，
    # 否则同key/body去掉能力头的合法回放会被挡在原Run之前。
    require_service_capability(service_contract, needed=False)
    try:
        run = create_run(
            session,
            settings,
            scope=scope,
            payload=RunRequest(
                environment_id=payload.environment_id,
                case_version_id=payload.case_version_id,
                debug_snapshot=payload.debug_snapshot.model_dump() if payload.debug_snapshot else None,
                idempotency_key=idempotency_key,
                source_case_id=payload.source_case_id,
                resolution_context=payload.resolution_context,
                service_capability=service_contract == "1",
            ),
        )
    except RunRejected as error:
        if error.code in ("production_blocked", "pool_not_granted"):
            raise forbidden(error.code, error.message) from error
        if error.code in ("environment_missing", "case_version_missing", "case_missing"):
            # 越权与不存在都按“不可见”处理，不泄露其他项目资源是否存在。
            raise not_found("环境或用例版本不存在") from error
        if error.code == "idempotency_result_unavailable":
            raise ApiError(409, error.code, error.message) from error
        if error.code in {
            "resolution_context_changed",
            "config_inconsistent",
            "service_unavailable",
            "mapping_missing",
            "service_contract_required",
        }:
            raise conflict(error.code, error.message) from error
        raise bad_request(error.code, error.message) from error
    response.headers["Location"] = (
        f"/api/v1/workspaces/{scope.workspace_id}/projects/{scope.project_id}/runs/{run.id}"
    )
    return _run_out(run)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/debug-snapshot-digest",
    response_model=DebugSnapshotDigestOut,
)
def debug_snapshot_digest(
    payload: DebugSnapshot,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
) -> DebugSnapshotDigestOut:
    """计算调试快照摘要，供身份管理员据此签发一次性授权。

    这是一条纯计算接口：不落库、不发请求。摘要由服务端按与创建运行相同的规范化
    步骤算出，管理员签发的授权与实际执行的内容因此指向同一份快照。计算摘要本身
    不构成权限提升——签发授权仍需要 `manage_secrets` 管理角色。
    """
    require_v2_capability(request_contract, needed=is_v2(payload.request))
    require_service_capability(service_contract, needed=is_named_service(payload.request))
    try:
        digest = digest_for_debug_snapshot(
            RunRequest(environment_id=uuid.uuid4(), debug_snapshot=payload.model_dump())
        )
    except RunRejected as error:
        raise bad_request(error.code, error.message) from error
    return DebugSnapshotDigestOut(hash=digest)


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/debug-preflight",
    response_model=DebugPreflightOut,
)
def debug_preflight(
    payload: DebugPreflightRequest,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> DebugPreflightOut:
    """发送前预检：只报告当前主体此刻的准入状态，不创建运行、不消费授权。

    权限为 execute（能执行的人才能问“我能不能执行”），而不是凭证管理的
    `manage_secrets`：普通编辑者拿不到 `GET credentials/*`，页面因此无法给出
    “到底缺什么”。这里返回脱敏结论与建议动作，真正发送前仍按权威规则重新检查。
    """
    scope = begin_consistent_read(session, scope)
    require(scope.role, "execute")
    require_v2_capability(
        request_contract, needed=is_v2(payload.debug_snapshot.request)
    )
    require_service_capability(
        service_contract, needed=is_named_service(payload.debug_snapshot.request)
    )
    try:
        validate_request(payload.debug_snapshot.request)
    except ServiceSpecError as error:
        raise bad_request("service_invalid", str(error)) from error
    except RequestSpecError:
        # 原普通请求结构错误继续由旧preflight 200问题信封表达。
        pass
    if payload.source_case_id is not None:
        try:
            _require_case_available(session, scope, payload.source_case_id)
        except RunRejected as error:
            action = "restore_case" if error.code == "case_archived" else "organize_case"
            return DebugPreflightOut(
                ready=False,
                issues=[PreflightIssueOut(code=error.code, message=error.message, action=action)],
                can_authorize=False,
                auth=PreflightAuthOut(required=False, state="none", profile_id=None),
                context=None,
            )
    environment_for_target = session.scalar(
        select(Environment).where(
            Environment.id == payload.environment_id,
            Environment.project_id == scope.project_id,
            Environment.status == "active",
        )
    )
    selected = None
    selected_summary = None
    target_error = None
    if environment_for_target is not None:
        try:
            selected_summary, selected, target_error = inspect_selected_target(
                session, environment_for_target, validate_request(payload.debug_snapshot.request)
            )
        except ServiceTargetError as error:
            raise ApiError(error.status_code, error.code, error.message) from error
        except RequestSpecError:
            selected_summary, selected, target_error = None, None, None
    result = preflight(
        session,
        settings,
        project_id=scope.project_id,
        principal_id=scope.principal.user_id,
        role=scope.role,
        environment_id=payload.environment_id,
        snapshot=payload.debug_snapshot.model_dump(),
        base_url=selected.base_url if selected is not None else None,
    )
    if target_error is not None:
        result.ready = False
        result.context = None
        if not any(item.code == target_error.code for item in result.issues):
            result.issues.append(
                PreflightIssue(target_error.code, target_error.message, "configure_environment")
            )
    resolution: dict | None = None
    resolution_issues: list[PreflightIssueOut] = []
    try:
        environment = session.scalar(
            select(Environment).where(
                Environment.id == payload.environment_id,
                Environment.project_id == scope.project_id,
                Environment.status == "active",
            )
        )
        if environment is not None and not (
            target_error is not None and target_error.global_basis
        ):
            request = validate_request(payload.debug_snapshot.request)
            assertions = list(payload.debug_snapshot.assertions)
            validate_assertions(assertions, request)
            schema = 2 if service_contract == "1" else 1
            target_ref = (
                selected.target_ref(environment.id)
                if selected is not None and target_error is None
                else None
            )
            resolution_value = build_resolution(
                session,
                settings,
                environment=environment,
                principal_id=scope.principal.user_id,
                request=request,
                assertions=assertions,
                source_kind="debug_snapshot",
                source_id=payload.source_case_id,
                auth={
                    "required": result.auth_required,
                    "status": result.auth_state,
                    "injection_slots": result.injection_slots,
                    "requires_worker_verification": result.requires_worker_verification,
                },
                base_url=selected.base_url if selected is not None else environment.base_url,
                resolution_schema_version=schema,
                target_ref=target_ref if schema == 2 else None,
                selected_target=selected_summary if schema == 2 else None,
            )
            if target_error is not None:
                resolution_value["issues"].append(
                    _resolution_issue(
                        target_error.code, target_error.message, "configure_environment"
                    )
                )
                resolution_value["ready"] = False
                resolution_value["masked_target"] = None
                resolution_value["resolution_context"] = None
            if not result.ready:
                # 旧信封的环境、池与策略结论同样属于完整resolution.ready；普通配置
                # 仍可独立签发context，不能因总ready=false把S1普通并发依据抹掉。
                resolution_value["ready"] = False
            resolution = public_resolution(resolution_value)
            resolution_issues = [
                PreflightIssueOut(
                    code=item["code"], message=item["message"], action=item["action"]
                )
                for item in resolution["issues"]
            ]
    except ResolutionError as error:
        result.ready = False
        resolution_issues = [
            PreflightIssueOut(
                code=error.code,
                message=error.message,
                action="configure_environment",
            )
        ]
        resolution = None
    except (RequestSpecError, AssertionSpecError):
        # 原 debug-preflight 信封继续由既有逻辑报告请求/断言/环境问题；解析扩展不能
        # 覆盖或改变旧字段语义。能建立同源解析对象时才附加 resolution。
        resolution = None
    combined_issues = [
        PreflightIssueOut(code=item.code, message=item.message, action=item.action)
        for item in result.issues
    ]
    existing_codes = {item.code for item in combined_issues}
    combined_issues.extend(item for item in resolution_issues if item.code not in existing_codes)
    return DebugPreflightOut(
        ready=result.ready and (resolution is None or bool(resolution["ready"])),
        issues=combined_issues,
        can_authorize=result.can_authorize,
        auth=PreflightAuthOut(
            required=result.auth_required,
            state=result.auth_state,
            profile_id=result.profile_id,
        ),
        context=RunContextOut(**result.context) if result.context is not None else None,
        resolution=ResolutionPreviewOut(**resolution) if resolution is not None else None,
    )


def _resolution_issue(code: str, message: str, action: str = "edit_request") -> dict:
    return {
        "issue_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"resolution:{code}:{action}")),
        "code": code,
        "message": message,
        "action": action,
        "location": None,
    }


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/resolution-preview",
    response_model=ResolutionPreviewOut,
)
def resolution_preview(
    payload: ResolutionPreviewRequest,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> ResolutionPreviewOut:
    scope = begin_consistent_read(session, scope)
    if payload.debug_snapshot is not None:
        require_v2_capability(request_contract, needed=is_v2(payload.debug_snapshot.request))
    environment = session.scalar(
        select(Environment).where(
            Environment.id == payload.environment_id,
            Environment.project_id == scope.project_id,
            Environment.status == "active",
        )
    )
    if environment is None:
        raise not_found("环境不存在或已归档")
    run_request = RunRequest(
        environment_id=payload.environment_id,
        case_version_id=payload.case_version_id,
        debug_snapshot=payload.debug_snapshot.model_dump() if payload.debug_snapshot else None,
        source_case_id=payload.source_case_id,
    )
    try:
        target_type, case_version_id, debug_source_case_id, request, assertions = _load_source(
            session, scope, run_request
        )
        require_service_capability(service_contract, needed=is_named_service(request))
        selected_summary, selected, target_error = inspect_selected_target(
            session, environment, request
        )
        if target_error is not None and target_error.global_basis:
            raise ApiError(
                target_error.status_code, target_error.code, target_error.message
            )
        pool_issue = None
        try:
            resolved = resolve_pool(session, settings, environment)
        except RunRejected as error:
            if error.code not in {
                "pool_not_granted", "pool_unavailable", "pool_rebound", "pool_config_invalid"
            }:
                raise
            resolved = None
            pool_issue = error
        target_origin = None
        if selected is not None and resolved is not None:
            _request, target_origin = resolve_target(
                request, environment, resolved.guard, base_url=selected.base_url
            )
        digest = (
            compute_debug_snapshot_digest(request, assertions)
            if target_type == "debug_snapshot"
            else None
        )
        if can(scope.role, "execute") and target_origin is not None:
            auth, auth_issues = inspect_auth_metadata(
                session,
                environment=environment,
                principal_id=scope.principal.user_id,
                target_origin=target_origin,
                auth_required=bool(request.get("auth_required")),
                case_version_id=case_version_id,
                debug_snapshot_hash=digest,
                can_authorize=can(scope.role, "manage_secrets"),
            )
        else:
            auth = {
                "required": bool(request.get("auth_required")),
                "status": "unchecked" if not can(scope.role, "execute") else "unavailable",
                "injection_slots": [],
                "requires_worker_verification": True,
            }
            auth_issues = []
        schema = 2 if service_contract == "1" else 1
        value = build_resolution(
            session,
            settings,
            environment=environment,
            principal_id=scope.principal.user_id,
            request=request,
            assertions=assertions,
            source_kind=target_type,
            source_id=case_version_id or debug_source_case_id,
            auth=auth,
            base_url=selected.base_url if selected is not None else environment.base_url,
            resolution_schema_version=schema,
            target_ref=(
                selected.target_ref(environment.id)
                if schema == 2 and selected is not None and target_error is None
                else None
            ),
            selected_target=selected_summary if schema == 2 else None,
        )
        if target_error is not None:
            value["issues"].append(
                _resolution_issue(
                    target_error.code, target_error.message, "configure_environment"
                )
            )
            value["ready"] = False
            value["masked_target"] = None
            value["resolution_context"] = None
            value["target_ref"] = None
        if pool_issue is not None:
            value["issues"].append(
                _resolution_issue(pool_issue.code, pool_issue.message, "configure_environment")
            )
            value["ready"] = False
        for issue in auth_issues:
            value["issues"].append(_resolution_issue(issue.code, issue.message, issue.action))
        if auth_issues:
            value["ready"] = False
        return ResolutionPreviewOut(**public_resolution(value))
    except ResolutionError as error:
        raise conflict(error.code, error.message) from error
    except ServiceTargetError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    except RunRejected as error:
        if error.code in {"case_version_missing", "case_missing", "environment_missing"}:
            raise not_found("环境或用例版本不存在") from error
        raise bad_request(error.code, error.message) from error


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/runs",
    response_model=list[RunOut],
)
def list_runs(
    environment_id: uuid.UUID | None = None,
    case_version_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> list[RunOut]:
    query = select(Run).where(Run.project_id == scope.project_id)
    if environment_id is not None:
        query = query.where(Run.environment_id == environment_id)
    if case_version_id is not None:
        query = query.where(Run.case_version_id == case_version_id)
    items = session.scalars(query.order_by(Run.created_at.desc()).limit(limit))
    return [_run_out(item) for item in items]


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/runs/{run_id}",
    response_model=RunOut,
)
def get_run(
    run_id: uuid.UUID,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> RunOut:
    return _run_out(_get_run(session, scope, run_id))


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/runs/{run_id}/steps",
    response_model=list[RunStepOut],
)
def list_steps(
    run_id: uuid.UUID,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> list[RunStepOut]:
    run = _get_run(session, scope, run_id)
    attempts = list(
        session.scalars(
            select(RunStepAttempt)
            .where(RunStepAttempt.run_id == run_id)
            .order_by(RunStepAttempt.attempt_no)
        )
    )
    return _run_steps_out(run, attempts)


def _report_context(
    run: Run, attempt: RunStepAttempt | None, settings: Settings
) -> RunContextOut | None:
    """从冻结快照生成可证明的运行来源；给不出结论时返回 None。

    只认执行器写在步骤证据里的语义标记：旧记录、旧执行器的产物、乃至“入队成功但
    执行保护从未跑过”的记录都拿不到标记，因此不给结论。不能凭创建 API 写下的版本
    标记倒推保护已执行——受理请求与真正执行保护是两件事。

    内容全部取自冻结快照，不重新读当前环境：环境可能已被改动，重新读出来的是另一
    份配置，用它标注历史结果会把“曾经记录的配置”当成“实际执行依据”。关联标记以
    **Run 的创建者**为绑定主体，与预检（当前登录主体）生成的是同一套算法。
    """
    if attempt is None or not has_guard_semantics(attempt.request):
        return None
    snapshot = run.snapshot or {}
    frozen = snapshot.get("environment")
    if not isinstance(frozen, dict) or not frozen.get("id"):
        return None
    try:
        environment_id = uuid.UUID(str(frozen["id"]))
    except ValueError:
        return None
    binding = read_context_binding(snapshot)
    if binding is None:
        return None
    try:
        key = settings.load_secret_key()
    except RuntimeError:
        # 主密钥读不出来时无法判断标记是否仍然有效，按历史处理，不猜。
        return None
    if not binding_is_current(binding, key):
        # 主密钥已轮换：旧标记不再可能匹配当前密钥下的任何输入。降为历史显示，
        # 不重算一个当前密钥下的值贴到旧记录上——那是替旧记录编造依据。
        return None
    resolution_proof = read_resolution_proof(
        attempt.request,
        snapshot,
        key,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        principal_id=run.created_by,
    )
    return RunContextOut(
        snapshot_fingerprint=binding.snapshot_fingerprint,
        environment=RunSourceEnvironment(
            id=environment_id,
            name=str(frozen.get("name") or ""),
            kind=str(frozen.get("kind") or ""),
            base_url=str(frozen.get("base_url") or ""),
        ),
        input_fingerprint=binding.input_fingerprint,
        resolution=(
            RunResolutionContextOut(**resolution_proof)
            if resolution_proof is not None
            else None
        ),
    )


def _report_resolution(run: Run) -> RunFrozenResolutionOut | None:
    """只读冻结的 S1 来源白名单字段；坏旁路不影响旧报告基本读取。"""
    snapshot = run.snapshot if isinstance(run.snapshot, dict) else {}
    raw = snapshot.get("resolution")
    if not isinstance(raw, dict):
        return None
    schema_version = raw.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in (1, 2)
    ):
        return None
    allowed = {
        "schema_version": raw.get("schema_version"),
        "config_basis": raw.get("config_basis"),
        "variable_sources": raw.get("variable_sources"),
        "bindings": raw.get("bindings"),
        "context_fingerprint": raw.get("context_fingerprint"),
        "target_ref": raw.get("target_ref"),
    }
    try:
        return RunFrozenResolutionOut(**allowed)
    except ValidationError:
        return None


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/runs/{run_id}/report",
    response_model=RunReportOut,
)
def get_report(
    run_id: uuid.UUID,
    request_contract: str | None = Header(default=None, alias="X-Request-Contract"),
    service_contract: str | None = Header(default=None, alias="X-Service-Contract"),
    scope: deps.ProjectScope = _VIEW_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> RunReportOut:
    """脱敏报告：请求与响应证据来自最后一次尝试，秘密已被遮蔽。"""
    run = _get_run(session, scope, run_id)
    snapshot = run.snapshot or {}
    raw_resolution = snapshot.get("resolution")
    raw_schema_version = (
        raw_resolution.get("schema_version") if isinstance(raw_resolution, dict) else None
    )
    schema2 = (
        isinstance(raw_schema_version, int)
        and not isinstance(raw_schema_version, bool)
        and raw_schema_version == 2
    )
    # 命名能力来自受理时写入的权威Run列；损坏target_ref不能把named伪装成default。
    named_target = run.service_id is not None
    require_service_capability(service_contract, needed=named_target)
    require_v2_capability(
        request_contract,
        needed=has_row_locator(snapshot.get("assertions")),
    )
    attempts = list(
        session.scalars(
            select(RunStepAttempt)
            .where(RunStepAttempt.run_id == run_id)
            .order_by(RunStepAttempt.attempt_no)
        )
    )
    latest = attempts[-1] if attempts else None
    results = (
        list(
            session.scalars(
                select(AssertionResult)
                .where(AssertionResult.step_attempt_id == latest.id)
                .order_by(AssertionResult.created_at, AssertionResult.assertion_id)
            )
        )
        if latest is not None
        else []
    )
    context = _report_context(run, latest, settings)
    frozen_resolution = _report_resolution(run)
    if schema2 and service_contract != "1":
        frozen_resolution = None
        if context is not None:
            context.resolution = None
    return RunReportOut(
        run=_run_out(run),
        steps=_run_steps_out(run, attempts),
        assertions=[
            AssertionResultOut(
                assertion_id=item.assertion_id,
                type=item.type,
                phase=item.phase,
                target=item.target,
                status=item.status,
                expected=item.expected,
                actual=item.actual,
                reason_code=item.reason_code,
                elapsed_ms=item.elapsed_ms,
            )
            for item in results
        ],
        request=strip_guard_semantics(latest.request) if latest is not None else None,
        response=latest.response if latest is not None else None,
        context=context,
        resolution=frozen_resolution,
    )


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/runs/{run_id}/cancel",
    response_model=RunOut,
)
def cancel_run(
    run_id: uuid.UUID,
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
    session: Session = Depends(get_db),
) -> RunOut:
    """取消运行：立即置为终态并结束未领取的工作项，阻止新的发送。

    取消本身也必须是有条件的原子状态变更。若先读状态、再按对象属性写回，运行可能
    在两步之间被 worker 正常结束（终态已提交），取消就会把 passed／failed 覆写为
    canceled，抹掉真实结果；反过来，worker 也可能在读到“未结束”之后把状态翻回
    运行中。因此这里直接按允许的前态做一次条件更新，更新不到行就说明它已经结束。
    """
    run = _get_run(session, scope, run_id)

    canceled = session.execute(
        text(
            "UPDATE app.runs SET state = 'finished', outcome = 'canceled',"
            " reason_category = 'policy', updated_at = now() "
            "WHERE id = :run AND workspace_id = :ws AND state IN ('created', 'queued', 'running') "
            "RETURNING id"
        ),
        {"run": str(run.id), "ws": str(scope.workspace_id)},
    ).first()
    if canceled is None:
        session.rollback()
        raise conflict("run_already_finished", "该运行已经结束，无法取消。")

    session.execute(
        text(
            "UPDATE app.jobs SET state = 'done', lease_until = NULL, updated_at = now() "
            "WHERE run_id = :run AND workspace_id = :ws AND state = 'queued'"
        ),
        {"run": str(run.id), "ws": str(scope.workspace_id)},
    )
    deps.commit(session)
    session.refresh(run)
    return _run_out(run)
