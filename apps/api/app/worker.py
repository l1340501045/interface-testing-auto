"""独立执行 worker：领取工作项、续租、调用统一执行内核。

worker 与管理 API 同镜像不同入口，共用同一执行内核，不在 HTTP handler 里
执行长时间巡检。worker 只领取自身被授权的执行池；空闲时按退避轮询，
收到 SIGTERM／SIGINT 时结束当前一轮后退出，不会丢弃已写下的发送意图。
"""
from __future__ import annotations

import logging
import signal
import threading
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .db import bind_tenant, get_session_factory
from .kernel.target_policy import TargetPolicyError
from .services.executor import LEASE_HELD_CONDITION, JobClaim, claim_job, execute_claim

logger = logging.getLogger("app.worker")

_IDLE_MIN_SECONDS = 0.5
_IDLE_MAX_SECONDS = 5.0


class WorkerConfigError(RuntimeError):
    """worker 配置缺失，直接失败而不是静默空转。"""


def parse_pool_ids(raw: str) -> list[uuid.UUID]:
    """解析 worker 服务的执行池；格式错误立即报错，不猜测。"""
    pools: list[uuid.UUID] = []
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        try:
            pools.append(uuid.UUID(entry))
        except ValueError as error:
            raise WorkerConfigError(f"WORKER_POOL_IDS 含非法 UUID：{entry}") from error
    if not pools:
        raise WorkerConfigError(
            "未配置 WORKER_POOL_IDS：执行器必须显式声明所服务的执行池，"
            "请先创建执行池并把池 id 配置到 worker。"
        )
    return pools


def require_declared_executor(settings: Settings) -> str:
    """确认本部署声明了正在运行的执行器，返回它在网络中的名字。

    执行器是部署可选的服务：`make up` 只起平台本体，它由显式命令启动，干净克隆的验证
    流程在进程内调用同一份执行内核，不依赖它。守卫因此只要求**本部署实际启用**的名字
    解析成功（解析失败就拼不出完整禁区，而“解析不了就跳过”会让指向同一台主机的别名合法
    漏过去），执行器必须由部署声明（`PLATFORM_REQUIRED_HOSTS`）而不是被无条件要求。

    放宽不能变成缺口，所以守护点放在这里：**只要这个进程在跑，声明就必须在**。缺声明时
    明确失败并给出改法，不静默降级——否则一台正在运行的执行器既不在必需来源里，也没有
    任何地方会发现它没被算进禁区。比对走 `TargetGuard.requires`，与发送前那份名单同源。
    """
    host = settings.worker_host.strip() or "worker"
    try:
        declared = settings.target_guard([]).requires(host)
    except TargetPolicyError as error:
        raise WorkerConfigError(f"平台基础设施来源配置无效：{error}") from error
    if not declared:
        raise WorkerConfigError(
            f"本部署没有声明执行器 {host}：运行执行器时必须在部署配置 PLATFORM_REQUIRED_HOSTS "
            f"里写上它在网络中的名字（{host}），守卫才会在发送前把它解析到的地址算进禁区。"
            "不运行执行器的部署应保持为空，不要写一个解析不出来的名字。"
        )
    return host


def require_host_boundary_ready(settings: Settings) -> None:
    """确认本部署的**地址层**禁区现在就是完整的，不完整就不启动。

    每次发送前当然还会再确认一次（见 `TargetGuard.pinned_address`），但在启动时先确认
    一次，能把“这个部署提供不出必需的宿主入口”暴露成启动失败：否则一台持续空转的执行器
    要等到第一条运行被拒，操作者才从报告里反着推原因。

    必需来源里包含**容器到宿主机的入口**（`host.docker.internal`，由部署模板的
    `extra_hosts: host-gateway` 提供）：宿主自己的地址必须进禁区，指向它的域名与 IP 直连
    否则都能作为被测目标发出去。它解析不出来时这里直接失败，不降级为“跳过这个来源”。
    """
    try:
        settings.target_guard([]).ensure_infrastructure_confirmed()
    except TargetPolicyError as error:
        raise WorkerConfigError(
            f"宿主边界保护未就绪：{error}"
            "部署模板通过 compose.yaml 的 extra_hosts 提供 host.docker.internal:host-gateway "
            "入口（Linux 与 Docker Desktop 都适用）；自定义部署必须提供等价的宿主入口，"
            "或者用 PLATFORM_FORBIDDEN_ADDRESSES 显式声明宿主地址范围。"
        ) from error


