"""发送前预检：只报告当前主体此刻能否把这份调试请求发出去。

预检补的是现有接口的一个真空：凭证管理接口（含 GET）一律要求 `manage_secrets`，
普通编辑者连“我这份请求还差什么”都读不到，界面只能显示一个不可点击的按钮。这里
按当前主体与当前环境给出脱敏的准入结论与可操作动作。

**它不是执行凭证。** 预检只读：不解密秘密、不消费一次性授权、不创建运行、不访问
目标 HTTP。所有判定都复用服务端已有实现（请求规范化、普通变量合并、执行池与目标
策略、凭证授权匹配），因此预检说“可以发送”与执行期说“可以发送”依据同一套规则。
真正发送前仍会按权威规则重新检查一次——预检与发送之间配置仍可能变化。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..kernel.assertion_spec import AssertionSpecError, validate_assertions
from ..kernel.request_spec import RequestSpecError, ServiceSpecError, validate_request
from ..models import CredentialSet, Environment
from .credentials import (
    CREDENTIAL_AMBIGUOUS,
    CREDENTIAL_REQUIRED,
    CredentialError,
    available_profiles,
    check_usable_grant,
    parse_auth_slot,
    set_auth_slots,
)
from .debug_context import build_context_binding
from .permissions import can
from .run_coordinator import (
    RunRejected,
    debug_snapshot_digest,
    resolve_pool,
    resolve_target,
)
from .variable_inputs import merged_variables


@dataclass(frozen=True)
class PreflightIssue:
    """一条可操作的问题：稳定错误码 + 中文说明 + 建议动作。"""

    code: str
    message: str
    action: str


@dataclass
class PreflightResult:
    ready: bool
    issues: list[PreflightIssue] = field(default_factory=list)
    can_authorize: bool = False
    auth_required: bool = False
    auth_state: str = "none"
    profile_id: uuid.UUID | None = None
    context: dict | None = None
    injection_slots: list[dict] = field(default_factory=list)
    requires_worker_verification: bool = False


# 需要管理员调整环境／执行池配置的问题码；其余归到“改请求”。
#
# `environment_url_invalid` 属于环境本身配错了地址（缺协议、主机或端口不合法），下一步是
# 去环境设置里改地址，不是去动用例。它与 `target_not_allowed` 不是一回事：后者是地址
# 合法但不在执行池白名单内，仍然按“改请求／确认目标”处理，不能一并归成环境问题。
_ENVIRONMENT_ACTIONS = {
    "production_blocked",
    "pool_not_granted",
    "pool_unavailable",
    "pool_rebound",
    "pool_config_invalid",
    "environment_url_invalid",
}


def preflight(
    session: Session,
    settings: Settings,
    *,
    project_id: uuid.UUID,
    principal_id: uuid.UUID,
    role: str,
    environment_id: uuid.UUID,
    snapshot: dict,
    base_url: str | None = None,
) -> PreflightResult:
    """检查当前主体能否发送这份调试快照。"""
    can_authorize = can(role, "manage_secrets")
    environment = session.scalar(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.project_id == project_id,
            Environment.status == "active",
        )
    )
    if environment is None:
        return PreflightResult(
            ready=False,
            issues=[
                PreflightIssue(
                    "environment_missing",
                    "环境不存在、已归档或不属于本项目，请重新选择执行环境。",
                    "select_environment",
                )
            ],
            can_authorize=can_authorize,
        )

    request_payload = snapshot.get("request", {})
    try:
        request = validate_request(request_payload)
    except ServiceSpecError as error:
        return PreflightResult(
            ready=False,
            issues=[PreflightIssue("service_invalid", str(error), "edit_request")],
            can_authorize=can_authorize,
        )
    except RequestSpecError as error:
        return PreflightResult(
            ready=False,
            issues=[PreflightIssue("case_invalid", f"调试请求定义无效：{error}", "edit_request")],
            can_authorize=can_authorize,
        )
    assertions = list(snapshot.get("assertions", []))
    auth_required = bool(request.get("auth_required"))
    try:
        # 断言按执行期的同一份校验过一遍，但**不用校验结果改写 assertions**：调试快照
        # 摘要是对提交的原始数组算的，换成规范化后的副本会让既有授权全部失配。这里
        # 只借它回答“这份配置能不能执行”，不改变任何参与摘要的内容。
        validate_assertions(assertions, request)
    except AssertionSpecError as error:
        return PreflightResult(
            ready=False,
            issues=[PreflightIssue("case_invalid", f"断言配置无效：{error}", "edit_request")],
            can_authorize=can_authorize,
            auth_required=auth_required,
        )

    # 环境与请求都能定位时才有来源可报；两者缺一都返回 null，不猜一个地址出来。
    # 标记以**当前登录主体**为绑定主体（报告侧以 Run 创建者为绑定主体，同一套算法）。
    frozen_variables = merged_variables(session, environment)
    binding = build_context_binding(
        settings.load_secret_key(),
        workspace_id=environment.workspace_id,
        project_id=environment.project_id,
        principal_id=principal_id,
        request=request,
        assertions=assertions,
        variables=frozen_variables,
    )
    # 授权匹配用的是原有的内部快照摘要，与下面这份对外标记是两个不同的值：前者是
    # 已签发授权的键，后者是绑定主体、带用途域的不透明证明。
    internal_snapshot_digest = debug_snapshot_digest(request, assertions)
    context = {
        "snapshot_fingerprint": binding.snapshot_fingerprint,
        "environment": {
            "id": str(environment.id),
            "name": environment.name,
            "kind": environment.kind,
            "base_url": base_url or environment.base_url,
        },
        "input_fingerprint": binding.input_fingerprint,
    }

    result = PreflightResult(
        ready=True,
        can_authorize=can_authorize,
        auth_required=auth_required,
        context=context,
    )

    try:
        resolved = resolve_pool(session, settings, environment)
        _request, target_origin = resolve_target(
            request, environment, resolved.guard, base_url=base_url
        )
    except RunRejected as error:
        result.ready = False
        result.issues.append(
            PreflightIssue(
                error.code,
                error.message,
                "configure_environment" if error.code in _ENVIRONMENT_ACTIONS else "edit_request",
            )
        )
        return result

    state, profile_id, issues, injection_slots = _inspect_credentials(
        session,
        environment=environment,
        principal_id=principal_id,
        target_origin=target_origin,
        auth_required=auth_required,
        # 授权匹配仍用原有的内部快照摘要：它是已签发授权的键，换成 HMAC 会让所有
        # 既有授权失配。对外返回的是上面那份绑定主体的不透明标记，两者互不替代。
        internal_snapshot_digest=internal_snapshot_digest,
        case_version_id=None,
        can_authorize=can_authorize,
    )
    result.auth_state = state
    result.profile_id = profile_id
    result.injection_slots = injection_slots
    result.requires_worker_verification = bool(injection_slots)
    if issues:
        result.ready = False
        result.issues.extend(issues)
    return result


def _inspect_credentials(
    session: Session,
    *,
    environment: Environment,
    principal_id: uuid.UUID,
    target_origin: str,
    auth_required: bool,
    internal_snapshot_digest: str,
    case_version_id: uuid.UUID | None,
    can_authorize: bool,
) -> tuple[str, uuid.UUID | None, list[PreflightIssue], list[dict]]:
    """检查环境身份与本次用途授权；返回（认证状态, 可返回的 profile_id, 问题）。

    只读元数据：秘密值、凭证集合里的秘密版本内容都不读取，也不解密。`profile_id`
    只在当前主体可以管理身份时返回——它是“就地授权”所需的坐标，对普通编辑者等于
    一份不该看到的凭证管理列表。
    """
    profiles = available_profiles(session, environment.id)
    if not profiles:
        if auth_required:
            return (
                "unavailable",
                None,
                [
                    PreflightIssue(
                        CREDENTIAL_REQUIRED,
                        "这份请求要求使用当前环境的登录态，但该环境还没有可用身份。",
                        _credential_action(can_authorize),
                    )
                ],
                [],
            )
        return "none", None, [], []

    if len(profiles) > 1:
        return (
            "ambiguous",
            None,
            [
                PreflightIssue(
                    CREDENTIAL_AMBIGUOUS,
                    f"该环境配置了 {len(profiles)} 份可用身份，无法确定本次使用哪一份。"
                    "请让身份管理员只保留一份可用身份。",
                    _credential_action(can_authorize),
                )
            ],
            [],
        )

    profile = profiles[0]
    exposed_profile_id = profile.id if can_authorize else None

    if profile.allowed_targets and target_origin not in set(profile.allowed_targets):
        return (
            "unavailable",
            exposed_profile_id,
            [
                PreflightIssue(
                    "credential_target_mismatch",
                    "当前身份的允许目标不包含本次目标，请让身份管理员核对允许目标配置。",
                    "manage_credentials",
                )
            ],
            [],
        )

    set_issue, required_slots = _inspect_credential_set(session, profile)
    if set_issue is not None:
        return "unavailable", exposed_profile_id, [set_issue], []

    slot_metadata = []
    for slot in sorted(required_slots):
        try:
            kind, name = parse_auth_slot(slot)
        except CredentialError:
            continue
        slot_metadata.append(
            {"kind": kind, "name": name, "status": "pending_worker_verification"}
        )

    try:
        check_usable_grant(
            session,
            profile,
            environment,
            principal_id=principal_id,
            case_version_id=case_version_id,
            debug_snapshot_hash=(internal_snapshot_digest if case_version_id is None else None),
            # 目标与槽位都取自本次真正要执行的配置：少了它们，预检会选中一条实际
            # 不允许本次目标或槽位的授权并报“可以发送”，与实际执行不一致。
            target_origin=target_origin,
            required_slots=required_slots,
        )
    except CredentialError as error:
        return (
            "needs_authorization",
            exposed_profile_id,
            [PreflightIssue(error.code, error.message, _credential_action(can_authorize))],
            slot_metadata,
        )
    return "ready", exposed_profile_id, [], slot_metadata


def inspect_auth_metadata(
    session: Session,
    *,
    environment: Environment,
    principal_id: uuid.UUID,
    target_origin: str,
    auth_required: bool,
    case_version_id: uuid.UUID | None,
    debug_snapshot_hash: str | None,
    can_authorize: bool,
) -> tuple[dict, list[PreflightIssue]]:
    """供固定版本/调试解析预览共用的只读认证元数据检查。"""
    state, _profile_id, issues, slots = _inspect_credentials(
        session,
        environment=environment,
        principal_id=principal_id,
        target_origin=target_origin,
        auth_required=auth_required,
        internal_snapshot_digest=debug_snapshot_hash or "",
        case_version_id=case_version_id,
        can_authorize=can_authorize,
    )
    return {
        "required": auth_required,
        "status": state,
        "injection_slots": slots,
        "requires_worker_verification": bool(slots),
    }, issues


def _inspect_credential_set(
    session: Session, profile
) -> tuple[PreflightIssue | None, set[str]]:
    """当前凭证集合是否可用；返回（问题, 本次会注入的槽位）。

    只读元数据，不触碰秘密内容。槽位列表用于按“真正要用的槽位”筛选授权：授权可以把
    范围收得比身份更窄，若预检不看槽位，就会选中一条实际不允许它的授权并报可发送。
    """
    if profile.current_set_id is None:
        return (
            PreflightIssue(
                "credential_unconfigured",
                "该身份尚未绑定凭证集合，请让身份管理员完成登录并更新凭证。",
                "manage_credentials",
            ),
            set(),
        )
    credential_set = session.get(CredentialSet, profile.current_set_id)
    if credential_set is None or credential_set.status != "active":
        return (
            PreflightIssue(
                "credential_unavailable",
                "当前凭证集合不可用，请让身份管理员更新凭证。",
                "manage_credentials",
            ),
            set(),
        )
    if credential_set.expires_at is not None and credential_set.expires_at <= datetime.now(UTC):
        return (
            PreflightIssue(
                "credential_expired",
                "当前凭证集合已过期，请让身份管理员重新登录并更新凭证。",
                "manage_credentials",
            ),
            set(),
        )
    return None, set_auth_slots(session, credential_set)


def _credential_action(can_authorize: bool) -> str:
    """有管理身份权限的人可以就地处理，其余成员只能请管理员。"""
    return "authorize" if can_authorize else "contact_admin"


__all__ = ["PreflightIssue", "PreflightResult", "inspect_auth_metadata", "preflight"]
