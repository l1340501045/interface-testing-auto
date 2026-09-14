"""运行时配置：从环境变量读取，绝不硬编码秘密。"""
from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine import URL

if TYPE_CHECKING:
    # 只用于类型标注：配置层运行时不需要内核层，跨界连接发生在 `target_guard` 里。
    from .kernel.target_policy import TargetGuard


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _split_list(raw: str) -> list[str]:
    """逗号分隔的配置项：去空白、丢空项。"""
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    pghost: str = "db"
    pgdatabase: str = "interface_testing"
    pguser: str = "app_runtime"
    pgpassword: str = ""
    pgport: int = 5432

    migrator_user: str = "interface_testing"
    migrator_password: str = ""

    secret_key: str = ""
    secret_key_path: str = "/run/secrets/app_secret_key"

    session_ttl_seconds: int = 86400
    cookie_name: str = "interface_session"
    csrf_cookie_name: str = "interface_csrf"
    cookie_secure: bool = False
    login_max_attempts: int = 10
    login_window_seconds: int = 300

    worker_id: str = "worker-1"
    # 执行器在网络中的名字（Compose 里就是服务名 `worker`）。它只用于执行器启动时自查
    # “本部署声明了自己”：执行器是部署可选的服务，声明缺失就意味着没有守卫要求确认它。
    # 不是实例标识——多实例用 `worker_id` 区分。
    worker_host: str = "worker"
    worker_pool_ids: str = ""
    lease_seconds: int = 30
    lease_heartbeat_seconds: int = 10
    queue_deadline_seconds: int = 60
    business_deadline_seconds: int = 600
    cleanup_budget_ms: int = 60000
    http_connect_timeout: float = 5.0
    http_read_timeout: float = 15.0

    controlled_targets: str = "http://echo:8080,http://echo-alt:8080"
    allow_production_execution: bool = False
    # 平台基础设施禁区（可信部署配置），分为两类：
    # - `platform_forbidden_hosts` / `platform_forbidden_addresses`：静态禁止访问的
    #   名字与网段。基础名单覆盖当前 Compose 的 api／db／web／worker、容器到宿主机的
    #   入口与各平台兼容别名，这两项只能追加、不能删减。
    # - `platform_required_hosts`：当前部署**实际启用**、必须完整确认地址的来源。
    #   它们在发送前必须全部解析成功，否则这次发送被拒绝；缺失可选兼容别名不受影响。
    # 普通项目的目标白名单与它们无关，覆盖不了禁区；这些配置也不来自请求或项目白名单。
    platform_forbidden_hosts: str = ""
    platform_forbidden_addresses: str = ""
    platform_required_hosts: str = ""

    def target_guard(self, allowed_origins: list[str]) -> TargetGuard:
        """按可信部署配置构造目标守卫。

        “禁区由哪些配置定义”只有这一处：两个调用点（创建运行、发送前复核）都从这里取，
        不会出现一个地方带上了部署禁区、另一个地方忘了带这种半个修复。白名单来自运行
        上下文（执行池），禁区来自本配置，两者不可能互相覆盖。

        必需的来源里额外带上**实际使用的数据库主机**（`pghost`）：它不一定是默认的
        `db`，写死在名单里迟早会和真实配置对不上。代码基线的 api／db（平台本体）由守卫
        无条件要求，这里不重复；`platform_forbidden_hosts` 声明的保护来源同样被守卫
        视为必需。执行器（worker）是部署可选的服务：`make up` 不启动它、干净克隆的检查
        流程也不运行它，所以它只能由部署声明（`platform_required_hosts`）加入必需来源，
        由执行器进程启动时自查，见 `app/worker.py`。检查只做名字形态与解析，不向任何
        来源发送请求。
        """
        # 运行时导入：类型标注走上面的 TYPE_CHECKING，这里才是真正的依赖。
        from .kernel.target_policy import TargetGuard

        return TargetGuard(
            allowed_origins,
            allow_production=self.allow_production_execution,
            platform_hosts=_split_list(self.platform_forbidden_hosts),
            platform_addresses=_split_list(self.platform_forbidden_addresses),
            required_hosts=[*_split_list(self.platform_required_hosts), self.pghost],
        )

    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.pguser,
            password=self.pgpassword or None,
            host=self.pghost,
            port=self.pgport,
            database=self.pgdatabase,
        )

    def migrator_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.migrator_user,
            password=self.migrator_password or None,
            host=self.pghost,
            port=self.pgport,
            database=self.pgdatabase,
        )

    def load_secret_key(self) -> bytes:
        """读取主密钥并校验格式；引导时生成，运行时不打印。

        主密钥必须正好是 32 字节的 urlsafe base64（Fernet 格式）。此处在配置边界
        校验，否则一个用 `token_urlsafe` 之类生成的普通随机串要到“保存第一份凭证”
        时才在请求里抛 `ValueError`，把配置错误伪装成运行时故障。
        """
        key = self.secret_key.strip()
        if not key:
            path = Path(self.secret_key_path)
            if path.exists():
                key = path.read_text().strip()
        if not key:
            raise RuntimeError("缺少主密钥：请先运行 bootstrap 引导命令生成")
        try:
            decoded = base64.urlsafe_b64decode(key.encode())
        except (binascii.Error, ValueError) as error:
            raise RuntimeError("主密钥不是合法的 base64，请重新生成 SECRET_KEY") from error
        if len(decoded) != 32:
            raise RuntimeError("主密钥解码后必须是 32 字节，请重新生成 SECRET_KEY")
        return key.encode()


