"""凭证存储与认证注入。

秘密只以密文入库；身份配置声明允许的目标与认证槽位，凭证集合按槽位绑定
秘密版本。执行时按“身份管理员授权的主体 + 环境 + 固定用例版本（或一次性
调试快照）”解析出注入值。注入位置同时标记为敏感路径，普通断言与试算不得
以受保护值作为比较对象，避免用布尔结果反推秘密。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..kernel.target_policy import normalize_origin
from ..models import (
    CaseVersion,
    CredentialProfile,
    CredentialProfileVersion,
    CredentialSet,
    CredentialSetSecretVersion,
    CredentialUseGrant,
    Environment,
    Secret,
    SecretVersion,
)
from ..security import decrypt_secret, encrypt_secret
from .variable_inputs import environment_input_digest

_SLOT_KINDS = {"header", "query"}


class CredentialError(Exception):
    """凭证配置或授权问题，附带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Injection:
    """解析出的认证注入结果。

    这里不记录“注入到哪个槽位”：槽位名与执行器的取值坐标不是同一套坐标
    （同一个头既能按名字取、也能按下标取），按名字标注会漏掉按坐标访问的同一次
    取值。受保护位置一律由执行器按**实际内容**反查真实结构坐标得出——注入值合并
    进请求头／查询参数之后本就在那些来源里，回显到响应同样能被反查出来。
    """

    headers: list[tuple[str, str]] = field(default_factory=list)
    query: list[tuple[str, str]] = field(default_factory=list)
    secret_values: list[str] = field(default_factory=list)
    profile_version_id: uuid.UUID | None = None
    credential_set_id: uuid.UUID | None = None
    credential_epoch: int = 0


@dataclass(frozen=True)
class LeaseGuard:
    """一次性授权消费的租约前提：工作项此刻仍由本次领取持有。

    授权消费发生在最终发送校验之前。如果只看“这行授权还没用过”，一个已经丢掉
    租约、工作项已被别人接管的旧执行者仍能把一次性授权消费掉，真正持有工作项的
    新执行者反而无授权可用。消费必须与租约条件写在同一条 UPDATE 里。
    """

    job_id: uuid.UUID
    worker_id: str
    fencing_token: int


def parse_auth_slot(slot: str) -> tuple[str, str]:
    kind, _, name = slot.partition(".")
    if kind not in _SLOT_KINDS or not name:
        raise CredentialError("auth_slot_invalid", f"认证槽位格式无效：{slot}（应为 header.X 或 query.x）")
    return kind, name


def store_secret(
    session: Session, settings: Settings, *, workspace_id: uuid.UUID, project_id: uuid.UUID, name: str, value: str
) -> Secret:
    key = settings.load_secret_key()
    secret = Secret(workspace_id=workspace_id, project_id=project_id, name=name)
    session.add(secret)
    session.flush()
    session.add(
        SecretVersion(
            workspace_id=workspace_id,
            project_id=project_id,
            secret_id=secret.id,
            version=1,
            encrypted_value=encrypt_secret(key, value),
        )
    )
    session.flush()
    return secret


def rotate_secret(
    session: Session, settings: Settings, secret: Secret, value: str
) -> SecretVersion:
    key = settings.load_secret_key()
    current = session.scalar(
        select(func.max(SecretVersion.version)).where(SecretVersion.secret_id == secret.id)
    )
    version = SecretVersion(
        workspace_id=secret.workspace_id,
        project_id=secret.project_id,
        secret_id=secret.id,
        version=(current or 0) + 1,
        encrypted_value=encrypt_secret(key, value),
    )
    session.add(version)
    session.flush()
    return version


def save_profile(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    name: str,
    allowed_targets: list[str],
    allowed_auth_slots: list[str],
) -> CredentialProfile:
    try:
        targets = [normalize_origin(item) for item in allowed_targets]
    except Exception as error:  # noqa: BLE001 - 统一转成配置错误
        raise CredentialError("invalid_target", f"允许目标格式无效：{error}") from error
    for slot in allowed_auth_slots:
        parse_auth_slot(slot)
    profile = CredentialProfile(
        workspace_id=workspace_id,
        project_id=project_id,
        environment_id=environment_id,
        name=name,
        allowed_targets=targets,
        allowed_auth_slots=list(allowed_auth_slots),
    )
    session.add(profile)
    session.flush()
    return profile


