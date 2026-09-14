"""运行创建、状态查询、脱敏报告与取消。

创建运行只做鉴权、快照固化与入队，返回 202 表示“已受理”，**不表示已经发出
HTTP**。真正的发送由独立 worker 在同一内核里完成，页面轮询这里读取状态。
取消会立即把运行置为终态，worker 在发出请求前重新校验运行状态，因此取消后
不会产生新的目标副作用。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...config import Settings
from ...db import get_db
from ...models import AssertionResult, Run, RunStepAttempt
from ...services.run_coordinator import (
    RunRejected,
    RunRequest,
    create_run,
    digest_for_debug_snapshot,
)
from .. import deps
from ..errors import bad_request, conflict, forbidden, not_found
from ..schemas import (
    AssertionResultOut,
    DebugSnapshot,
    DebugSnapshotDigestOut,
    RunCreate,
    RunOut,
    RunReportOut,
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


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/runs",
    response_model=RunOut,
    status_code=202,
)
def start_run(
    payload: RunCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
    settings: Settings = Depends(deps.get_settings_dep),
    session: Session = Depends(get_db),
) -> RunOut:
    if (payload.case_version_id is None) == (payload.debug_snapshot is None):
        raise bad_request(
            "target_required", "请选择已发布用例版本或提供调试快照，二者只能有一个。"
        )
    try:
        run = create_run(
            session,
            settings,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            role=scope.role,
            principal_id=scope.principal.user_id,
            payload=RunRequest(
                environment_id=payload.environment_id,
                case_version_id=payload.case_version_id,
                debug_snapshot=payload.debug_snapshot.model_dump() if payload.debug_snapshot else None,
                idempotency_key=idempotency_key,
            ),
        )
    except RunRejected as error:
        if error.code in ("production_blocked", "pool_not_granted"):
            raise forbidden(error.code, error.message) from error
        if error.code in ("environment_missing", "case_version_missing"):
            # 越权与不存在都按“不可见”处理，不泄露其他项目资源是否存在。
            raise not_found("环境或用例版本不存在") from error
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
    scope: deps.ProjectScope = _EXECUTE_SCOPE,
) -> DebugSnapshotDigestOut:
    """计算调试快照摘要，供身份管理员据此签发一次性授权。

    这是一条纯计算接口：不落库、不发请求。摘要由服务端按与创建运行相同的规范化
    步骤算出，管理员签发的授权与实际执行的内容因此指向同一份快照。计算摘要本身
    不构成权限提升——签发授权仍需要 `manage_secrets` 管理角色。
    """
    try:
        digest = digest_for_debug_snapshot(
            RunRequest(environment_id=uuid.uuid4(), debug_snapshot=payload.model_dump())
        )
    except RunRejected as error:
        raise bad_request(error.code, error.message) from error
    return DebugSnapshotDigestOut(hash=digest)


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
    _get_run(session, scope, run_id)
    items = session.scalars(
        select(RunStepAttempt)
        .where(RunStepAttempt.run_id == run_id)
        .order_by(RunStepAttempt.attempt_no)
    )
    return [
        RunStepOut(
            step_key=item.step_key,
            attempt_no=item.attempt_no,
            state=item.state,
            outcome=item.outcome,
            elapsed_ms=item.elapsed_ms,
            error_code=item.error_code,
        )
        for item in items
    ]


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/runs/{run_id}/report",
    response_model=RunReportOut,
)
def get_report(
    run_id: uuid.UUID,
    scope: deps.ProjectScope = _VIEW_SCOPE,
    session: Session = Depends(get_db),
) -> RunReportOut:
    """脱敏报告：请求与响应证据来自最后一次尝试，秘密已被遮蔽。"""
    run = _get_run(session, scope, run_id)
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
    return RunReportOut(
        run=_run_out(run),
        steps=[
            RunStepOut(
                step_key=item.step_key,
                attempt_no=item.attempt_no,
                state=item.state,
                outcome=item.outcome,
                elapsed_ms=item.elapsed_ms,
                error_code=item.error_code,
            )
            for item in attempts
        ],
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
        request=latest.request if latest is not None else None,
        response=latest.response if latest is not None else None,
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