def get_settings() -> Settings:
    return Settings(
        pghost=_env("PGHOST", "db"),
        pgdatabase=_env("PGDATABASE", "interface_testing"),
        pguser=_env("PGUSER", "app_runtime"),
        pgpassword=_env("PGPASSWORD", ""),
        pgport=int(_env("PGPORT", "5432")),
        migrator_user=_env("MIGRATOR_USER", "interface_testing"),
        migrator_password=_env("MIGRATOR_PASSWORD", ""),
        secret_key=_env("SECRET_KEY", ""),
        secret_key_path=_env("SECRET_KEY_PATH", "/run/secrets/app_secret_key"),
        session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", "86400")),
        cookie_name=_env("COOKIE_NAME", "interface_session"),
        csrf_cookie_name=_env("CSRF_COOKIE_NAME", "interface_csrf"),
        cookie_secure=_env("COOKIE_SECURE", "0") in ("1", "true", "True"),
        login_max_attempts=int(_env("LOGIN_MAX_ATTEMPTS", "10")),
        login_window_seconds=int(_env("LOGIN_WINDOW_SECONDS", "300")),
        worker_id=_env("WORKER_ID", "worker-1"),
        worker_host=_env("WORKER_HOST", "worker"),
        worker_pool_ids=_env("WORKER_POOL_IDS", ""),
        lease_seconds=int(_env("LEASE_SECONDS", "30")),
        lease_heartbeat_seconds=int(_env("LEASE_HEARTBEAT_SECONDS", "10")),
        queue_deadline_seconds=int(_env("QUEUE_DEADLINE_SECONDS", "60")),
        business_deadline_seconds=int(_env("BUSINESS_DEADLINE_SECONDS", "600")),
        cleanup_budget_ms=int(_env("CLEANUP_BUDGET_MS", "60000")),
        http_connect_timeout=float(_env("HTTP_CONNECT_TIMEOUT", "5.0")),
        http_read_timeout=float(_env("HTTP_READ_TIMEOUT", "15.0")),
        controlled_targets=_env("CONTROLLED_TARGETS", "http://echo:8080"),
        allow_production_execution=_env("ALLOW_PRODUCTION_EXECUTION", "0") in ("1", "true", "True"),
        platform_forbidden_hosts=_env("PLATFORM_FORBIDDEN_HOSTS", ""),
        platform_forbidden_addresses=_env("PLATFORM_FORBIDDEN_ADDRESSES", ""),
        platform_required_hosts=_env("PLATFORM_REQUIRED_HOSTS", ""),
    )
