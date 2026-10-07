"""运行与结果模型：运行、步骤尝试、断言结果、工作项租约、幂等与审计。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import NULLABLE_JSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin
from .projects import project_fk, project_object_fk, workspace_object_fk


class Run(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_runs_ws_project_id"),
        project_fk("fk_runs_project"),
        project_object_fk(["case_version_id"], "case_versions", "fk_runs_case_version", ondelete="RESTRICT"),
        project_object_fk(["debug_source_case_id"], "cases", "fk_runs_debug_source_case", ondelete="RESTRICT"),
        project_object_fk(["environment_id"], "environments", "fk_runs_environment", ondelete="RESTRICT"),
        project_object_fk(["service_id"], "project_services", "fk_runs_service", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "environment_id", "service_id", "environment_service_version_id"],
            ["app.environment_service_versions.workspace_id", "app.environment_service_versions.project_id", "app.environment_service_versions.environment_id", "app.environment_service_versions.service_id", "app.environment_service_versions.id"],
            name="fk_runs_environment_service_version",
            ondelete="RESTRICT",
        ),
        workspace_object_fk(["pool_id"], "runner_pools", "fk_runs_pool", ondelete="SET NULL"),
        CheckConstraint(
            "target_type = 'case_version' OR target_type = 'debug_snapshot'",
            name="target_type",
        ),
        CheckConstraint(
            "debug_source_case_id IS NULL OR target_type = 'debug_snapshot'",
            name="debug_source_target",
        ),
        CheckConstraint(
            "(service_id IS NULL) = (environment_service_version_id IS NULL)",
            name="service_version_pair",
        ),
        CheckConstraint(
            "(target_type = 'case_version' AND case_version_id IS NOT NULL AND debug_snapshot IS NULL)"
            " OR (target_type = 'debug_snapshot' AND debug_snapshot IS NOT NULL AND case_version_id IS NULL)",
            name="target_consistency",
        ),
        {"comment": "接口用例单次执行记录，按工作空间与项目隔离"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    target_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="目标类型：case_version/debug_snapshot")
    case_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="执行的已发布用例版本；仅 target_type=case_version 时非空"
    )
    debug_snapshot: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="临时不可变调试快照；仅 target_type=debug_snapshot 时非空")
    debug_source_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="已保存用例临时调试的来源；独立调试、固定版本及历史运行为空",
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="目标环境")
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="命名服务引用；默认及历史运行为空")
    environment_service_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="命名服务受理时固定的映射版本；默认及历史运行为空")
    trigger: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual", comment="触发方式：manual"
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="created", server_default="created",
        comment="生命周期：created/queued/running/finished",
    )
    outcome: Mapped[str | None] = mapped_column(
        String(30), nullable=True,
        comment="终态结果：passed/failed/error/timed_out/canceled/interrupted/completed_unchecked",
    )
    reason_category: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="失败分类：assertion/network/authentication/configuration/policy/cleanup/platform/unknown",
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}", comment="脱敏执行快照，不含密钥明文")
    pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, comment="解析后的执行池")
    queue_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="最晚允许开始执行时间，排队超时不发请求")
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="首次领取并开始执行的数据库时间，只写一次")
    business_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="业务步骤截止时间")
    hard_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="整轮硬截止时间")
    cleanup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="首次进入清理的时间，只写一次")
    cleanup_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="清理截止，为清理开始加预算与硬截止的较早值")
    cleanup_budget_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=60000, server_default="60000", comment="清理最多耗时，单位毫秒")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="创建者用户 id")


class RunStepAttempt(Base):
    __tablename__ = "run_step_attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id", name="uq_run_step_attempts_ws_project_id"),
        UniqueConstraint(
            "workspace_id", "project_id", "run_id", "step_key", "attempt_no",
            name="uq_run_step_attempts_key_attempt",
        ),
        project_fk("fk_run_step_attempts_project"),
        project_object_fk(["run_id"], "runs", "fk_run_step_attempts_run", ondelete="CASCADE"),
        {"comment": "运行内步骤执行尝试，记录发送意图与脱敏结果"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属运行")
    step_key: Mapped[str] = mapped_column(String(50), nullable=False, default="main", server_default="main", comment="步骤键，单接口固定为 main")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="尝试序号，从 1 递增")
    state: Mapped[str] = mapped_column(String(20), nullable=False, comment="步骤状态：queued/sending/finished/skipped")
    outcome: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment=(
            "步骤结果：passed通过、failed断言失败、error执行或配置错误、"
            "interrupted结果不明、canceled已取消、completed_unchecked响应未校验；"
            "空表示没有已提交的最终结论"
        ),
    )
    send_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="持久化发送意图的时间")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="开始执行时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="结束时间")
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时，单位毫秒")
    request: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="脱敏后的实际发送请求")
    response: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="脱敏后的响应摘要")
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="稳定错误码")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class AssertionResult(Base):
    __tablename__ = "assertion_results"
    __table_args__ = (
        project_fk("fk_assertion_results_project"),
        project_object_fk(["run_id"], "runs", "fk_assertion_results_run", ondelete="CASCADE"),
        project_object_fk(["step_attempt_id"], "run_step_attempts", "fk_assertion_results_step_attempt", ondelete="CASCADE"),
        Index("ix_assertion_results_step_attempt", "step_attempt_id"),
        {"comment": "单次尝试中每条断言的求值结果"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属运行")
    step_attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属步骤尝试")
    assertion_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="断言标识")
    type: Mapped[str] = mapped_column(String(50), nullable=False, comment="断言类型")
    operator_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="公共方法语义版本")
    phase: Mapped[str] = mapped_column(String(20), nullable=False, comment="阶段：pre_request/post_response")
    target: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="检查目标摘要")
    status: Mapped[str] = mapped_column(String(20), nullable=False, comment="结果：passed/failed/error/skipped")
    expected: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="脱敏期望值")
    actual: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="脱敏实际值")
    reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="失败或跳过原因码")
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="求值耗时，单位毫秒")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class Job(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "run_id", name="uq_jobs_ws_project_run"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "run_id"],
            ["app.runs.workspace_id", "app.runs.project_id", "app.runs.id"],
            name="fk_jobs_run_scope",
            ondelete="CASCADE",
        ),
        workspace_object_fk(["pool_id"], "runner_pools", "fk_jobs_pool", ondelete="RESTRICT"),
        Index("ix_jobs_queued_available", "state", "available_at"),
        Index("ix_jobs_pool_queued", "pool_id", "state", "available_at"),
        {"comment": "持久任务队列与租约，按工作空间与项目范围绑定运行"},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="项目范围")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属运行")
    pool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="领取所需的执行池")
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued", comment="任务状态：queued/leased/done"
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="可领取时间")
    leased_by: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="领取者 worker 标识")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="租约到期时间")
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="递增 fencing token，每次领取 +1")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="领取次数")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), comment="更新时间")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "principal_id", "action", "idempotency_key",
            name="uq_idempotency_records_key",
        ),
        project_fk("fk_idempotency_records_project"),
        {"comment": "创建运行等动作的幂等保护记录"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="请求主体用户 id")
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="动作类型，如 run:create")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, comment="幂等键")
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="请求内容摘要")
    result_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="结果引用")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="过期时间")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        project_fk("fk_audit_events_project"),
        Index("ix_audit_events_project_created", "project_id", "created_at"),
        {"comment": "关键操作的脱敏审计记录"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid(), comment="主键，UUID"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="工作空间范围")
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="所属项目")
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, comment="操作者用户 id")
    action: Mapped[str] = mapped_column(String(100), nullable=False, comment="动作标识")
    object_type: Mapped[str] = mapped_column(String(100), nullable=False, comment="对象类型")
    object_id: Mapped[str] = mapped_column(String(200), nullable=False, comment="对象标识")
    diff: Mapped[dict | None] = mapped_column(NULLABLE_JSONB, nullable=True, comment="脱敏差异")
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="请求 trace_id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), comment="创建时间")