def save_profile_version(session: Session, profile: CredentialProfile, config: dict) -> CredentialProfileVersion:
    current = session.scalar(
        select(func.max(CredentialProfileVersion.version)).where(
            CredentialProfileVersion.profile_id == profile.id
        )
    )
    version = CredentialProfileVersion(
        workspace_id=profile.workspace_id,
        project_id=profile.project_id,
        profile_id=profile.id,
        version=(current or 0) + 1,
        config=config,
    )
    session.add(version)
    session.flush()
    return version


def activate_set(
    session: Session,
    *,
    profile: CredentialProfile,
    slots: dict[str, uuid.UUID],
    expires_at: datetime | None = None,
) -> CredentialSet:
    """原子切换当前凭证集合：新集合与 epoch 同事务生效，不混用新旧凭证。"""
    for slot, secret_version_id in slots.items():
        parse_auth_slot(slot)
        owner = session.scalar(
            select(SecretVersion).where(
                SecretVersion.id == secret_version_id,
                SecretVersion.project_id == profile.project_id,
            )
        )
        if owner is None:
            raise CredentialError("secret_not_in_project", "秘密版本不存在或不属于本项目。")

    profile.current_epoch += 1
    credential_set = CredentialSet(
        workspace_id=profile.workspace_id,
        project_id=profile.project_id,
        profile_id=profile.id,
        epoch=profile.current_epoch,
        expires_at=expires_at,
    )
    session.add(credential_set)
    session.flush()
    for slot, secret_version_id in slots.items():
        session.add(
            CredentialSetSecretVersion(
                workspace_id=profile.workspace_id,
                project_id=profile.project_id,
                credential_set_id=credential_set.id,
                secret_version_id=secret_version_id,
                auth_slot=slot,
            )
        )
    profile.current_set_id = credential_set.id
    profile.status = "available"
    session.flush()
    return credential_set


