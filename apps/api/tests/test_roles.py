"""角色初始化：语句构造安全性与受限属性保证。

重点不是“能拼出 SQL”，而是三件会被真实环境放大的事：密码里的特殊字符不得破坏
语句结构、已存在角色也必须回到受限属性、密码必须按原值使用。
"""
from __future__ import annotations

import pytest
from psycopg import sql

from app.config import Settings
from app.roles import BROKER_ROLE, RUNTIME_ROLE, build_broker_statements, build_role_statements


def _render(statements: list[sql.Composed]) -> str:
    return "\n".join(statement.as_string(None) for statement in statements)


@pytest.mark.parametrize(
    "password",
    ["pa$$word", "o'brien", "a%b@c:d/e", "back\\slash", "quote'and$$both"],
)
def test_no_dollar_quoted_block(password: str) -> None:
    rendered = _render(build_role_statements(role_exists=False, password=password))
    assert "DO $" not in rendered, "不得使用 DO $$ 匿名块，密码中的 $$ 会提前结束块"
    assert "$$" not in rendered.replace(password.replace("'", "''"), "")


@pytest.mark.parametrize(
    "password,escaped",
    [
        ("o'brien", "'o''brien'"),
        ("pa$$word", "'pa$$word'"),
        ("quote'and$$both", "'quote''and$$both'"),
    ],
)
def test_password_escaped_as_sql_literal(password: str, escaped: str) -> None:
    rendered = _render(build_role_statements(role_exists=False, password=password))
    assert escaped in rendered, "密码应作为标准字符串字面量编码，单引号成对转义"


def test_existing_role_uses_alter_not_create() -> None:
    rendered = _render(build_role_statements(role_exists=True, password="secret$$1"))
    assert "ALTER ROLE" in rendered
    assert "CREATE ROLE" not in rendered
    assert "NOBYPASSRLS" in rendered


def test_no_password_creates_nologin_role() -> None:
    rendered = _render(build_role_statements(role_exists=False, password=""))
    assert "NOLOGIN" in rendered
    assert "NOBYPASSRLS" in rendered


# —— 受限属性：新建与已存在两条路径都必须收紧 ——

_REQUIRED_ATTRS = ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOINHERIT")


def _tightening_statement(role: str) -> str:
    # sql.Identifier 会给角色名加双引号，断言按实际下发文本编写。
    return f'ALTER ROLE "{role}" WITH'


@pytest.mark.parametrize("role_exists", [True, False])
def test_runtime_role_is_hardened_on_both_paths(role_exists: bool) -> None:
    """已存在角色也要回到受限属性。

    只给新建路径加限制，会把一个此前是超级用户或可建库的旧角色原样留下，而初始化
    仍然报告“非超级用户”。两条路径都必须无条件下发收紧语句。
    """
    rendered = _render(build_role_statements(role_exists=role_exists, password="pw"))
    for attribute in (*_REQUIRED_ATTRS, "NOBYPASSRLS"):
        assert attribute in rendered, f"角色属性缺少 {attribute}（role_exists={role_exists}）"
    assert _tightening_statement(RUNTIME_ROLE) in rendered


@pytest.mark.parametrize("role_exists", [True, False])
def test_broker_role_is_hardened_on_both_paths(role_exists: bool) -> None:
    rendered = _render(build_broker_statements(role_exists=role_exists))
    for attribute in _REQUIRED_ATTRS:
        assert attribute in rendered, f"领取代理属性缺少 {attribute}（role_exists={role_exists}）"
    # 跨租户领取是该角色的既定职责，BYPASSRLS 显式设置而不是从创建语句沿用。
    assert "BYPASSRLS" in rendered
    assert _tightening_statement(BROKER_ROLE) in rendered


# —— 密码必须按原值使用 ——


class _RecordingCursor:
    def __init__(self, executed: list) -> None:
        self._executed = executed

    def execute(self, statement) -> None:
        self._executed.append(statement)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _RecordingConnection:
    """只记录角色初始化实际下发的语句，不连接真实数据库。"""

    def __init__(self) -> None:
        self.executed: list = []

    def execute(self, query, params=None):
        return self

    def fetchone(self):
        # 角色尚不存在，走新建分支，密码会出现在语句里便于核对。
        return None

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.executed)

    def commit(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_password_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """首尾空格属于密码本身，初始化不得改写。

    用 strip() 会把数据库里的密码变成与 .env 不同的值，直到应用连接时才以认证失败
    暴露；这里直接核对实际下发的语句包含未经加工的原始秘密。
    """
    from app import roles

    connection = _RecordingConnection()
    monkeypatch.setattr(roles.psycopg, "connect", lambda **kwargs: connection)
    secret = "  spaced secret  "
    settings = Settings(pghost="db", pgdatabase="db", pgpassword=secret)

    roles.init_runtime_role(settings)

    assert connection.executed, "应至少下发一条角色初始化语句"
    rendered = _render(connection.executed)
    assert secret in rendered, "密码必须按原值下发，不能被 strip 改写"


def test_missing_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置密码时明确报错，而不是建出一个永远无法登录的角色。"""
    from app import roles

    monkeypatch.setattr(
        roles.psycopg, "connect", lambda **kwargs: pytest.fail("缺密码时不应连接数据库")
    )
    settings = Settings(pghost="db", pgdatabase="db", pgpassword="")
    with pytest.raises(RuntimeError, match="PGPASSWORD"):
        roles.init_runtime_role(settings)
