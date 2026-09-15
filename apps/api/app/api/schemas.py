"""API 请求／响应模型：显式字段、extra=forbid，拒绝未声明参数。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# —— 认证 ——


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class PrincipalOut(ApiModel):
    user_id: uuid.UUID
    username: str
    display_name: str
    is_admin: bool


class WorkspaceOut(ApiModel):
    id: uuid.UUID
    name: str
    role: str


class SessionOut(ApiModel):
    user: PrincipalOut
    workspaces: list[WorkspaceOut]


# —— 项目与环境 ——


class ProjectOut(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    key: str
    name: str
    status: str
    role: str
    pool_id: uuid.UUID | None = None


class ProjectCreate(ApiModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=200)


class EnvironmentOut(ApiModel):
    id: uuid.UUID
    name: str
    kind: str
    base_url: str
    pool_id: uuid.UUID | None
    variables: dict[str, Any]
    status: str


class EnvironmentCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["test", "production"] = "test"
    base_url: str = Field(min_length=1, max_length=500)
    variables: dict[str, Any] = Field(default_factory=dict)


class EnvironmentUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    variables: dict[str, Any] | None = None
    status: Literal["active", "archived"] | None = None


class VariableItem(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    value: dict[str, Any]


class VariablesOut(ApiModel):
    version: int
    variables: list[VariableItem]


class VariablesUpdate(ApiModel):
    variables: list[VariableItem]


# —— 执行池授权与目标白名单 ——


class RunnerPoolOut(ApiModel):
    """当前项目可用的执行池；`allowed_targets` 是出网目标白名单，不是秘密。"""

    id: uuid.UUID
    name: str
    status: str
    network_zone: str
    allowed_targets: list[str]
    grant_id: uuid.UUID
    grant_status: str
    granted_at: datetime
    environment_ids: list[uuid.UUID]


class RunnerPoolTargetsUpdate(ApiModel):
    """替换执行池允许的目标白名单；保存只改配置，不访问任何目标。"""

    allowed_targets: list[str] = Field(min_length=1, max_length=50)


class FolderOut(ApiModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    archived_at: datetime | None


class FolderCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None


class FolderUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None


# —— 用例 ——


class CaseOut(ApiModel):
    id: uuid.UUID
    folder_id: uuid.UUID | None
    name: str
    request: dict[str, Any]
    assertions: list[dict[str, Any]]
    rev: int
    status: str
    latest_version: int | None
    updated_at: datetime
    # 当前草稿内容的快照摘要，与发布会固化的版本使用同一算法。前端据此判断某次运行
    # 执行的那份快照是否仍与屏幕上的用例一致：只按断言标识回填结果，会把上一轮的
    # 通过贴在已经改过的条件旁边，那是当前配置支持不了的结论。
    snapshot_hash: str


class CaseSummaryOut(ApiModel):
    id: uuid.UUID
    folder_id: uuid.UUID | None
    name: str
    method: str
    status: str
    rev: int
    latest_version: int | None


class CaseCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    folder_id: uuid.UUID | None = None
    request: dict[str, Any]
    assertions: list[dict[str, Any]] = Field(default_factory=list)


class CaseUpdate(ApiModel):
    """用例草稿的部分更新。

    `folder_id` 的三态含义必须分清，否则“移到未分组”会静默失效：

    - **字段缺席**：本次不改所属目录，保留原值（即使原值指向一个已归档的目录）；
    - **显式 null**：移到未分组；
    - **给定 id**：移到该目录，且该目录必须属于同一个项目（跨项目按“目录不存在”拒绝）。

    “缺席”与“显式 null”都序列化成 `None`，只能靠 pydantic 的 `model_fields_set`
    区分，调用点因此不能写成 `if payload.folder_id is not None`。
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    folder_id: uuid.UUID | None = None
    request: dict[str, Any] | None = None
    assertions: list[dict[str, Any]] | None = None


class CaseVersionOut(ApiModel):
    id: uuid.UUID
    case_id: uuid.UUID
    version: int
    schema_version: int
    side_effect: str
    snapshot_hash: str
    created_by: uuid.UUID
    created_at: datetime


class CasePublish(ApiModel):
    side_effect: Literal["read", "write", "unknown"] = "unknown"
    # 发布固化的草稿修订号。缺少它时，发布无法证明自己固化的是用户看到的那一版。
    draft_rev: int = Field(ge=1)


# —— cURL 导入与断言试算 ——


class CurlPreviewRequest(ApiModel):
    text: str = Field(min_length=1, max_length=20000)