def create_use_grant(
    session: Session,
    *,
    profile: CredentialProfile,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    grant_type: str,
    case_version_id: uuid.UUID | None = None,
    debug_snapshot_hash: str | None = None,
    allowed_targets: list[str] | None = None,
    allowed_auth_slots: list[str] | None = None,
    allowed_inputs: dict | None = None,
    expires_at: datetime | None = None,
) -> CredentialUseGrant:
    """建立凭证用途授权。

    授权只能比身份声明的范围更窄：允许槽位、允许目标都必须是身份配置的子集，
    否则管理员可以借授权的名义把凭证注入到身份从未声明的槽位或目标上。
    用例版本或一次性快照摘要必须给出其一，不能以“两者都为空”代表任意草稿。

    输入绑定：签发时冻结当时生效的项目／环境普通变量摘要。请求里的 `{{...}}`
    由这些变量解析，同一次版本可以因变量不同而指向完全不同的请求；不绑定输入，
    授权就只约束了模板文本，而非真正要发的请求。
    """
    if allowed_inputs:
        # 收下未实现的约束再忽略，等同于向管理员承诺了一个不存在的保护。
        raise CredentialError(
            "grant_input_contract_unsupported",
            "本版本不支持按输入白名单签发授权；授权已按签发时的变量输入冻结，"
            "变量变化需要重新授权。",
        )

    if grant_type == "case_version":
        if case_version_id is None or debug_snapshot_hash is not None:
            raise CredentialError(
                "grant_target_invalid", "按用例版本授权的必须且只能提供 case_version_id。"
            )
    elif grant_type == "debug_snapshot":
        if not debug_snapshot_hash or case_version_id is not None:
            raise CredentialError(
                "grant_target_invalid", "按临时快照授权的必须且只能提供 debug_snapshot_hash。"
            )
    else:
        raise CredentialError("grant_type_invalid", f"未知授权类型：{grant_type}")

    if environment_id != profile.environment_id:
        raise CredentialError("grant_environment_mismatch", "授权环境与身份配置的环境不一致。")

    try:
        targets = [normalize_origin(item) for item in (allowed_targets or [])]
    except Exception as error:  # noqa: BLE001 - 统一转成配置错误
        raise CredentialError("invalid_target", f"允许目标格式无效：{error}") from error
    profile_targets = set(profile.allowed_targets or [])
    if targets and profile_targets and not set(targets) <= profile_targets:
        raise CredentialError("grant_scope_wider_than_profile", "授权的允许目标超出身份声明范围。")

    slots = list(allowed_auth_slots or [])
    for slot in slots:
        parse_auth_slot(slot)
    if slots and not set(slots) <= set(profile.allowed_auth_slots or []):
        raise CredentialError("grant_scope_wider_than_profile", "授权的认证槽位超出身份声明范围。")

    if case_version_id is not None:
        version = session.scalar(
            select(CaseVersion).where(
                CaseVersion.id == case_version_id,
                CaseVersion.project_id == profile.project_id,
            )
        )
        if version is None:
            raise CredentialError("case_version_not_found", "用例版本不存在或不属于本项目。")

    environment = session.get(Environment, environment_id)
    if environment is None:
        raise CredentialError("grant_environment_mismatch", "授权环境不存在。")
    frozen_inputs = environment_input_digest(session, environment)

    grant = CredentialUseGrant(
        workspace_id=profile.workspace_id,
        project_id=profile.project_id,
        profile_id=profile.id,
        environment_id=environment_id,
        case_version_id=case_version_id,
        debug_snapshot_hash=debug_snapshot_hash,
        grant_type=grant_type,
        principal_id=principal_id,
        allowed_targets=targets,
        allowed_auth_slots=slots,
        allowed_inputs={},
        input_digest=frozen_inputs,
        expires_at=expires_at,
    )
    session.add(grant)
    session.flush()
    return grant


def revoke_use_grant(session: Session, grant: CredentialUseGrant) -> None:
    grant.status = "revoked"
    session.flush()


def _find_grant(
    session: Session,
    profile: CredentialProfile,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    case_version_id: uuid.UUID | None,
    debug_snapshot_hash: str | None,
) -> CredentialUseGrant:
    """按运行的**目标类型**逐项匹配授权，不做“等于传入值”的宽松比较。

    已发布版本与调试快照是两种互不相通的授权：各自的定位字段只有一个非空。
    若只比较“等于传入值”，调试路径传入空摘要就会命中 `debug_snapshot_hash IS
    NULL` 的已发布版本授权，等于让任意调试请求借用为固定版本签发的凭证；反过来
    缺少 grant_type 过滤，任意一条空摘要授权也能被命中。因此这里先按目标类型选
    分支，再在该分支内比较自己那一列。
    """
    now = datetime.now(UTC)
    if case_version_id is not None:
        conditions = (
            CredentialUseGrant.grant_type == "case_version",
            CredentialUseGrant.case_version_id == case_version_id,
        )
    elif debug_snapshot_hash is not None:
        conditions = (
            CredentialUseGrant.grant_type == "debug_snapshot",
            CredentialUseGrant.debug_snapshot_hash == debug_snapshot_hash,
        )
    else:
        # 调试运行没有可核对的快照摘要（例如冻结摘要之前入队的旧记录）。这不能
        # 退化成“匹配任意空摘要授权”，只能按未授权处理。
        raise CredentialError(
            "credential_not_granted", "本次运行没有可核对的凭证授权目标，已拒绝使用凭证。"
        )
    grant = session.scalar(
        select(CredentialUseGrant).where(
            CredentialUseGrant.profile_id == profile.id,
            CredentialUseGrant.environment_id == environment_id,
            *conditions,
            CredentialUseGrant.principal_id == principal_id,
            CredentialUseGrant.status == "active",
        )
    )
    if grant is None:
        raise CredentialError("credential_not_granted", "当前身份未获授权使用该环境的凭证。")
    if grant.expires_at is not None and grant.expires_at <= now:
        raise CredentialError("credential_grant_expired", "凭证用途授权已过期。")
    return grant


