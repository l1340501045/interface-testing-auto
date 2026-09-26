"""执行内核：从领取到终态提交的完整单接口执行。

手工调试、单用例与后续场景共用本内核。流程固定为：固定快照与身份版本 →
解析变量与认证注入 → 入参断言（失败不发送）→ 持久化发送意图 → 受控 HTTP →
响应断言 → 脱敏证据与 fencing 保护的终态提交。

租约、取消与截止时间在发送前重新校验；网络中不持有数据库长事务；终态被条件
更新保护，旧 fencing token 不能覆写当前状态；已发出写请求但结果不明时记为
interrupted，不自动重放。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db import bind_tenant
from ..kernel.assertion_catalog import get_type, phase_of
from ..kernel.assertion_inputs import (
    SourceRoots,
    json_root,
    normalize_for_compare,
    pairs_root,
    parse_cookie_root,
    paths_containing,
)
from ..kernel.assertion_spec import AssertionSpecError, validate_assertions
from ..kernel.assertions import (
    AssertionConfigError,
    AssertionContext,
    AssertionOutcome,
    AssertionPolicyError,
    evaluate,
)
from ..kernel.http_client import PinnedAddressMissing, build_client
from ..kernel.lossless_json import LosslessJSONError, NumberNode, dumps, loads
from ..kernel.redaction import (
    contains_secret,
    redact_pairs,
    redact_text,
    redact_value,
    value_contains_secret,
)
from ..kernel.request_spec import (
    PreparedRequest,
    RequestSpecError,
    prepare,
    validate_request,
)
from ..kernel.target_policy import TargetGuard, TargetPolicyError
from ..kernel.variables import VariableResolutionError, build_resolver
from ..models import (
    AssertionResult,
    CaseVersion,
    Environment,
    Run,
    RunnerPool,
    RunnerPoolProjectGrant,
    RunStepAttempt,
    User,
)
from .credentials import (
    CredentialError,
    LeaseGuard,
    recheck_grant_authority,
    resolve_injection,
)
from .debug_context import stamp_guard_semantics
from .permissions import can
from .variable_inputs import frozen_snapshot_digest

_MAX_STORED_BODY_BYTES = 64 * 1024
_MAX_PARSED_BODY_BYTES = 5 * 1024 * 1024

# “本次领取仍然有效”的唯一判据，绑定了工作项身份、持有者、fencing token 与**当前
# 尚未过期**四个条件。它同时用于发送前校验、终态提交与 worker 心跳续租：这四处过去
# 各写一遍，漏掉任一字段就出现一条缝隙——心跳尤其明显，只看身份不看有效期的话，
# 进程暂停到租约过期后，一次心跳就能把旧租约续活，让已经不该再干活的执行者继续
# 通过其他检查。条件集中在这里，新增路径直接复用，不再各写一份。
LEASE_HELD_CONDITION = (
    "state = 'leased' AND leased_by = :worker AND fencing_token = :token "
    "AND lease_until > now()"
)


@dataclass(frozen=True)
class JobClaim:
    """一次原子领取的结果坐标；其余读写仍在调用方的租户上下文里按 RLS 执行。"""

    job_id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    pool_id: uuid.UUID
    fencing_token: int
    attempt: int


@dataclass
class AssertionRecord:
    """一条待落库的断言结果。"""

    assertion: dict
    phase: str
    outcome: AssertionOutcome
    elapsed_ms: int = 0


@dataclass
class StepEvidence:
    """一次尝试的可入库证据（均已脱敏）。"""

    request: dict = field(default_factory=dict)
    response: dict | None = None
    # 本轮环境冻结与必需认证保护是否**真的被这次尝试执行过**。
    #
    # 报告里的来源证明只能由“确实跑过保护”的尝试给出。同一段代码在恢复分支、截止时间
    # 分支、环境缺失分支上也会写尝试记录，但那些路径根本没有走到保护逻辑——给它们盖章
    # 会让一条“旧执行器留下未完成发送意图、新执行器直接判 interrupted、请求从未受本轮
    # 约束”的记录看起来像受过保护，报告于是给出一个它并没有依据的结论。
    guards_evaluated: bool = False


@dataclass(frozen=True)
class StoredBody:
    """响应正文的入库形态。

    三态而不是两态：`text` 为 None 表示**明确省略**——格式或完整性无法确认时整段
    不写，而不是写一段“看起来没问题”的片段。省略与“空正文”必须分得开，否则报告
    里读不出这次到底是目标没返回内容，还是我们拒绝保存它。
    """

    text: str | None
    truncated: bool = False
    omitted_reason: str | None = None
    # 这次按哪种格式处理的（json／text）；省略时为 None。它是“凭什么按纯文本放行”
    # 的上下文，用来说明判据，而不是装饰字段。
    format: str | None = None


class SendFailure(Exception):
    """发送失败，携带稳定错误码、失败分类与“是否可能已产生副作用”。"""

    def __init__(self, code: str, message: str, category: str, ambiguous: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.ambiguous = ambiguous


def claim_job(
    session: Session, worker_id: str, pool_ids: list[uuid.UUID], lease_seconds: int
) -> JobClaim | None:
    """通过单用途 SECURITY DEFINER 函数原子领取一个工作项。

    函数属主是 BYPASSRLS 的 app_job_broker，只做锁定与递增 fencing token，
    不返回租户数据；调用方随后自行建立租户上下文，其余读写仍受 RLS 约束。
    """
    row = session.execute(
        text(
            "SELECT job_id, run_id, workspace_id, project_id, pool_id, fencing_token, attempt "
            "FROM app.claim_job(:worker, :pools, :lease)"
        ),
        {"worker": worker_id, "pools": pool_ids, "lease": lease_seconds},
    ).first()
    if row is None:
        return None
    return JobClaim(
        job_id=row.job_id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        pool_id=row.pool_id,
        fencing_token=row.fencing_token,
        attempt=row.attempt,
    )


def _set_tenant(
    session: Session, workspace_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> None:
    """绑定租户；执行内核会多次提交，上下文由会话状态在每个事务自动重放。

    成员表与工作空间表的策略按 `app.user_id` 过滤，复核主体权限时必须把当前身份
    一并带入上下文，否则只会查到零行，把“查不到”误判成“已撤权”。
    """
    bind_tenant(session, workspace_id, user_id)


def _jsonable(value: Any) -> Any:
    """把求值结果转为可安全写入 JSONB 的形式；数字保留十进制文本。"""
    if isinstance(value, NumberNode):
        return value.text
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    return str(value)


def describe_selector(selector: list[dict]) -> str:
    """把定位路径渲染为可读标签，便于报告展示；显示名与定位表达式分开保存。"""
    label = ""
    for step in selector:
        kind = step.get("kind")
        if kind == "key":
            label = f"{label}.{step.get('key')}" if label else str(step.get("key"))
        elif kind == "index":
            label = f"{label}[{step.get('index')}]"
        elif kind == "repeat_key":
            label = f"{label}.{step.get('key')}#{step.get('occurrence', 0)}"
        elif kind == "row":
            row = f"row({step.get('row_id')})"
            label = f"{label}.{row}" if label else row
    return label


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _request_evidence(evidence: StepEvidence) -> dict:
    """入库前的请求证据：只有**确实执行过本轮保护**的尝试才盖语义标记。

    标记是报告给出“这份结果按哪份配置产生”的唯一依据。恢复分支、截止时间分支、
    环境缺失分支同样会写尝试记录，但它们没有走到环境冻结与必需认证的判定，因此
    一律不盖章——旧执行器留下的未完成发送意图被新执行器判为 interrupted 时，请求
    从未受本轮约束，报告必须返回 context=null 而不是给出一个没有依据的结论。
    """
    if not evidence.guards_evaluated:
        return evidence.request
    return stamp_guard_semantics(evidence.request)


def _build_request_roots(prepared: PreparedRequest, request: dict) -> SourceRoots:
    roots = SourceRoots()
    # 路径与查询参数、请求头一样取**实际发出的那一份**：请求定义里的 `{{变量}}`
    # 到了这里已经解析成真实值（`/{{operation}}` 发出去的是 `/echo`）。用准备之前的
    # 模板会让一条核对路径的输入断言对着一个从未出现在线上的字符串求值：真的发对了
    # 反而判失败，而它恰恰是最该拦住错误的那道门。
    #
    # 取的是**完整最终路径**（环境基础路径 + 用例路径）：环境写成
    # `http://echo:8080/api/v1` 时线上请求的是 `/api/v1/echo`，只给 `/echo` 同样
    # 是在核对一个没发出去的地址。`final_path` 是未编码原值，与 `request.query.*`
    # 的取值口径一致；`url_path` 是同一路径在 URL 里的编码形态。
    roots.add_direct("request.path", prepared.final_path or "/")
    roots.add_direct("request.text", prepared.body_text())
    # 查询参数与请求头一样取**实际发出的那一份**：变量已解析、认证注入已并入。
    # 用请求定义里的原始模板会核对一个没发出去的虚构值。
    roots.add_tree(
        "request.query",
        pairs_root(prepared.query_as_pairs()),
        row_indices=prepared.query_row_indices,
    )
    roots.add_tree(
        "request.header",
        pairs_root(prepared.headers_as_pairs()),
        row_indices=prepared.header_row_indices,
    )
    if request.get("body_type") == "json":
        tree = json_root(prepared.body_text())
        if tree is not None:
            roots.add_tree("request.body", tree)
    return roots


def _build_response_roots(
    prepared: PreparedRequest,
    request: dict,
    response: httpx.Response,
    body_text: str,
    elapsed_ms: int,
    stored: StoredBody,
) -> SourceRoots:
    roots = _build_request_roots(prepared, request)
    # 状态码与耗时同样属于数值，按统一无损表示返回，避免报告里同一个
    # “数字”有时是文本、有时是整数，比较和展示都出现分歧。
    roots.add_direct("response.status", NumberNode(str(response.status_code)))
    roots.add_direct("response.elapsed", NumberNode(str(elapsed_ms)))
    roots.add_tree(
        "response.header",
        pairs_root([{"name": key, "value": value} for key, value in response.headers.multi_items()]),
    )
    roots.add_tree(
        "response.cookie", parse_cookie_root(response.headers.get_list("set-cookie"))
    )
    if stored.omitted_reason is not None:
        # 正文已被明确省略：正文来源必须一起省略。只把正文从报告里拿掉、却仍让断言
        # 读到原文，等于用一个“报告里没有、结果里能猜”的通道把凭据放了出去——报告
        # 遮了、断言真假却仍能逐位试出秘密，比不遮更糟。状态码与响应头不受影响：
        # 它们是另一批来源，且本身不含正文内容。
        reason = stored.omitted_reason
        roots.add_omitted("response.text", reason)
        roots.add_omitted("response.body", reason)
        return roots
    roots.add_direct("response.text", body_text)
    if len(body_text.encode("utf-8")) <= _MAX_PARSED_BODY_BYTES:
        tree = json_root(body_text)
        if tree is not None:
            roots.add_tree("response.body", tree)
    return roots


def _assertion_path(item: dict) -> tuple:
    """把断言的取值来源与定位步骤还原为内核保护的坐标。

    内核按“来源 + 定位步骤”判断是否触到受保护值，执行器若一律传空路径，这条保护
    实际永远不会生效。只需来源名与每一步的稳定标识（键名／下标）；重复键用键名
    而不是序号，这样同名槽位的每一次出现都落在保护范围内。
    """
    path: list[Any] = [item["target_source"]]
    for step in item.get("selector") or []:
        if not isinstance(step, dict):
            raise AssertionConfigError("定位步骤必须是对象")
        kind = step.get("kind")
        if kind in ("key", "repeat_key"):
            path.append(step.get("key"))
        elif kind == "index":
            path.append(step.get("index"))
        elif kind == "row":
            path.append(step.get("row_id"))
        else:
            raise AssertionConfigError(f"未知定位步骤：{kind!r}")
    return tuple(path)


def _evaluate_assertions(
    assertions: list[dict], phase: str, roots: SourceRoots, ctx: AssertionContext
) -> list[AssertionRecord]:
    records: list[AssertionRecord] = []
    for item in assertions:
        if not item.get("enabled", True):
            continue
        if phase_of(item["target_source"]) != phase:
            continue
        started = time.perf_counter()
        omitted = roots.omitted_reason(item["target_source"])
        if omitted is not None:
            # 来源被**明确省略**：正文的格式或完整性无法确认，原文没有入库。这里既
            # 不取原文做猜测性求值（那会把可能带着凭据片段的文本读出来比对），也不
            # 当作“来源不存在”而静默跳过——本条断言无法求值，记为错误。
            records.append(
                AssertionRecord(
                    item,
                    phase,
                    AssertionOutcome(
                        "error",
                        "source_omitted",
                        message=(
                            f"{item['target_source']} 的内容因格式或完整性无法确认已省略"
                            f"（{omitted}），本条断言无法求值"
                        ),
                    ),
                    _elapsed_ms(started),
                )
            )
            continue
        if not roots.has(item["target_source"]):
            # 没有该阶段的输入时整体 skipped，不当作通过。
            records.append(
                AssertionRecord(
                    item,
                    phase,
                    AssertionOutcome(
                        "skipped", "source_unavailable", message="该阶段的取值来源不可用，未执行"
                    ),
                )
            )
            continue
        found, value = roots.extract(item["target_source"], item.get("selector") or [])
        value = normalize_for_compare(value, item.get("compare_as"))
        # 敏感性判定用**真实结构坐标**（字段名／下标），与敏感标记同一坐标系；
        # 解析不出来（字段缺失）时退回按名字的别名坐标，宁可多拦不可漏拦。
        coordinate = roots.coordinate(item["target_source"], item.get("selector") or [])
        try:
            outcome = evaluate(
                item["type"],
                get_type(item["type"]).operator_version,
                value,
                found,
                item.get("parameters") or {},
                path=coordinate if coordinate is not None else _assertion_path(item),
                ctx=ctx,
            )
        except AssertionPolicyError as error:
            outcome = AssertionOutcome("error", "policy_rejected", message=str(error))
        except AssertionConfigError as error:
            outcome = AssertionOutcome("error", "config_invalid", message=str(error))
        records.append(AssertionRecord(item, phase, outcome, _elapsed_ms(started)))
    return records


def _blocking_failure(records: list[AssertionRecord]) -> AssertionRecord | None:
    """阻塞性失败：严重级别为 error 且结果 failed／error 的记录。"""
    for record in records:
        if record.assertion.get("severity", "error") != "error":
            continue
        if record.outcome.status in ("failed", "error"):
            return record
    return None


def _required_unchecked(records: list[AssertionRecord]) -> bool:
    """是否存在**必需**（严重级别 error）却根本没被执行的条件。

    多条件默认全部满足：一条必需条件因为来源不可用而跳过时，另一条通过**并不等于**
    这一条也满足。只按“有没有至少一条响应条件被执行过”判定，就会让一次没做完的检查
    整体显示通过——同一行上两条同级条件，一条 passed、一条 skipped，结果却是 passed。

    未校验既不是满足也不是失败：它不能算 passed，也不该按断言失败报 failed。严重级别
    warning 的条件不阻塞，跳过它不影响终态。显式禁用的条件不会进 `records`（求值前就
    被过滤），因此也不会把检查拖成未校验。
    """
    return any(
        record.outcome.status == "skipped"
        and record.assertion.get("severity", "error") == "error"
        for record in records
    )


def _summarize(
    pre_records: list[AssertionRecord], post_records: list[AssertionRecord]
) -> tuple[str, str | None, str | None]:
    """汇总运行终态：配置错误优先，其次阻塞失败，最后区分是否真正校验过。"""
    executed_post = [item for item in post_records if item.outcome.status != "skipped"]
    for record in pre_records + post_records:
        if record.outcome.status == "error":
            return "error", "configuration", record.outcome.reason_code or "assertion_error"
    blocking = _blocking_failure(post_records) or _blocking_failure(pre_records)
    if blocking is not None:
        return "failed", "assertion", blocking.outcome.reason_code
    if _required_unchecked(pre_records + post_records):
        # 有必需条件没跑完（来源不可用而跳过）：响应并没有被完整校验过，不能因为
        # “另有一条通过了”就算接口健康。原因写进终态，报告读得到为什么不是 passed。
        return "completed_unchecked", "assertion", "assertion_not_evaluated"
    if not executed_post:
        # 只有入参断言或没有断言：不能把“响应未校验”当作接口健康通过。
        return "completed_unchecked", None, None
    return "passed", None, None


def _recheck_current_facts(session: Session, run: Run, environment: Environment) -> None:
    """发送前按当前事实复核发起人的执行权限与项目／环境可用状态。

    运行会在队列里停留任意长时间。创建运行时校验过一次，并不代表发送时依然成立：
    账号可能在期间被停用或降为查看者，成员关系可能被移除，项目或环境可能被归档。
    这些都是“用户已经明确收回许可”的动作，如果只在创建时检查，撤回就只能拦住
    之后新建的运行，对已经排队的运行完全无效，而后果是真实 HTTP 照样发出。
    """
    user = session.get(User, run.created_by)
    if user is None or user.status != "active":
        raise SendFailure("principal_inactive", "发起账号已停用，已阻止执行。", "policy", False)

    role = session.execute(
        text(
            "SELECT role FROM app.workspace_memberships "
            "WHERE user_id = :user AND workspace_id = :ws"
        ),
        {"user": str(run.created_by), "ws": str(run.workspace_id)},
    ).scalar()
    if role is None:
        raise SendFailure(
            "membership_revoked", "发起人已不是该工作空间成员，已阻止执行。", "policy", False
        )
    if not can(role, "execute"):
        raise SendFailure(
            "principal_not_authorized", "发起人已无权执行，已阻止执行。", "policy", False
        )

    project_status = session.execute(
        text("SELECT status FROM app.projects WHERE id = :pid AND workspace_id = :ws"),
        {"pid": str(run.project_id), "ws": str(run.workspace_id)},
    ).scalar()
    if project_status != "active":
        raise SendFailure("project_archived", "项目已归档，已阻止执行。", "policy", False)

    if environment.status != "active":
        raise SendFailure("environment_archived", "环境已归档，已阻止执行。", "policy", False)


def _recheck_pool_authority(
    session: Session, run: Run, environment: Environment, settings: Settings
) -> TargetGuard:
    """发送前按**当前**数据库状态重算执行池授权，不沿用创建运行时的快照。

    创建运行时校验一次不足以支撑“旧环境快照不能越过已撤销授权”：工作项在入队之后、
    被领取之前可能已经撤权，而运行快照会把当时通过的结论一直保留下去。撤权只拦住
    “之后新建的运行”，对已经在队列里的运行无效——撤权本身就成了假动作。这里以当前
    授权为准，并把目标白名单也换成执行池当前配置，避免快照里的旧白名单继续放行。

    失败一律走 `SendFailure`，与其它策略拒绝同一语义：不发请求、不产生副作用、
    按策略类终态落库。
    """
    pool_id = run.pool_id
    if pool_id is None:
        raise SendFailure("pool_not_granted", "本次运行未绑定执行池，已阻止执行。", "policy", False)

    pool = session.get(RunnerPool, pool_id)
    if pool is None or pool.status != "active":
        raise SendFailure("pool_unavailable", "执行池已不可用，已阻止执行。", "policy", False)

    if environment.pool_id is not None and environment.pool_id != pool_id:
        raise SendFailure(
            "pool_rebound", "环境已改绑到其他执行池，本次运行不再执行。", "policy", False
        )

    granted = session.scalar(
        select(RunnerPoolProjectGrant.id)
        .where(
            RunnerPoolProjectGrant.pool_id == pool_id,
            RunnerPoolProjectGrant.project_id == run.project_id,
            RunnerPoolProjectGrant.status == "active",
        )
        .limit(1)
    )
    if granted is None:
        raise SendFailure(
            "pool_not_granted", "该执行池已撤销对本项目的授权，已阻止执行。", "policy", False
        )

    allowed = list(pool.allowed_targets or [])
    if not allowed:
        allowed = [item.strip() for item in settings.controlled_targets.split(",") if item.strip()]
    try:
        return settings.target_guard(allowed)
    except TargetPolicyError as error:
        raise SendFailure("pool_config_invalid", str(error), "policy", False) from error


def _recheck_frozen_environment(run: Run, environment: Environment, snapshot: dict) -> None:
    """冻结的环境配置与当前配置不一致时拒绝发送。

    创建运行时把环境 id、类型与地址冻进了快照，但请求构造一直用的是**当前**环境：
    排队期间改一次 base_url，请求就会发到另一个主机／基础路径，而报告里的来源仍然
    是入队时那个环境。用户以为自己在调试 A，实际上打到了 B，且没有任何提示。

    最保守的处理是差异拒绝：请求确实没有发出，用户确认新配置后重新发送即可。按旧
    快照构造、用当前权限放行这类折中会让“策略检查的目标”与“实际发送的目标”再次
    分家，正是这里要消除的那个问题。
    """
    frozen = snapshot.get("environment")
    if not isinstance(frozen, dict) or not frozen:
        # 早期记录可能没有冻结环境：保持既有行为，不因缺少字段而新增拒绝。
        return
    if str(frozen.get("id") or "") != str(environment.id):
        raise SendFailure(
            "environment_changed",
            "本次运行绑定的环境已变更，已阻止执行。请重新选择环境后再发送。",
            "configuration",
            False,
        )
    fields = {
        "kind": (frozen.get("kind"), environment.kind),
        "base_url": (frozen.get("base_url"), environment.base_url),
    }
    drifted = [
        name
        for name, (frozen_value, current_value) in fields.items()
        if str(frozen_value or "").rstrip("/") != str(current_value or "").rstrip("/")
    ]
    if drifted:
        raise SendFailure(
            "environment_changed",
            "环境配置在本运行入队之后被改动（类型或地址已不同），本次请求不会发往新"
            "目标。请确认新配置后重新发送。",
            "configuration",
            False,
        )


def _authorized_target(guard: TargetGuard, environment: Environment, request: dict):
    """发送前重新校验环境类型与目标来源。

    环境可能在入队之后被改成生产或被改到白名单之外，创建运行时的那次校验已经
    过期；发送前必须以当前环境状态重新判定，否则“入队时是测试环境”会一路
    放行到真实请求。
    """
    try:
        guard.check_environment(environment.kind)
    except TargetPolicyError as error:
        raise SendFailure("target_not_allowed", str(error), "policy", False) from error

    base = environment.base_url.rstrip("/")
    try:
        return guard.authorize_url(f"{base}{request.get('path', '/')}")
    except TargetPolicyError as error:
        raise SendFailure("target_not_allowed", str(error), "policy", False) from error


# 省略原因：正文的格式／完整性无法确认。写进证据后，报告读得到“为什么这段没有正文”。
OMITTED_UNPARSABLE_JSON = "unparsable_json_body"
# 省略原因：结构化脱敏后回写，正文里**仍然**能按既定口径读出凭据。
OMITTED_INCOMPLETE_REDACTION = "incomplete_redaction"


def _declares_json(content_type: str | None) -> bool:
    """目标是否**声明**这段正文是 JSON。

    只看媒体类型，忽略参数（`; charset=utf-8`）。`application/json`、`text/json`
    与 `application/vnd.api+json` 这类 `+json` 后缀都算声明。
    """
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    if "/" not in media:
        return False
    subtype = media.split("/", 1)[1]
    return subtype == "json" or subtype.endswith("+json")


def _looks_like_json_document(text: str) -> bool:
    """正文是否以 JSON 文档的起始符开头（`{`／`[`）。

    这是不看声明的第二条判据：目标把 `Content-Type` 写成 `text/plain` 时，
    `{"token":"abc` 这样的残缺片段仍然是**没处理完的 JSON**，不能按纯文本放行。
    只认起始符、不猜内容——真正的纯文本（`C:\\temp`、`前缀 abc 后缀`）不受影响。
    """
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in ("{", "[")


def _truncate_stored(text: str) -> tuple[str, bool]:
    """按入库上限截断（切到的只是掩码：调用方必须先脱敏）。"""
    raw = text.encode("utf-8")
    if len(raw) <= _MAX_STORED_BODY_BYTES:
        return text, False
    return raw[:_MAX_STORED_BODY_BYTES].decode("utf-8", errors="replace"), True


def _expects_json_body(assertions: list[dict]) -> bool:
    """用例是否对**解析后的正文**（`response.body`）提了条件。

    把检查配在 `response.body` 上，就是在声明“这份正文应当是 JSON”。少了这条，格式
    上下文只有目标自己的 `Content-Type`：类型头写成 `text/plain` 或者干脆不带时，残缺
    的 JSON 字符串根（`"abc`，凭据 `abcdef` 只剩前三字符）既不像 `{`／`[` 开头、也没被
    声明成 JSON，就被当成纯文本放行——`response.text` 照常求值，而配置在解析后正文上的
    那条条件因为来源不存在被跳过。一次没做完的检查于是看起来像是通过了。

    只看**启用中**的条件：显式禁用的条件不表达任何预期，不能因此改变正文的格式判定。
    这里只补“格式上下文”，不扩大字符串扫描范围——扫描的仍是被判定为 JSON 的那段正文。
    """
    return any(
        item.get("enabled", True) and item.get("target_source") == "response.body"
        for item in assertions
    )


def _stored_body(
    body_text: str, secrets: list[str], *, json_expected: bool = False
) -> StoredBody:
    """入库正文：**先脱敏，再截断**；无法完整处理的正文**明确省略**。

    截断顺序反了会留下秘密片段：截断按字节切，秘密横跨 64 KiB 边界时后半段被切掉，
    替换就再也匹配不到完整秘密，前半段原样留在报告里。脱敏在前则截断切到的只是
    掩码，切在哪个字节都不再含秘密。

    省略的判据是“能不能**完整**处理”，不是“有没有命中完整秘密”。残缺的 JSON 里
    凭据可能只剩前半截（正文止于 `{"token":"abc`，凭据是 `abcdef`）：完整秘密查不到
    不等于这段文本安全——它是一份**没拿全**的正文，后面缺着的内容谁也无法证明不含
    凭据。所以只要格式（目标声明 JSON，或**用例对解析后的正文提了条件**）或形态
    （以 `{`／`[` 开头）任一指向 JSON 而解析失败，整段省略，不走“按原文替换”的出口。

    能无损解析的走结构判定与遮蔽：转义写法（`\\u0061bc`）、对象键与 JSON 数字上的
    秘密都在这里覆盖（`redact_value` / `value_contains_secret`）。解析失败且两条判据
    都不指向 JSON 的，才算**确实**是纯文本，走文本出口——它按 JSON 转义解码后判断与
    遮蔽，所以残缺片段里的转义写法同样盖得住。

    最后一步是**自查**：把即将入库的正文按同一口径再判一次。结构化脱敏是在**解码后**
    的值上做的，而回写又是一次**重新转义**——解码后已不含凭据的值，转义后可能重新拼
    出凭据的字符序列（目标回显 `C:\\temp | C:\\\\temp`、凭据是字面量 `C:\\temp` 时，
    第一段的 `\\t` 解出制表符，回写又变回 `\\t`）。自己都验不过的正文不放出去：这属于
    “无法确认脱敏完整”，与残缺 JSON 同等处理——整段明确省略。
    """
    if not body_text.strip():
        # 空正文没有内容可解析，也不存在“没处理完”的部分。
        return StoredBody(text=body_text, format="text")
    try:
        parsed = loads(body_text)
    except LosslessJSONError:
        if json_expected or _looks_like_json_document(body_text):
            return StoredBody(text=None, omitted_reason=OMITTED_UNPARSABLE_JSON)
        text, truncated = _truncate_stored(redact_text(body_text, secrets))
        return StoredBody(text=text, truncated=truncated, format="text")
    if not value_contains_secret(parsed, secrets):
        redacted = body_text
    else:
        redacted = dumps(redact_value(parsed, secrets))
    if contains_secret(redacted, secrets):
        return StoredBody(text=None, omitted_reason=OMITTED_INCOMPLETE_REDACTION)
    text, truncated = _truncate_stored(redacted)
    return StoredBody(text=text, truncated=truncated, format="json")


def _evidence_url(prepared: PreparedRequest, secrets: list[str]) -> str:
    """请求证据里的 URL：从**未编码的真实查询项**重建，先脱敏、再编码。

    直接对 `prepared.url` 做字符串替换挡不住“必须编码的秘密”。凭据 `a+b/c=` 上网
    时是 `a%2Bb%2Fc%3D`，原文串在 URL 里一个字符都不存在，replace 匹配不到，报告
    里留下的是一份 URL 解码就能还原的副本。

    正确顺序只能是“先处理敏感性、再编码”：取值此刻还是明文，遮蔽是精确的；编码
    随后按发送时那一个入口做，凭据实际会变成什么形态由本地编码器决定，不需要、也
    不允许再维护一条“猜秘密可能被编码成什么样”的替换分支。普通参数（含重复项与
    顺序）在重建后与线上完全一致——`rebuild_url` 就是发送用的同一个函数。

    传入的是**用例相对路径**：环境基础路径由 `rebuild_url` 通过 `base` 拼在前面，
    传 `final_path` 会把它拼成两份（`/api/v1/api/v1/echo`）。
    """
    pairs = [
        (redact_text(name, secrets), redact_text(value, secrets))
        for name, value in prepared.query
    ]
    return prepared.rebuild_url(redact_text(prepared.relative_path, secrets), pairs)


def _sanitize(message: str, secrets: list[str]) -> str:
    """错误出口的兜底脱敏：任何要写进报告／数据库的文本先过这里。

    秘密出现在错误信息里有多条路径（请求头传输校验、变量解析失败、策略拒绝时
    拼接的上下文）。逐条去堵来源，总会漏掉还没想到的那一条；把脱敏放在**出口**
    上，新加的报错自动受保护，也不必要求每个 `raise` 都自觉带脱敏。

    走的是与正文、断言结果同一个原语（`redact_text`），因此转义形态一并覆盖：
    按 `json.dumps(secret)` 拼一种转义再替换，只能挡住那一种写法。
    """
    return redact_text(message, secrets)


def _sanitize_evidence(value: Any, secrets: list[str]) -> Any:
    """证据信封的出口脱敏：遮蔽**字符串取值**，保留信封自己的字段名。

    与成功路径的脱敏不是二选一。这里是发送意图已写下、准入复核拒绝的出口，它带着
    已经构造好的请求证据；即使上游按秘密位置脱过一遍，出口仍要再过一道，避免某天
    新增的证据字段漏了脱敏就整份落库。

    只遮蔽取值，**不动字段名**：信封的字段名（`method`／`url`／`headers`／`body`
    与参数项里的 `name`／`value`）是服务端常量，不是用户内容。对它们一并遮蔽会在
    合法凭证恰好等于其中某个词时把信封改成机器读不懂的形状——凭据是 `body` 时
    `{"method":"GET","body":…}` 会变成 `{"method":"GET","***":…}`，报告里读不出
    这条请求的证据去哪了。用户／响应派生的键名不在这里：它们出现在响应 JSON 正文
    与抽取出的结构里，由 `redact_value` 在正文出口遮蔽键名与取值。
    """
    if isinstance(value, str):
        return _sanitize(value, secrets)
    if isinstance(value, dict):
        return {str(key): _sanitize_evidence(val, secrets) for key, val in value.items()}
    if isinstance(value, list):
        return [_sanitize_evidence(item, secrets) for item in value]
    return value


def _sanitize_selector(selector: list[dict], secrets: list[str]) -> list[dict]:
    """按定位步骤的**判别联合契约**脱敏，而不是把整个步骤当普通结构递归。

    步骤形如 `{"kind":"key","key":…}`／`{"kind":"index","index":…}`／
    `{"kind":"repeat_key","key":…,"occurrence":…}`／
    `{"kind":"row","row_id":…}`。其中：

    - `kind`（枚举）、`index`（数字下标）、`occurrence`（重复项序号）来自服务端
      常量与结构位置，不是用户内容，也不是可读文本——数字索引的语义必须原样保留，
      否则 `[0]` 与 `[1]` 会变成同一个定位；
    - 已校验 row 步骤中的标准 UUID `row_id` 是请求写入前已知的结构身份，与数字
      下标一样原样保留；它不从凭证、响应或变量派生；
    - `key` 是**用户／响应派生**的字段名，秘密整个作为字段名出现时就落在它上面，
      必须照常遮蔽。

    整棵递归遮蔽会同时砸掉这两类：合法凭证恰好等于固定词 `key` 时，
    `{"kind":"key","key":"name"}` 会变成 `{"kind":"***","***":"name"}`——报告里既
    读不出核对的是哪个字段，机器也再解析不出这条定位。契约之外的多余字段按用户
    内容处理：宁可多遮，不能让将来新增的字段成为漏出口。
    """
    steps: list[dict] = []
    for step in selector:
        if not isinstance(step, dict):
            steps.append(_sanitize_evidence(step, secrets))
            continue
        cleaned: dict = {}
        is_valid_row = False
        if step.get("kind") == "row" and isinstance(step.get("row_id"), str):
            try:
                normalized_row_id = str(uuid.UUID(step["row_id"]))
            except ValueError:
                pass
            else:
                is_valid_row = step["row_id"].lower() == normalized_row_id
        for name, value in step.items():
            if name == "key":
                cleaned[name] = _sanitize(str(value), secrets)
            elif name == "row_id" and is_valid_row:
                cleaned[name] = value
            elif name in ("kind", "index", "occurrence"):
                cleaned[name] = value
            else:
                cleaned[name] = _sanitize_evidence(value, secrets)
        steps.append(cleaned)
    return steps


def _authorize_prepared(guard: TargetGuard, prepared: PreparedRequest) -> tuple[Any, str]:
    """按**实际将发送的 URL** 复核目标，并给出与之匹配的固定连接地址。

    不能用“按当前环境重新拼一个目标”代替这一步：那校验的是另一个地址，而请求仍然
    发往 `prepared.url`。环境若在准备请求与真正发送之间被改到 B，检查会按 B 通过，
    请求却发给了 A——既绕过了 B 的白名单判断，也让按 B 解析出的固定地址根本套不到
    A 的主机上（主机名不同，固定地址查不到），于是静默退回普通 DNS 连接。

    因此这里只认 `prepared.url`：它就是将要上线的那一份内容。允许来源按它的
    scheme/host/port 在**当前**策略下重新判定，撤销或改到白名单之外一律拒绝，
    绝不静默换一个目标。
    """
    try:
        target = guard.authorize_url(prepared.url)
    except TargetPolicyError as error:
        raise SendFailure("target_not_allowed", str(error), "policy", False) from error
    try:
        pinned = guard.pinned_address(target)
    except TargetPolicyError as error:
        raise SendFailure("target_not_allowed", str(error), "policy", False) from error
    return target, pinned


def _advance_to_running(session: Session, claim: JobClaim, worker_id: str) -> bool:
    """把运行从 created/queued 推进到 running，条件是它仍可执行且本次领取仍有效。

    条件更新而不是 ORM 赋值：finished 是终态，任何“先读后写”的路径都可能被并发
    的取消插在中间，把已经结束的运行重新变成运行中。

    租约与 fencing token 同样是条件的一部分：工作项在租约到期后会被重新领取并
    递增 token，此时旧执行者已经不再持有该工作项。旧执行者若还能推进状态，就是
    替新执行者伪造运行状态，并继续走完后面的凭证解析——一次性授权会在真正发送
    之前被它消费掉。返回是否推进成功，由调用方区分“被取消”与“租约已失效”。
    """
    advanced = session.execute(
        text(
            "UPDATE app.runs SET state = 'running', updated_at = now() "
            "WHERE id = :run AND workspace_id = :ws AND state IN ('created', 'queued') "
            "AND EXISTS (SELECT 1 FROM app.jobs j WHERE j.id = :job AND j.state = 'leased' "
            "AND j.leased_by = :worker AND j.fencing_token = :token AND j.lease_until > now()) "
            "RETURNING id"
        ),
        {
            "run": str(claim.run_id),
            "ws": str(claim.workspace_id),
            "job": str(claim.job_id),
            "worker": worker_id,
            "token": claim.fencing_token,
        },
    ).first()
    return advanced is not None


def _current_state(session: Session, claim: JobClaim) -> str | None:
    return session.execute(
        text("SELECT state FROM app.runs WHERE id = :run AND workspace_id = :ws"),
        {"run": str(claim.run_id), "ws": str(claim.workspace_id)},
    ).scalar()


def _side_effect_of(session: Session, run: Run) -> str:
    """本次运行声明的作用分类；未发布版本或未标记时按 unknown 处理。

    unknown 比 read 更保守：只有用例版本显式标记为只读，恢复时才允许重新发送。
    """
    if run.target_type == "case_version" and run.case_version_id is not None:
        version = session.get(CaseVersion, run.case_version_id)
        if version is not None:
            return version.side_effect
    return "unknown"


def _unresolved_send_intent(session: Session, claim: JobClaim) -> datetime | None:
    """查找同一运行同一步骤里“已写发送意图但从未落结果”的尝试。

    发送意图先于实际请求提交，因此存在该行就说明请求可能已经发出；缺少完成证据
    时目标是否执行过无法判定。返回最早一条的时间，供调用方决定是否禁止重发。
    """
    return session.execute(
        text(
            "SELECT min(send_intent_at) FROM app.run_step_attempts "
            "WHERE run_id = :run AND workspace_id = :ws AND step_key = 'main' "
            "AND send_intent_at IS NOT NULL AND outcome IS NULL"
        ),
        {"run": str(claim.run_id), "ws": str(claim.workspace_id)},
    ).scalar()


def _persist_attempt(
    session: Session,
    claim: JobClaim,
    *,
    state: str,
    outcome: str | None,
    evidence: StepEvidence,
    error_code: str | None,
    send_intent_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    elapsed_ms: int | None,
) -> RunStepAttempt:
    attempt = RunStepAttempt(
        workspace_id=claim.workspace_id,
        project_id=claim.project_id,
        run_id=claim.run_id,
        step_key="main",
        attempt_no=claim.attempt,
        state=state,
        outcome=outcome,
        send_intent_at=send_intent_at,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        request=_request_evidence(evidence),
        response=evidence.response,
        error_code=error_code,
    )
    session.add(attempt)
    session.flush()
    return attempt


def _persist_assertion_results(
    session: Session,
    claim: JobClaim,
    attempt_id: uuid.UUID,
    records: list[AssertionRecord],
    secrets: list[str] | None = None,
) -> None:
    """写入断言结果；动态字段（定位、可读路径、期望与实际）在落库前统一脱敏。

    敏感性判定已经会让触到受保护值的断言以策略拒绝收尾，但那是主保护而不是唯一
    一层：任何一条绕过判定的路径（新增取值来源、规则改动）都不应该把明文直接写进
    数据库。这里按已解析出的秘密统一遮蔽，报告读到的就是最终形态，不需要依赖读方
    再做一次处理。

    遮蔽范围不只 `expected`／`actual`。**定位信息同样是用户／响应派生的动态内容**：
    秘密整个作为对象键出现时（目标回显 `{"<凭据>": "ok"}`），被拒绝的断言即使期望
    与实际都是 NULL，`selector` 里的键名与渲染出来的可读路径照样把秘密写进报告——
    遮蔽一个出口、留着另一个，等于没遮。固定信封字段（`type`、`phase`、`source`
    等枚举与版本号）保持原样：它们来自服务端常量，不是用户内容。
    """
    secrets = secrets or []
    for record in records:
        selector = record.assertion.get("selector") or []
        session.add(
            AssertionResult(
                workspace_id=claim.workspace_id,
                project_id=claim.project_id,
                run_id=claim.run_id,
                step_attempt_id=attempt_id,
                assertion_id=record.assertion["id"],
                type=record.assertion["type"],
                operator_version=get_type(record.assertion["type"]).operator_version,
                phase=record.phase,
                target={
                    "source": record.assertion["target_source"],
                    "selector": _sanitize_selector(selector, secrets),
                    "path": redact_text(describe_selector(selector), secrets),
                },
                status=record.outcome.status,
                expected=redact_value(_jsonable(record.outcome.expected), secrets),
                actual=redact_value(_jsonable(record.outcome.actual), secrets),
                reason_code=record.outcome.reason_code,
                elapsed_ms=record.elapsed_ms,
            )
        )
    session.flush()


def _finalize(
    session: Session,
    claim: JobClaim,
    worker_id: str,
    *,
    outcome: str,
    reason_category: str | None,
    attempt_state: str,
    attempt_outcome: str | None,
    evidence: StepEvidence,
    error_code: str | None,
    send_intent_at: datetime | None,
    started_at: datetime | None,
    elapsed_ms: int | None,
    records: list[AssertionRecord] | None = None,
    attempt_id: uuid.UUID | None = None,
    secrets: list[str] | None = None,
) -> bool:
    """以 fencing token 为条件提交终态；旧 token 的结果不得覆写当前状态。

    已写下发送意图的尝试通过 `attempt_id` 在原来那行收尾，不新建第二行：一次
    (run, step_key, attempt_no) 只有一条尝试记录，发送意图与最终结果属于同一行。

    fencing token 之外还要比对租约本身是否仍然有效：租约到期但**尚未被重新领取**
    的工作项，token 与 leased_by 都还是旧的，只比这两项等于让一个已经超期的执行者
    继续写结果。过期执行者的结论不比其他执行者的更新，一律拒收，由恢复流程收敛。
    """
    session.rollback()
    _set_tenant(session, claim.workspace_id)
    guarded = session.execute(
        text(
            "UPDATE app.jobs SET state = 'done', lease_until = NULL, updated_at = now() "
            "WHERE id = :job AND state = 'leased' AND leased_by = :worker AND fencing_token = :token "
            "AND lease_until > now() "
            "RETURNING id"
        ),
        {"job": str(claim.job_id), "worker": worker_id, "token": claim.fencing_token},
    ).first()
    if guarded is None:
        session.rollback()
        return False

    updated = session.execute(
        text(
            "UPDATE app.runs SET state = 'finished', outcome = :outcome, reason_category = :reason, "
            "updated_at = now() "
            "WHERE id = :run AND workspace_id = :ws AND state IN ('queued', 'running') "
            "RETURNING id"
        ),
        {
            "outcome": outcome,
            "reason": reason_category,
            "run": str(claim.run_id),
            "ws": str(claim.workspace_id),
        },
    ).first()
    if updated is None:
        session.rollback()
        return False

    now = datetime.now(UTC)
    if attempt_id is not None:
        attempt = session.get(RunStepAttempt, attempt_id)
        if attempt is None:
            session.rollback()
            return False
        attempt.state = attempt_state
        attempt.outcome = attempt_outcome
        attempt.error_code = error_code
        attempt.send_intent_at = send_intent_at
        attempt.started_at = started_at
        attempt.finished_at = now
        attempt.elapsed_ms = elapsed_ms
        attempt.request = _request_evidence(evidence)
        attempt.response = evidence.response
        session.flush()
        resolved_id = attempt.id
    else:
        attempt = _persist_attempt(
            session,
            claim,
            state=attempt_state,
            outcome=attempt_outcome,
            evidence=evidence,
            error_code=error_code,
            send_intent_at=send_intent_at,
            started_at=started_at,
            finished_at=now,
            elapsed_ms=elapsed_ms,
        )
        resolved_id = attempt.id
    if records:
        _persist_assertion_results(session, claim, resolved_id, records, secrets)
    session.commit()
    return True


def _send(
    settings: Settings, pin: dict[str, str], prepared: PreparedRequest
) -> tuple[httpx.Response | None, str, SendFailure | None]:
    """受控发送：禁代理、禁重定向、连接地址固定为已校验的 IP。"""
    try:
        with build_client(pin, settings.http_connect_timeout, settings.http_read_timeout) as client:
            response = client.request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                content=prepared.content,
            )
            try:
                body_text = response.text
            except UnicodeDecodeError:
                body_text = response.content.decode("utf-8", errors="replace")
        return response, body_text, None
    except httpx.ConnectTimeout as error:
        return None, "", SendFailure("connect_timeout", str(error), "network", False)
    except httpx.ConnectError as error:
        return None, "", SendFailure("connect_failed", str(error), "network", False)
    except PinnedAddressMissing as error:
        # 兜底：准入已按 `prepared.url` 取到固定地址，这里查不到说明内部状态不一致。
        # 客户端宁可抛错也不退回普通 DNS 连接，执行器把它记成明确的策略拒绝，
        # 而不是让异常冒到 worker 外层、把工作项留给下一轮租约到期重新领取。
        return None, "", SendFailure("target_not_pinned", str(error), "policy", False)
    except (httpx.ReadTimeout, httpx.WriteTimeout) as error:
        # 请求可能已送达：副作用不明时不得自动重放。
        return None, "", SendFailure("read_timeout", str(error), "network", True)
    except httpx.TooManyRedirects as error:
        return None, "", SendFailure("redirect_blocked", str(error), "policy", True)
    except httpx.HTTPError as error:
        return None, "", SendFailure("http_transport_error", str(error), "network", True)


def execute_claim(
    session_factory: sessionmaker[Session], settings: Settings, claim: JobClaim
) -> str:
    """执行一个已领取的工作项，返回写入的终态（或 stale／canceled）。"""
    session = session_factory()
    try:
        return _execute(session, settings, claim)
    finally:
        session.close()


def _execute(session: Session, settings: Settings, claim: JobClaim) -> str:
    worker_id = settings.worker_id
    # 先只绑定工作空间读出运行：runs 的策略只按 workspace_id 过滤，而复核发起人
    # 权限需要 run.created_by，身份上下文只能在读到这一行之后补上。
    _set_tenant(session, claim.workspace_id)
    run = session.get(Run, claim.run_id)
    if run is None:
        session.rollback()
        return "missing"
    _set_tenant(session, claim.workspace_id, run.created_by)
    if run.state == "finished":
        # 已终态（例如被取消）：只收尾工作项，不重复执行。
        session.execute(
            text(
                "UPDATE app.jobs SET state = 'done', lease_until = NULL, updated_at = now() "
                "WHERE id = :job AND state = 'leased' AND leased_by = :worker AND fencing_token = :token"
            ),
            {"job": str(claim.job_id), "worker": worker_id, "token": claim.fencing_token},
        )
        session.commit()
        return run.outcome or "finished"

    # 上一次尝试已落下发送意图、却没写下任何完成证据，说明进程在“请求可能已经
    # 发出”和“结果入库”之间中断了。重新领取的 worker 无法判断目标是否已经执行，
    # 因此除明确标记为只读的用例外一律不再发送，收敛为 interrupted；否则一次
    # 崩溃就会变成对目标系统的重复写操作。
    prior_intent_at = _unresolved_send_intent(session, claim)
    if prior_intent_at is not None and _side_effect_of(session, run) != "read":
        _finalize(
            session,
            claim,
            worker_id,
            outcome="interrupted",
            reason_category="unknown",
            attempt_state="skipped",
            attempt_outcome="interrupted",
            evidence=StepEvidence(request={"skipped": "previous_send_intent_unresolved"}),
            error_code="write_result_unknown",
            send_intent_at=None,
            started_at=None,
            elapsed_ms=None,
        )
        return "interrupted"

    now = datetime.now(UTC)
    # 排队超时与业务截止都在发送前判定：超时不再发出请求。
    deadline_code = None
    if run.queue_deadline_at is not None and now > run.queue_deadline_at:
        deadline_code = "queue_deadline_exceeded"
    elif run.business_deadline_at is not None and now > run.business_deadline_at:
        deadline_code = "business_deadline_exceeded"
    if deadline_code is not None:
        _finalize(
            session,
            claim,
            worker_id,
            outcome="timed_out",
            reason_category="platform",
            attempt_state="skipped",
            attempt_outcome="error",
            evidence=StepEvidence(request={"skipped": deadline_code}),
            error_code=deadline_code,
            send_intent_at=None,
            started_at=None,
            elapsed_ms=None,
        )
        return "timed_out"

    if run.execution_started_at is None:
        run.execution_started_at = now
    advanced = _advance_to_running(session, claim, worker_id)
    session.commit()

    # 领取与开始执行之间可能已经被取消。取消方把运行置为终态时 work 仍在进行，
    # 无条件写 running 会把这个终态翻回来，请求随后照常发出——取消因此失效。
    # 状态推进必须以当前状态为前提条件，终态不得被复活。
    if not advanced:
        # 推进失败有两种原因：运行已被取消（终态不得复活），或本次领取已经失效。
        # 后者说明工作项已被别的执行者接管，此时必须立刻退出，不能再往下走凭证
        # 解析与入参断言——那会在发送前消费掉一次性授权。
        if not _lease_still_valid(session, worker_id, claim):
            session.rollback()
            return "stale"
    if _current_state(session, claim) != "running":
        session.rollback()
        return "canceled"

    environment = session.get(Environment, run.environment_id)
    if environment is None:
        _finalize(
            session,
            claim,
            worker_id,
            outcome="error",
            reason_category="configuration",
            attempt_state="skipped",
            attempt_outcome="error",
            evidence=StepEvidence(request={}),
            error_code="environment_missing",
            send_intent_at=None,
            started_at=None,
            elapsed_ms=None,
        )
        return "error"

    snapshot = run.snapshot or {}
    # 本次运行准备请求实际取用的输入摘要（来自创建运行时冻结的快照）。授权绑定必须
    # 以它为准：只比“当前变量”会漏掉“按 A 授权 → 改成 B 入队 → 改回 A”这条路径，
    # 那时当前值等于签发值、检查通过，而发出去的请求用的是快照里的 B。
    frozen_input_digest = frozen_snapshot_digest(snapshot.get("variables"))
    try:
        # 执行池授权按当前状态重算：入队之后被撤销的授权必须在这里拦住。
        guard = _recheck_pool_authority(session, run, environment, settings)
        # 执行池只是其中一项。发起人是否仍能执行、环境是否仍可用同样可能在入队
        # 之后变化，必须在发送前按当前事实复核，否则停用账号或归档环境都拦不住
        # 已经在队列里的运行。
        _recheck_current_facts(session, run, environment)
        request = validate_request(snapshot.get("request") or {})
        assertions = validate_assertions(snapshot.get("assertions") or [], request)
        target = _authorized_target(guard, environment, request)
        # 环境冻结的差异检查放在策略检查**之后**：改成生产、改到白名单之外这类拒绝
        # 比“配置和提交时不一样”具体得多，用户按提示要做的处理也不同。两者都不发
        # 请求，但报错只有一次机会，应当报最贴近原因的那一个。
        _recheck_frozen_environment(run, environment, snapshot)
        pinned = guard.pinned_address(target)
    except (RequestSpecError, AssertionSpecError) as error:
        return _fail_without_send(session, claim, worker_id, "configuration", "case_invalid", str(error))
    except SendFailure as failure:
        return _fail_without_send(
            session, claim, worker_id, failure.category, failure.code, failure.message
        )
    except TargetPolicyError as error:
        return _fail_without_send(
            session, claim, worker_id, "policy", "target_not_allowed", str(error)
        )

    side_effect = _side_effect_of(session, run)

    resolver = build_resolver(
        [{"name": name, "value": value} for name, value in (snapshot.get("variables") or {}).items()]
    )
    # 注入解析成功之前没有秘密可脱敏，但失败出口必须能无条件取到这份列表。
    secrets: list[str] = []
    try:
        injection = resolve_injection(
            session,
            settings,
            environment=environment,
            principal_id=run.created_by,
            case_version_id=run.case_version_id,
            # 摘要来自创建运行时冻结的快照内容，执行阶段不重新计算：重新计算会把
            # “授权绑定哪份快照”变成执行器可自行决定的值。已发布版本这里为空，
            # 授权匹配按 grant_type 分支，不会误借调试授权的空摘要。
            debug_snapshot_hash=snapshot.get("debug_snapshot_hash"),
            target_origin=target.origin,
            frozen_input_digest=frozen_input_digest,
            # 必须使用当前环境登录态的请求（导入时识别到认证头／Cookie）在快照里带
            # auth_required；没有可用身份时在这里失败，不退回匿名发送。
            auth_required=bool(request.get("auth_required")),
            # 一次性授权只能由仍持有本次领取的执行者消费。
            lease_guard=LeaseGuard(
                job_id=claim.job_id,
                worker_id=worker_id,
                fencing_token=claim.fencing_token,
            ),
        )
        # 注入一旦解析出来，秘密就进入了后续的请求构造。从这里起的每个错误出口
        # 都要带上这份值做出口脱敏：请求头校验、变量解析都可能把明文写进异常。
        secrets = list(injection.secret_values)
        prepared = prepare(
            request,
            environment.base_url,
            resolver,
            injected_headers=injection.headers,
            injected_query=injection.query,
        )
    except CredentialError as error:
        if error.code == "credential_lease_lost":
            # 工作项已被接管：本次执行作废，但不写终态、不发请求，由新执行者按
            # 正常路径收敛，避免用旧执行者的一条错误结论覆盖新结论。
            session.rollback()
            return "stale"
        return _fail_without_send(
            session, claim, worker_id, "authentication", error.code, error.message, secrets,
            guards_evaluated=True,
        )
    except (RequestSpecError, VariableResolutionError) as error:
        return _fail_without_send(
            session,
            claim,
            worker_id,
            "configuration",
            "request_invalid",
            str(error),
            secrets,
            guards_evaluated=True,
        )
    session.commit()

    ctx = AssertionContext()
    # 受保护位置不止“注入到哪”，还包括“秘密实际出现在哪”。目标把认证头原样回显到
    # 正文时，回显字段同样是秘密的可读副本；只标注注入位置会漏掉它。
    ctx.sensitive_paths |= paths_containing(
        _build_request_roots(prepared, request), secrets
    )
    request_evidence = {
        "method": prepared.method,
        "url": _evidence_url(prepared, secrets),
        "headers": redact_pairs(prepared.headers_as_pairs(), secrets),
        "body": redact_text(prepared.body_text(), secrets),
    }

    pre_records = _evaluate_assertions(
        assertions, "pre_request", _build_request_roots(prepared, request), ctx
    )
    blocked = _blocking_failure(pre_records) or _first_error(pre_records)
    if blocked is not None:
        # 入参断言失败不发 HTTP；响应断言记为未执行。
        skipped_post = _evaluate_assertions(assertions, "post_response", SourceRoots(), ctx)
        _finalize(
            session,
            claim,
            worker_id,
            outcome="failed" if blocked.outcome.status == "failed" else "error",
            reason_category="assertion" if blocked.outcome.status == "failed" else "configuration",
            attempt_state="skipped",
            attempt_outcome=blocked.outcome.status,
            evidence=StepEvidence(request=request_evidence, guards_evaluated=True),
            error_code="pre_request_assertion_failed",
            send_intent_at=None,
            started_at=None,
            elapsed_ms=None,
            records=pre_records + skipped_post,
            secrets=secrets,
        )
        return "failed" if blocked.outcome.status == "failed" else "error"

    # —— 发送意图：先持久化，再校验租约与取消，最后发出请求 ——
    intent_at = datetime.now(UTC)
    attempt = _persist_attempt(
        session,
        claim,
        state="sending",
        outcome=None,
        evidence=StepEvidence(request=request_evidence, guards_evaluated=True),
        error_code=None,
        send_intent_at=intent_at,
        started_at=intent_at,
        finished_at=None,
        elapsed_ms=None,
    )
    _persist_assertion_results(session, claim, attempt.id, pre_records, secrets)
    session.commit()

    # —— 准入点：按**当前**状态重读主体／项目／环境／池／凭证授权 ——
    # 上面的复核与这里之间还隔着凭证解析、请求准备与入参断言，撤销账号、归档环境
    # 或收回授权都可能发生在这段窗口里；只查运行状态与租约拦不住这些。
    # 数据库连接用 expire_on_commit=False，同一 Session 里的对象不会自动重读，
    # 因此先显式过期：不复位就会把先前的校验结论原样当成“当前事实”。
    session.expire_all()
    try:
        admission_guard = _recheck_pool_authority(session, run, environment, settings)
        _recheck_current_facts(session, run, environment)
        # 以真正将发送的 URL 为准：权限可以按当前策略重读，但目标不能重新构造一个
        # 来代替实际要发的地址，否则检查与发送会指向两个不同的主机。
        send_target, pinned = _authorize_prepared(admission_guard, prepared)
        recheck_grant_authority(
            session,
            injection,
            environment=environment,
            principal_id=run.created_by,
            target_origin=send_target.origin,
            frozen_input_digest=frozen_input_digest,
        )
    except SendFailure as failure:
        return _block_after_intent(
            session, claim, worker_id, attempt.id, intent_at, request_evidence,
            failure.category, failure.code, secrets,
        )
    except TargetPolicyError:
        return _block_after_intent(
            session, claim, worker_id, attempt.id, intent_at, request_evidence,
            "policy", "target_not_allowed", secrets,
        )
    except CredentialError as error:
        if error.code == "credential_lease_lost":
            session.rollback()
            return "stale"
        return _block_after_intent(
            session, claim, worker_id, attempt.id, intent_at, request_evidence,
            "authentication", error.code, secrets,
        )

    if not _lease_still_valid(session, worker_id, claim):
        # 租约校验在发送之前，走到这里说明请求确实没有发出。把这一事实写成终止
        # 证据，恢复时才能区分“意图已存但确定没发”和“进程在发送前后崩溃”。
        session.rollback()
        _record_attempt_not_sent(session, claim, attempt.id, "lease_lost_before_send")
        return "stale"
    state_now = session.execute(
        text("SELECT state FROM app.runs WHERE id = :run AND workspace_id = :ws"),
        {"run": str(claim.run_id), "ws": str(claim.workspace_id)},
    ).scalar()
    session.rollback()
    if state_now != "running":
        # 取消发生在发送意图之后、实际发送之前：记录“意图已存但未发出”，
        # 既不补发也不把这次尝试当作已发请求。
        _record_attempt_not_sent(session, claim, attempt.id, "canceled_before_send")
        return "canceled"

    # 发送前结束读事务，网络中不持有数据库连接。
    started = time.perf_counter()
    response, body_text, failure = _send(settings, {send_target.host: pinned}, prepared)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if failure is not None:
        return _handle_send_failure(
            session, claim, worker_id, failure, side_effect, request_evidence, secrets,
            intent_at, attempt.id,
        )

    assert response is not None
    stored = _stored_body(
        body_text,
        secrets,
        # 格式上下文有两个来源：目标自己声明的类型头，以及**用例对解析后的正文提的
        # 条件**。只看类型头时，把 JSON 写成 text/plain 的目标（或压根不带类型头的
        # 目标）会让残缺正文以纯文本入库，配置在 response.body 上的条件静默跳过。
        json_expected=(
            _declares_json(response.headers.get("content-type"))
            or _expects_json_body(assertions)
        ),
    )
    response_evidence = {
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "headers": redact_pairs(
            [{"name": key, "value": value} for key, value in response.headers.multi_items()],
            secrets,
        ),
        "body": stored.text,
        "body_format": stored.format,
        "body_truncated": stored.truncated,
        "body_omitted_reason": stored.omitted_reason,
        "size_bytes": len(body_text.encode("utf-8")),
    }
    response_roots = _build_response_roots(
        prepared, request, response, body_text, elapsed_ms, stored
    )
    # 响应侧的受保护坐标必须在求值之前补齐：目标回显的秘密就是从这里被认出来的。
    ctx.sensitive_paths |= paths_containing(response_roots, secrets)
    post_records = _evaluate_assertions(
        assertions,
        "post_response",
        response_roots,
        ctx,
    )
    outcome, reason, error_code = _summarize(pre_records, post_records)
    attempt_outcome = {"passed": "passed", "failed": "failed"}.get(outcome, "error")
    _finalize(
        session,
        claim,
        worker_id,
        outcome=outcome,
        reason_category=reason,
        attempt_state="finished",
        attempt_outcome=attempt_outcome,
        evidence=StepEvidence(
            request=request_evidence, response=response_evidence, guards_evaluated=True
        ),
        error_code=error_code,
        send_intent_at=intent_at,
        started_at=intent_at,
        elapsed_ms=elapsed_ms,
        # 只写响应阶段的结果：入参结果已在发送意图那一步提交并持久化，作为
        # 本次尝试的一部分存在。再传一次会在同一 attempt 下插入重复行，
        # 报告中同一字段会出现两条一模一样的断言记录。
        records=post_records,
        attempt_id=attempt.id,
        secrets=secrets,
    )
    return outcome


def _first_error(records: list[AssertionRecord]) -> AssertionRecord | None:
    return next((item for item in records if item.outcome.status == "error"), None)


def _fail_without_send(
    session: Session,
    claim: JobClaim,
    worker_id: str,
    category: str,
    code: str,
    message: str,
    secrets: list[str] | None = None,
    guards_evaluated: bool = False,
) -> str:
    """发送前的拒绝：不写发送意图，不产生任何目标副作用。

    `message` 会原样进报告与数据库，因此按出口统一脱敏：认证注入解析出来之后
    才失败的路径（请求准备、入参断言）都可能把秘密带进异常文本。

    `guards_evaluated` 标记本轮环境冻结与必需认证保护是否已经执行过：保护的检查块
    内部失败时它仍是 False，报告因此不会给这条记录一个它没有依据的来源证明。
    """
    _finalize(
        session,
        claim,
        worker_id,
        outcome="error",
        reason_category=category,
        attempt_state="skipped",
        attempt_outcome="error",
        evidence=StepEvidence(
            request={"rejected": code, "message": _sanitize(message, secrets or [])},
            guards_evaluated=guards_evaluated,
        ),
        error_code=code,
        send_intent_at=None,
        started_at=None,
        elapsed_ms=None,
    )
    return "error"


def _block_after_intent(
    session: Session,
    claim: JobClaim,
    worker_id: str,
    attempt_id: uuid.UUID,
    intent_at: datetime,
    request_evidence: dict,
    category: str,
    code: str,
    secrets: list[str] | None = None,
) -> str:
    """发送意图已写下，但准入复核在真正发送之前拒绝了本次执行。

    这次请求**确定没有发出**，因此按“未发送”收尾：在原来那行尝试上写明错误码，
    保留发送意图时间（它是下一次恢复判断“是否可能已发”的依据），不产生任何目标
    副作用。终态分类与发送前拒绝保持一致，避免同一个原因出现两套语义。
    证据经出口脱敏后再落库，与发送前拒绝走同一套保护。
    """
    _finalize(
        session,
        claim,
        worker_id,
        outcome="error",
        reason_category=category,
        attempt_state="skipped",
        attempt_outcome="error",
        evidence=StepEvidence(
            request=_sanitize_evidence(request_evidence, secrets or []),
            guards_evaluated=True,
        ),
        error_code=code,
        send_intent_at=intent_at,
        started_at=None,
        elapsed_ms=None,
        attempt_id=attempt_id,
    )
    return "error"


def _handle_send_failure(
    session: Session,
    claim: JobClaim,
    worker_id: str,
    failure: SendFailure,
    side_effect: str,
    request_evidence: dict,
    secrets: list[str],
    intent_at: datetime,
    attempt_id: uuid.UUID,
) -> str:
    """发送失败：已送达但结果不明且可能写副作用时记为 interrupted，不重放。"""
    interrupted = failure.ambiguous and side_effect != "read"
    outcome = "interrupted" if interrupted else "error"
    error_code = "write_result_unknown" if interrupted else failure.code
    _finalize(
        session,
        claim,
        worker_id,
        outcome=outcome,
        reason_category=failure.category,
        attempt_state="finished",
        attempt_outcome="interrupted" if interrupted else "error",
        evidence=StepEvidence(
            request=request_evidence,
            response={"error": redact_text(failure.message, secrets)},
            guards_evaluated=True,
        ),
        error_code=error_code,
        send_intent_at=intent_at,
        started_at=intent_at,
        elapsed_ms=None,
        attempt_id=attempt_id,
    )
    return outcome


def _record_attempt_not_sent(
    session: Session,
    claim: JobClaim,
    attempt_id: uuid.UUID,
    error_code: str,
) -> None:
    """在发送之前终止尝试：保留发送意图时间，并明确写出“这一次没有发出请求”。

    这是恢复路径唯一可信的“未发送”证据。没有它，后来重新领取的 worker 只能看到
    “有意图、没结果”，按最保守的规则判为结果不明，把一次确定的空发误报为需要
    人工确认的写副作用。取消与租约丢失都属于这一类。
    """
    session.rollback()
    _set_tenant(session, claim.workspace_id)
    session.execute(
        text(
            "UPDATE app.run_step_attempts SET state = 'skipped', "
            "outcome = CASE WHEN :code = 'canceled_before_send' THEN 'canceled' ELSE 'error' END, "
            "error_code = :code, finished_at = now() "
            "WHERE id = :attempt AND workspace_id = :ws"
        ),
        {"attempt": str(attempt_id), "ws": str(claim.workspace_id), "code": error_code},
    )
    session.commit()


def _lease_still_valid(session: Session, worker_id: str, claim: JobClaim) -> bool:
    row = session.execute(
        text(
            "SELECT 1 FROM app.jobs WHERE id = :job AND " + LEASE_HELD_CONDITION
        ),
        {"job": str(claim.job_id), "worker": worker_id, "token": claim.fencing_token},
    ).first()
    return row is not None


__all__ = [
    "AssertionRecord",
    "JobClaim",
    "LEASE_HELD_CONDITION",
    "SendFailure",
    "StepEvidence",
    "claim_job",
    "describe_selector",
    "execute_claim",
]