class CurlPreviewOut(ApiModel):
    draft: dict[str, Any]
    sendable: bool
    warnings: list[str]
    unsupported: list[str]
    auth_hint: dict[str, Any] | None


class AssertionTypeOut(ApiModel):
    id: str
    label: str
    group: str
    applies_to: list[str]
    params_schema: dict[str, Any]
    summary: str
    operator_version: int


class AssertionPreviewRequest(ApiModel):
    type: str
    parameters: dict[str, Any]
    value: dict[str, Any] | None = None
    found: bool = True
    compare_as: str | None = None


class AssertionPreviewOut(ApiModel):
    status: str
    reason_code: str | None
    message: str
    expected: Any
    actual: Any


class FieldTreeRequest(ApiModel):
    """由后端无损展开字段树；前端不自行 JSON.parse，避免长整数失真。"""

    text: str = Field(min_length=1, max_length=5 * 1024 * 1024)


class FieldNodeOut(ApiModel):
    label: str
    type: str
    text: str
    selector: list[dict[str, Any]]
    children: list[FieldNodeOut] = Field(default_factory=list)
    truncated: str | None = None


class FieldTreeOut(ApiModel):
    root: FieldNodeOut
    node_count: int


# —— 运行与报告 ——


class DebugSnapshot(ApiModel):
    """临时不可变调试快照：请求 + 断言，不落用例，也不产生版本。"""

    request: dict[str, Any]
    assertions: list[dict[str, Any]] = Field(default_factory=list)


class DebugSnapshotDigestOut(ApiModel):
    """调试快照摘要：供身份管理员据此签发绑定该快照的一次性授权。"""

    hash: str


class RunCreate(ApiModel):
    environment_id: uuid.UUID
    case_version_id: uuid.UUID | None = None
    debug_snapshot: DebugSnapshot | None = None


class RunOut(ApiModel):
    id: uuid.UUID
    target_type: str
    case_version_id: uuid.UUID | None
    environment_id: uuid.UUID
    state: str
    outcome: str | None
    reason_category: str | None
    pool_id: uuid.UUID | None
    created_at: datetime


class RunStepOut(ApiModel):
    step_key: str
    attempt_no: int
    state: str
    outcome: str | None
    elapsed_ms: int | None
    error_code: str | None


class AssertionResultOut(ApiModel):
    assertion_id: str
    type: str
    phase: str
    target: dict[str, Any]
    status: str
    expected: Any
    actual: Any
    reason_code: str | None
    elapsed_ms: int | None


class RunSourceEnvironment(ApiModel):
    """运行来源环境：来自冻结快照的脱敏描述，不含变量值。"""

    id: uuid.UUID
    name: str
    kind: str
    base_url: str


class RunContextOut(ApiModel):
    """运行来源：服务端以受保护主密钥生成的**不透明**关联标记。

    两个标记是 HMAC-SHA256（独立用途域、绑定 workspace／project／发起主体），不是
    内容摘要：报告会遮蔽敏感字段的值，若这里换成裸摘要，任何能读到报告的人都可以拿
    低熵候选离线撞出“是不是这一条”，跨主体比对也会因为同一份内容得到同一摘要而成立。
    标记不含认证注入值或密文，也不是授权凭证——它只用来把结果和输入关联起来。
    """

    snapshot_fingerprint: str
    environment: RunSourceEnvironment
    input_fingerprint: str


class RunReportOut(ApiModel):
    run: RunOut
    steps: list[RunStepOut]
    assertions: list[AssertionResultOut]
    request: dict[str, Any] | None
    response: dict[str, Any] | None
    # 可证明的运行来源。缺席与 null 同义：这条记录给不出“按哪份配置产生”的结论，
    # 仍可查看原始证据，但不能把它当作当前草稿已通过的证明。
    context: RunContextOut | None = None


# —— 发送前预检 ——
#
# 只报告当前主体此刻的准入状态：不解密秘密、不消费授权、不创建运行、不访问目标
# HTTP。它不是执行凭证，真正发送前仍按权威规则重新检查一次。

PreflightAction = Literal[
    "edit_request",
    "select_environment",
    "manage_credentials",
    "authorize",
    "contact_admin",
    "configure_environment",
]

PreflightAuthState = Literal["none", "ready", "needs_authorization", "unavailable", "ambiguous"]


class DebugPreflightRequest(ApiModel):
    environment_id: uuid.UUID
    debug_snapshot: DebugSnapshot