def _require_bound_inputs(
    session: Session,
    grant: CredentialUseGrant,
    environment,
    frozen_input_digest: str | None = None,
) -> None:
    """授权必须绑定到**本次运行真正使用的**变量输入。

    版本模板里的 `{{...}}` 由项目／环境普通变量解析，同一份版本可以因变量不同而
    指向完全不同的请求，所以签发时要把那批输入摘要冻进授权。

    校验对象必须是“本次运行实际取用的那一份”。运行创建时会把当时的合并变量冻进
    快照，执行阶段准备请求用的正是这份快照；若这里改去比对**当前**环境变量，就会
    出现一条明确的绕过路径：按 A 签发授权 → 把变量改成 B 并在此刻入队（快照冻的是
    B）→ 再把变量改回 A。此时“当前值”等于签发时的 A，检查通过，而实际解析并发出
    的请求用的是快照里的 B——被批准的输入与真正发送的输入不是同一份。

    `frozen_input_digest` 由调用方从运行快照算出。额外保留“当前变量也一致”的保守
    判断：快照与当前值同时匹配才放行，任意一侧变化都拒绝。两条都查不会放宽任何
    限制，只是把“变量后来又被改回原样”这种下游状态也一并拦掉。
    """
    if grant.input_digest is None:
        raise CredentialError(
            "credential_inputs_unbound",
            "该凭证授权未绑定变量输入，请重新签发授权后再执行。",
        )
    if frozen_input_digest is not None and grant.input_digest != frozen_input_digest:
        raise CredentialError(
            "credential_inputs_changed",
            "本次运行冻结的变量输入与授权签发时的不一致（变量在入队与执行之间被改过），"
            "请按当前输入重新授权后再执行。",
        )
    if grant.input_digest != environment_input_digest(session, environment):
        raise CredentialError(
            "credential_inputs_changed",
            "项目／环境普通变量在授权之后发生了变化，请重新授权后再执行。",
        )


def _consume_debug_grant(
    session: Session, grant: CredentialUseGrant, lease_guard: LeaseGuard | None = None
) -> None:
    """一次性消费调试授权：条件更新，并发下至多成功一次。

    先读 used_at 再赋值写回会让两个并发运行各自读到“未使用”，然后都写入，同一份
    一次性授权被用两次。把判断与标记合并成一条带条件的 UPDATE，第二次更新在行锁
    释放后重新判断条件，命中零行即判定已消费。

    租约条件写进同一条 UPDATE：已经丢掉租约的旧执行者不得消费授权，否则真正持有
    工作项的新执行者会面对一份“已被用掉”的一次性授权。零行时再区分“已被消费”与
    “本此领取已失效”，两者的处置完全不同。
    """
    params: dict[str, str] = {"grant": str(grant.id)}
    guard_sql = ""
    if lease_guard is not None:
        guard_sql = (
            " AND EXISTS (SELECT 1 FROM app.jobs j WHERE j.id = :job AND j.state = 'leased' "
            "AND j.leased_by = :worker AND j.fencing_token = :token AND j.lease_until > now())"
        )
        params |= {
            "job": str(lease_guard.job_id),
            "worker": lease_guard.worker_id,
            "token": str(lease_guard.fencing_token),
        }
    consumed = session.execute(
        text(
            "UPDATE app.credential_use_grants SET used_at = now() "
            "WHERE id = :grant AND used_at IS NULL" + guard_sql + " RETURNING id"
        ),
        params,
    ).first()
    if consumed is None:
        if lease_guard is not None and not _lease_still_held(session, lease_guard):
            raise CredentialError(
                "credential_lease_lost", "本次领取已失效，未消费凭证授权。"
            )
        raise CredentialError("credential_grant_used", "一次性凭证授权已使用。")


