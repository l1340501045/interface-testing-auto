"""配置构造测试：验证特殊字符密码经 URL.create 严格编码，且 Alembic 路径不触发插值。"""
from __future__ import annotations

import base64

import pytest
from sqlalchemy.engine import URL

from app.config import Settings
from app.kernel.target_policy import TargetPolicyError


def _settings_with_password(password: str) -> Settings:
    return Settings(
        pghost="db",
        pgdatabase="interface_testing",
        pguser="app_runtime",
        pgpassword=password,
        migrator_user="interface_testing",
        migrator_password=password,
    )


def test_url_create_encodes_special_chars_roundtrip() -> None:
    # @ / : % 等字符若直接拼字符串会改变 URL 解析或触发 % 插值。
    password = "p@ss/w:ord%40x"
    settings = _settings_with_password(password)

    url: URL = settings.database_url()
    assert url.password == password, "URL 组件必须原样保留密码，不做预编码"

    rendered = url.render_as_string(hide_password=False)
    assert "p@ss/w:ord%40x" not in rendered, "原始特殊字符不应裸露在 URL 字符串中"


def test_migrator_url_uses_same_encoding() -> None:
    password = "mig@rator/p%ass:word"
    settings = _settings_with_password(password)
    url: URL = settings.migrator_url()

    assert url.username == "interface_testing"
    assert url.password == password
    # 反解析必须能精确还原密码。
    assert URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=url.database,
    ).password == password


def test_empty_password_omitted() -> None:
    url: URL = Settings(pgpassword="").database_url()
    assert url.password is None or url.password == ""


def _fernet_shaped_key() -> str:
    return base64.urlsafe_b64encode(b"k" * 32).decode()


def test_secret_key_accepts_fernet_format() -> None:
    settings = Settings(secret_key=_fernet_shaped_key())
    assert settings.load_secret_key() == _fernet_shaped_key().encode()


def test_secret_key_rejects_non_fernet_shape() -> None:
    """`token_urlsafe(32)` 之类的普通随机串不是 Fernet 密钥，必须在配置边界拒绝。

    历史上开发口令与加密主密钥共用一个生成器，结果要到保存第一份凭证时才在
    请求里抛 ValueError；这里锁定“早失败、说清楚原因”的行为。
    """
    bad = "RmZ8Puy8sqvSd4ClfHI3-0zc51PU8MBQlpkFMN-OdXI"  # 43 字符，非 4 的倍数
    with pytest.raises(RuntimeError, match="base64"):
        Settings(secret_key=bad).load_secret_key()


def test_secret_key_rejects_wrong_length() -> None:
    short = base64.urlsafe_b64encode(b"only16bytes!!!!!").decode()
    with pytest.raises(RuntimeError, match="32 字节"):
        Settings(secret_key=short).load_secret_key()


def test_secret_key_missing_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="缺少主密钥"):
        Settings(secret_key="", secret_key_path="/nonexistent/key").load_secret_key()


def test_required_sources_include_the_actual_database_host() -> None:
    """必须确认的来源里要带上**实际使用的**数据库主机，而不是写死默认的 `db`。

    `PGHOST` 换掉之后，数据库当前指向的地址仍是平台基础设施；只认名字 `db` 会让这台
    数据库的 IP 直连失去保护。
    """
    settings = Settings(
        pghost="pg.internal",
        platform_required_hosts="intranet-api, intranet-api ,web",
    )

    guard = settings.target_guard(["http://echo:8080"])

    assert guard.required_hosts == [
        "api",
        "db",
        "host.docker.internal",
        "intranet-api",
        "pg.internal",
        "web",
    ]
    # 这些来源同时是主机名层的禁区，写成目标直接拒绝。
    for host in ("pg.internal", "intranet-api"):
        with pytest.raises(TargetPolicyError, match="平台基础设施"):
            guard.authorize_url(f"http://{host}:8080/echo")


def test_executor_is_required_only_when_the_deployment_declares_it() -> None:
    """执行器（worker）是部署可选的服务：声明了就必须确认，没声明就不是必需来源。

    `make up` 不启动 worker，干净克隆的检查在进程内调用同一份执行内核，所以它不能像
    api／db 那样被无条件要求——否则一个当前部署没有的名字会让所有发送被拒。运行它的
    部署把它写进 `PLATFORM_REQUIRED_HOSTS`，与其它启用来源同一条路；执行器进程启动时
    自查这条声明（见 `app/worker.py`），所以“跑着执行器却没声明”在部署里无法成立。
    """
    stopped = Settings(pghost="db", platform_required_hosts="web")
    assert stopped.target_guard([]).required_hosts == ["api", "db", "host.docker.internal", "web"]

    running = Settings(pghost="db", platform_required_hosts="web,worker")
    guard = running.target_guard([])
    assert guard.required_hosts == ["api", "db", "host.docker.internal", "web", "worker"]
    assert guard.requires("worker") is True
    assert guard.requires("WORKER.") is True, "主机名比较规则与名单一致（大小写、尾点）"


def test_required_sources_are_not_taken_from_the_request() -> None:
    """必需来源只来自部署配置：执行池白名单覆盖不了它，也不会被目标内容改写。"""
    settings = Settings(pghost="db", platform_required_hosts="")

    guard = settings.target_guard(["http://echo:8080"])

    assert guard.required_hosts == ["api", "db", "host.docker.internal"]
    assert guard.allowed_origins == ["http://echo:8080"]


def test_invalid_required_source_fails_at_configuration_time() -> None:
    """写错的必需来源在构造守卫时就报配置错误，而不是静默缩小禁区。"""
    settings = Settings(platform_required_hosts="intranet-api:8000")

    with pytest.raises(TargetPolicyError, match="来源配置无效"):
        settings.target_guard(["http://echo:8080"])