class PreflightIssueOut(ApiModel):
    """一条可操作的准入问题：稳定错误码、中文说明与建议动作。"""

    code: str
    message: str
    action: PreflightAction


class PreflightAuthOut(ApiModel):
    """当前环境的认证状态；profile_id 只对可管理身份者返回。"""

    required: bool
    state: PreflightAuthState
    profile_id: uuid.UUID | None = None


class DebugPreflightOut(ApiModel):
    ready: bool
    issues: list[PreflightIssueOut] = Field(default_factory=list)
    can_authorize: bool
    auth: PreflightAuthOut
    context: RunContextOut | None = None


# —— 身份凭证 ——
#
# 秘密只进不出：请求可以带明文值，响应永远不返回明文，也不返回密文，
# 只返回名称、版本号与可用状态，避免值经任何一条读接口回流。


class SecretCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=8192)


class SecretOut(ApiModel):
    # 绑定凭证集合需要秘密版本 id；只给版本号会让新建的秘密无法被引用。
    # UUID 本身不是敏感值，明文与密文都不出现在这里。
    id: uuid.UUID
    name: str
    kind: str
    latest_version: int
    latest_version_id: uuid.UUID


class SecretRotate(ApiModel):
    value: str = Field(min_length=1, max_length=8192)


class SecretVersionOut(ApiModel):
    secret_id: uuid.UUID
    version_id: uuid.UUID
    version: int


class CredentialProfileCreate(ApiModel):
    environment_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_auth_slots: list[str] = Field(default_factory=list)
    # 认证位置／失效判据的首个不可变版本。与身份同事务写入：分成两步会让“身份已建、
    # 但配置没存”变成一个看不出差别的中间状态，而配置缺失的身份无法被正确使用。
    config: dict[str, Any] | None = None


class CredentialProfileVersionCreate(ApiModel):
    """身份配置的不可变版本；首次填写认证位置与失效判据。"""

    config: dict[str, Any]


class CredentialSetActivate(ApiModel):
    """按槽位绑定秘密版本，原子切换当前集合。

    提交的是**完整集合**：服务端每次都会新建集合并整份激活，只带一个槽位等于把
    其他槽位一并删掉。`expected_epoch` 是表单读取时的 epoch，用于挡住旧表单整份
    覆盖已被别人换过的新集合。
    """

    slots: dict[str, uuid.UUID]
    expires_at: datetime | None = None
    expected_epoch: int | None = None


class CredentialSetSlotOut(ApiModel):
    """当前集合里的一个槽位绑定；只有位置与秘密名称／版本号，没有秘密值。"""

    auth_slot: str
    secret_id: uuid.UUID
    secret_name: str
    secret_version_id: uuid.UUID
    secret_version: int


class CredentialSetOut(ApiModel):
    """当前凭证集合的绑定元数据；不返回明文，也不返回密文。"""

    profile_id: uuid.UUID
    set_id: uuid.UUID | None
    epoch: int
    status: str | None
    expires_at: datetime | None
    slots: list[CredentialSetSlotOut]


class WorkspaceMemberOut(ApiModel):
    """工作空间成员的可见元数据：用于按姓名选择被授权主体，不含凭据。"""

    user_id: uuid.UUID
    username: str
    display_name: str
    role: str


class CredentialProfileOut(ApiModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    name: str
    provider: str
    status: str
    current_epoch: int
    current_set_id: uuid.UUID | None
    profile_version_id: uuid.UUID | None
    allowed_targets: list[str]
    allowed_auth_slots: list[str]
    slot_count: int


class CredentialProfileVersionOut(ApiModel):
    id: uuid.UUID
    profile_id: uuid.UUID
    version: int
    config: dict[str, Any]


class CredentialGrantCreate(ApiModel):
    profile_id: uuid.UUID
    grant_type: Literal["case_version", "debug_snapshot"]
    principal_id: uuid.UUID
    case_version_id: uuid.UUID | None = None
    debug_snapshot_hash: str | None = Field(default=None, max_length=64)
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_auth_slots: list[str] = Field(default_factory=list)
    allowed_inputs: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class CredentialGrantOut(ApiModel):
    id: uuid.UUID
    profile_id: uuid.UUID
    environment_id: uuid.UUID
    grant_type: str
    principal_id: uuid.UUID
    case_version_id: uuid.UUID | None
    debug_snapshot_hash: str | None
    allowed_targets: list[str]
    allowed_auth_slots: list[str]
    status: str
    expires_at: datetime | None
    used_at: datetime | None