def _lease_still_held(session: Session, lease_guard: LeaseGuard) -> bool:
    """工作项是否仍由这次领取持有；用于区分“授权已用”与“领取已失效”。"""
    row = session.execute(
        text(
            "SELECT 1 FROM app.jobs WHERE id = :job AND state = 'leased' "
            "AND leased_by = :worker AND fencing_token = :token AND lease_until > now()"
        ),
        {
            "job": str(lease_guard.job_id),
            "worker": lease_guard.worker_id,
            "token": str(lease_guard.fencing_token),
        },
    ).first()
    return row is not None


def _auth_prefixes(version: CredentialProfileVersion | None) -> dict[str, str]:
    """身份最新版本声明的“槽位 → 值前缀”。

    界面上的认证方案把 Bearer Token 表达成「放在 Authorization、值前加 `Bearer `」。
    这份声明存在配置快照里，注入时必须真的用上：只把秘密原文塞进请求头，目标收到的
    Authorization 就不再是声明的那个方案——表单上写着 Bearer，发出去却是裸值，直到
    被真实服务拒绝才暴露。槽位按大小写不敏感比对（HTTP 头名本就不区分大小写）。

    没有声明（早期身份或只用查询参数原样注入）返回空表，不报错；声明存在但形状不对
    则显式拒绝：那说明配置写坏了，静默当成“没有前缀”会重演上面那个“配了却没生效”。
    """
    if version is None:
        return {}
    config = version.config
    if not isinstance(config, dict):
        raise CredentialError("credential_config_invalid", "身份配置格式无效。")
    locations = config.get("auth_locations")
    if locations is None:
        return {}
    if not isinstance(locations, list):
        raise CredentialError("credential_config_invalid", "身份配置的认证位置格式无效。")
    prefixes: dict[str, str] = {}
    for item in locations:
        if not isinstance(item, dict):
            raise CredentialError("credential_config_invalid", "身份配置的认证位置格式无效。")
        slot = item.get("slot")
        if not isinstance(slot, str) or not slot:
            raise CredentialError("credential_config_invalid", "身份配置的认证位置缺少槽位。")
        prefix = item.get("prefix", "")
        if not isinstance(prefix, str):
            raise CredentialError("credential_config_invalid", "身份配置的值前缀格式无效。")
        prefixes[slot.lower()] = prefix
    return prefixes