@dataclass
class Heartbeat:
    """执行期间按间隔续租；旧租约令牌无法续期，看到即说明已被抢占。

    续租条件必须包含“当前租约仍未过期”。只匹配状态、持有者与 token 是不够的：
    进程暂停（长 GC、宿主机挂起、断点调试）到租约过期后，如果还没有别的 worker
    接管，一次心跳就能把旧租约重新延长，让一个本应重新领取的执行者继续通过发送前
    校验——租约的有效期形同虚设。过期只能靠重新领取（claim_job 会递增 fencing
    token），不能靠心跳复活。
    """

    session_factory: sessionmaker[Session]
    claim: JobClaim
    worker_id: str
    lease_seconds: int
    interval: float
    lost: threading.Event

    def _beat(self) -> None:
        session = self.session_factory()
        try:
            # 心跳事务需要租户上下文才能更新受 RLS 保护的 jobs；绑定后由
            # 会话状态在每个事务自动重放，不依赖手动 set_config。
            bind_tenant(session, self.claim.workspace_id)
            row = session.execute(
                text(
                    "UPDATE app.jobs SET lease_until = now() + make_interval(secs => :lease), "
                    "updated_at = now() "
                    "WHERE id = :job AND " + LEASE_HELD_CONDITION + " RETURNING id"
                ),
                {
                    "lease": self.lease_seconds,
                    "job": str(self.claim.job_id),
                    "worker": self.worker_id,
                    "token": self.claim.fencing_token,
                },
            ).first()
            session.commit()
            if row is None:
                self.lost.set()
        except Exception:  # noqa: BLE001 - 心跳失败不中断执行，由发送前校验兜底
            logger.warning("续租失败，运行 %s 将在发送前重新校验租约", self.claim.run_id)
            session.rollback()
        finally:
            session.close()

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self.interval):
            if self.lost.is_set():
                return
            self._beat()

    def __enter__(self) -> Heartbeat:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)


def _claim_once(
    session_factory: sessionmaker[Session], settings: Settings, pools: list[uuid.UUID]
) -> JobClaim | None:
    session = session_factory()
    try:
        claim = claim_job(session, settings.worker_id, pools, settings.lease_seconds)
        session.commit()
        return claim
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_forever(
    settings: Settings, session_factory: sessionmaker[Session], stop: threading.Event
) -> None:
    # 先确认“本部署声明了这个执行器”，再解析执行池：声明是部署级前置条件，
    # 缺了就不该起进程，也不该让操作者先被池授权的问题引到另一条路上。
    host = require_declared_executor(settings)
    # 再看地址层禁区此刻是否完整（必需来源能否解析）。同样属于部署级前置条件：
    # 保护没就绪就不该开始领工作项。
    require_host_boundary_ready(settings)
    pools = parse_pool_ids(settings.worker_pool_ids)
    logger.info(
        "执行器启动：worker=%s（%s），服务执行池数量=%d", settings.worker_id, host, len(pools)
    )
    idle = _IDLE_MIN_SECONDS
    while not stop.is_set():
        claim = _claim_once(session_factory, settings, pools)
        if claim is None:
            stop.wait(idle)
            idle = min(idle * 2, _IDLE_MAX_SECONDS)
            continue
        idle = _IDLE_MIN_SECONDS
        lost = threading.Event()
        with Heartbeat(
            session_factory=session_factory,
            claim=claim,
            worker_id=settings.worker_id,
            lease_seconds=settings.lease_seconds,
            interval=float(settings.lease_heartbeat_seconds),
            lost=lost,
        ):
            try:
                outcome = execute_claim(session_factory, settings, claim)
            except Exception:  # noqa: BLE001 - 单个工作项失败不得终止 worker
                logger.exception("运行 %s 执行失败，工作项将等待租约到期后被重新领取", claim.run_id)
                continue
        logger.info("运行 %s 结束：%s", claim.run_id, outcome)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    stop = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，当前工作项结束后退出", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        run_forever(settings, get_session_factory(), stop)
    except WorkerConfigError as error:
        logger.error("%s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