def resolve_injection(
    session: Session,
    settings: Settings,
    *,
    environment,
    principal_id: uuid.UUID,
    case_version_id: uuid.UUID | None,
    debug_snapshot_hash: str | None,
    target_origin: str,
    frozen_input_digest: str | None = None,
    lease_guard: LeaseGuard | None = None,
) -> Injection:
    """按环境解析认证注入；没有配置身份时返回空注入，而不是报错。

    `frozen_input_digest` 是本次运行快照里那批变量的摘要（准备请求实际用的输入）。
    授权绑定校验以它为准，见 `_require_bound_inputs`。
    """
    profile = session.scalar(
        select(CredentialProfile).where(
            CredentialProfile.environment_id == environment.id,
            CredentialProfile.status == "available",
        )
    )
    if profile is None:
        return Injection()

    if profile.allowed_targets and target_origin not in set(profile.allowed_targets):
        raise CredentialError("credential_target_mismatch", "该身份的允许目标不包含本次目标。")

    grant = _find_grant(
        session, profile, environment.id, principal_id, case_version_id, debug_snapshot_hash
    )
    _require_bound_inputs(session, grant, environment, frozen_input_digest)
    if grant.allowed_targets and target_origin not in set(grant.allowed_targets):
        raise CredentialError("credential_target_mismatch", "凭证授权的允许目标不包含本次目标。")

    if profile.current_set_id is None:
        raise CredentialError("credential_unconfigured", "该身份尚未配置凭证集合。")
    credential_set = session.get(CredentialSet, profile.current_set_id)
    if credential_set is None or credential_set.status != "active":
        raise CredentialError("credential_unavailable", "当前凭证集合不可用。")
    now = datetime.now(UTC)
    if credential_set.expires_at is not None and credential_set.expires_at <= now:
        raise CredentialError("credential_expired", "当前凭证集合已过期，请更新。")

    # 授权只能在身份声明范围内收窄，不能扩大。
    allowed_slots = set(profile.allowed_auth_slots or [])
    if grant.allowed_auth_slots:
        allowed_slots &= set(grant.allowed_auth_slots)

    key = settings.load_secret_key()
    version = session.scalar(
        select(CredentialProfileVersion)
        .where(CredentialProfileVersion.profile_id == profile.id)
        .order_by(CredentialProfileVersion.version.desc())
        .limit(1)
    )
    prefixes = _auth_prefixes(version)
    injection = Injection(
        profile_version_id=version.id if version is not None else None,
        credential_set_id=credential_set.id,
        credential_epoch=credential_set.epoch,
    )
    bindings = session.scalars(
        select(CredentialSetSecretVersion).where(
            CredentialSetSecretVersion.credential_set_id == credential_set.id
        )
    )
    for binding in bindings:
        if binding.auth_slot not in allowed_slots:
            raise CredentialError(
                "credential_slot_not_allowed", f"认证槽位 {binding.auth_slot} 未被授权使用。"
            )
        secret_version = session.get(SecretVersion, binding.secret_version_id)
        if secret_version is None:
            raise CredentialError("credential_unavailable", "凭证集合引用的秘密版本缺失。")
        plaintext = decrypt_secret(key, secret_version.encrypted_value)
        kind, name = parse_auth_slot(binding.auth_slot)
        # 值前缀是身份声明的一部分：Bearer 方案要发的是 “Bearer <token>” 而不是裸 token。
        value = prefixes.get(binding.auth_slot.lower(), "") + plaintext
        if kind == "header":
            injection.headers.append((name, value))
        else:
            injection.query.append((name, value))
        # 只登记秘密本身；受保护坐标由执行器按内容反查（含注入位置与目标回显），
        # 不用这里的槽位名去构造坐标——那是与取值不同的一套坐标系。登记秘密原文即可：
        # 反查是按“包含”判定，带前缀的注入值与目标回显都能被同一条秘密命中。
        injection.secret_values.append(plaintext)

    if grant.grant_type == "debug_snapshot":
        # 一次性授权必须原子消费：判断“是否已用”和标记“已用”不能分成两步，
        # 并且必须与本次领取的租约同条件，否则丢租约的旧执行者会把它烧掉。
        _consume_debug_grant(session, grant, lease_guard)
    return injection


def recheck_grant_authority(
    session: Session,
    injection: Injection,
    *,
    environment,
    principal_id: uuid.UUID,
    case_version_id: uuid.UUID | None,
    debug_snapshot_hash: str | None,
    target_origin: str,
    frozen_input_digest: str | None = None,
) -> None:
    """发送前按当前状态复核凭证授权仍然有效；本次运行没有注入凭证时不做任何事。

    注入发生在发送之前的若干步骤里，期间授权可能被撤销、过期，身份配置可能被停用，
    变量也可能被改掉。只读复核能拦住这些情况，而**不再消费一次性授权**——重新消费
    会让同一份授权被算两次，把还能用的授权判成已用。

    `frozen_input_digest` 是运行快照里那批变量的摘要：绑定校验必须落在“本次真正会
    发送的输入”上，而不是碰巧等于签发值的当前变量。
    """
    if injection.profile_version_id is None:
        return

    profile = session.scalar(
        select(CredentialProfile).where(
            CredentialProfile.environment_id == environment.id,
            CredentialProfile.status == "available",
        )
    )
    if profile is None:
        raise CredentialError("credential_unavailable", "身份配置已不可用，已阻止执行。")
    if profile.allowed_targets and target_origin not in set(profile.allowed_targets):
        raise CredentialError("credential_target_mismatch", "该身份的允许目标不包含本次目标。")

    grant = _find_grant(
        session, profile, environment.id, principal_id, case_version_id, debug_snapshot_hash
    )
    _require_bound_inputs(session, grant, environment, frozen_input_digest)
    if grant.allowed_targets and target_origin not in set(grant.allowed_targets):
        raise CredentialError("credential_target_mismatch", "凭证授权的允许目标不包含本次目标。")
