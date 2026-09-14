"""真实数据库结构契约：FORCE RLS、中文表列注释与角色权责分离。

对应 SC-10。这里用 PostgreSQL 的 obj_description／col_description 直接读取
真实注释，不用全文搜索代替；注释准确性仍由人工复核，本测试只保证“存在且为中文”。
"""
from __future__ import annotations

import re

import psycopg
import pytest

from app.roles import BROKER_ROLE, RUNTIME_ROLE

pytestmark = pytest.mark.integration

_CJK = re.compile(r"[一-鿿]")


def _has_chinese(text: str | None) -> bool:
    return bool(text) and bool(_CJK.search(text))


def _tenant_tables(connection: psycopg.Connection) -> set[str]:
    """租户隔离表以是否带 workspace_id 列为准，与迁移中的表清单同源。

    不使用硬编码排除名单：会话表按用户而非工作空间归属，平台账号同样不带租户
    范围，它们本就不应受租户策略约束。
    """
    rows = connection.execute(
        "SELECT c.relname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid"
        " WHERE n.nspname = 'app' AND c.relkind = 'r'"
        "   AND a.attname = 'workspace_id' AND a.attnum > 0 AND NOT a.attisdropped"
    ).fetchall()
    return {name for (name,) in rows}


@pytest.fixture(scope="module")
def schema_connection(migrator_url: str):
    connection = psycopg.connect(migrator_url)
    try:
        yield connection
    finally:
        connection.close()


def test_every_business_table_has_chinese_comment(schema_connection: psycopg.Connection) -> None:
    rows = schema_connection.execute(
        "SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'app' AND c.relkind = 'r' ORDER BY c.relname"
    ).fetchall()
    assert rows, "app schema 中没有任何业务表"
    missing = [name for name, comment in rows if not _has_chinese(comment)]
    assert missing == [], f"以下业务表缺少中文 COMMENT：{missing}"


def test_every_column_has_chinese_comment(schema_connection: psycopg.Connection) -> None:
    rows = schema_connection.execute(
        "SELECT c.relname, a.attname, col_description(c.oid, a.attnum)"
        " FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid"
        " WHERE n.nspname = 'app' AND c.relkind = 'r'"
        "   AND a.attnum > 0 AND NOT a.attisdropped"
        " ORDER BY c.relname, a.attnum"
    ).fetchall()
    missing = [f"{table}.{column}" for table, column, comment in rows if not _has_chinese(comment)]
    assert missing == [], f"以下列缺少中文 COMMENT：{missing}"


def test_tenant_tables_force_row_level_security(
    schema_connection: psycopg.Connection,
) -> None:
    rows = schema_connection.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'app' AND c.relkind = 'r' ORDER BY c.relname"
    ).fetchall()
    tenant_tables = _tenant_tables(schema_connection)
    assert tenant_tables, "没有可用于租户隔离的业务表"
    not_forced = [
        name
        for name, enabled, forced in rows
        if name in tenant_tables and not (enabled and forced)
    ]
    assert not_forced == [], f"以下租户表未同时启用 FORCE ROW LEVEL SECURITY：{not_forced}"


def test_tenant_tables_are_owned_by_migration_role_not_runtime(
    schema_connection: psycopg.Connection,
) -> None:
    rows = schema_connection.execute(
        "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'app' AND c.relkind = 'r'"
    ).fetchall()
    owned_by_runtime = [name for name, owner in rows if owner == "app_runtime"]
    assert owned_by_runtime == [], f"运行角色不能拥有业务表：{owned_by_runtime}"


def test_broker_role_is_nologin_with_bypass_and_minimal_table_grants(
    schema_connection: psycopg.Connection,
) -> None:
    row = schema_connection.execute(
        "SELECT rolcanlogin, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'app_job_broker'"
    ).fetchone()
    assert row is not None, "缺少领取代理角色 app_job_broker"
    can_login, bypass, superuser = row
    assert can_login is False, "领取代理角色不可登录"
    assert bypass is True, "领取代理角色需要 BYPASSRLS 才能跨租户领取"
    assert superuser is False, "领取代理角色不得是超级用户"

    # 最小权限：只授予领取工作项所需的 jobs 表访问与 schema 使用，不含建表权。
    privileges = schema_connection.execute(
        "SELECT has_table_privilege('app_job_broker', 'app.jobs', 'SELECT'),"
        "       has_table_privilege('app_job_broker', 'app.jobs', 'UPDATE'),"
        "       has_table_privilege('app_job_broker', 'app.runs', 'SELECT'),"
        "       has_schema_privilege('app_job_broker', 'app', 'CREATE')"
    ).fetchone()
    assert privileges == (True, True, False, False), (
        "领取代理角色应只具备 app.jobs 的 SELECT/UPDATE 与 schema USAGE"
    )


def test_runtime_role_is_fully_hardened(
    schema_connection: psycopg.Connection,
) -> None:
    """运行角色必须能登录，但不能有任何提权或绕过隔离的能力。

    只检查 rolsuper 不够：可建库、可建角色、INHERIT 都能让运行角色绕开本应由
    迁移身份承担的能力。初始化对“已存在角色”同样下发收紧语句，这里按真实
    pg_roles 核对最终状态，而不是核对语句文本。
    """
    row = schema_connection.execute(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolbypassrls"
        " FROM pg_roles WHERE rolname = 'app_runtime'"
    ).fetchone()
    assert row is not None, "缺少运行时角色 app_runtime"
    can_login, superuser, createdb, createrole, inherit, bypass = row
    assert can_login is True, "运行角色必须能登录"
    assert (superuser, createdb, createrole, inherit, bypass) == (False,) * 5, (
        f"运行角色仍具备不应有的能力：super={superuser} createdb={createdb}"
        f" createrole={createrole} inherit={inherit} bypassrls={bypass}"
    )


def test_claim_function_is_security_definer_with_chinese_comment(
    schema_connection: psycopg.Connection,
) -> None:
    row = schema_connection.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), obj_description(p.oid, 'pg_proc')"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'app' AND p.proname = 'claim_job'"
    ).fetchone()
    assert row is not None, "缺少 app.claim_job 函数"
    is_definer, owner, comment = row
    assert is_definer is True, "跨租户领取必须通过 SECURITY DEFINER 收敛"
    assert owner == "app_job_broker", "函数属主必须是领取代理角色"
    assert _has_chinese(comment), "函数缺少中文 COMMENT"


def test_assertion_results_are_unique_per_attempt_and_phase(
    schema_connection: psycopg.Connection,
) -> None:
    """一次尝试里每条断言每阶段只允许一条结果。

    唯一约束是“报告不出现重复断言”的最后一道防线：应用层的重复写入即使再次
    出现，也会在这里被数据库拒绝，而不是悄悄变成页面上的两条相同记录。
    """
    row = schema_connection.execute(
        "SELECT conname, pg_get_constraintdef(oid), obj_description(oid, 'pg_constraint')"
        " FROM pg_constraint"
        " WHERE conrelid = 'app.assertion_results'::regclass AND contype = 'u'"
    ).fetchone()
    assert row is not None, "assertion_results 缺少唯一约束"
    name, definition, comment = row
    assert name == "uq_assertion_results_attempt_assertion_phase", name
    assert {column.strip() for column in definition.partition("(")[2].partition(")")[0].split(",")} == {
        "step_attempt_id",
        "assertion_id",
        "phase",
    }, definition
    assert _has_chinese(comment), "唯一约束缺少中文 COMMENT"


# —— 角色初始化必须在真实数据库上执行，而不只是核对语句文本 ——

# 合成密码，仅用于本测试：含 $$、单引号与首尾空格，覆盖“改写秘密”和“破坏语句结构”
# 两类问题。不使用任何真实秘密，也不打印它。
_SYNTHETIC_ROLE_SECRET = "  syn$$the'tic role pass  "

_ROLE_FLAGS = (
    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolbypassrls"
    " FROM pg_roles WHERE rolname = %s"
)


def _runtime_role_flags(connection: psycopg.Connection) -> tuple:
    row = connection.execute(_ROLE_FLAGS, (RUNTIME_ROLE,)).fetchone()
    assert row is not None, f"缺少运行时角色 {RUNTIME_ROLE}"
    return row


def _real_settings(**changes):
    from dataclasses import replace

    from app.config import get_settings

    # 复用容器内的真实主机／库名／迁移身份，只覆盖本测试要控制的字段。
    return replace(get_settings(), **changes)


def test_init_runtime_role_runs_against_real_database(
    schema_connection: psycopg.Connection,
) -> None:
    """真实执行初始化入口，而不是只核对构造出的 SQL 字符串。

    存在性查询必须用驱动支持的取行方式：psycopg 的游标没有 SQLAlchemy 的
    `.scalar()`，一旦用错，`init-roles` 会在启动时就抛 AttributeError，
    而“语句文本正确”的单测发现不了这一点。
    """
    from app.roles import init_runtime_role

    message = init_runtime_role(_real_settings())
    assert RUNTIME_ROLE in message and BROKER_ROLE in message

    can_login, superuser, createdb, createrole, inherit, bypass = _runtime_role_flags(
        schema_connection
    )
    assert can_login is True
    assert (superuser, createdb, createrole, inherit, bypass) == (False,) * 5


def test_existing_over_privileged_runtime_role_is_tightened_on_real_database(
    schema_connection: psycopg.Connection,
) -> None:
    """已存在角色路径必须在真实数据库上收紧属性，且密码按原值使用。

    先故意把运行角色提成超级用户／可建库／可建角色／INHERIT，并改成含 $$、单引号和
    首尾空格的合成密码，再调用真实初始化入口，最后查真实 `pg_roles` 并用真实连接
    验证密码原值。提权本身也在 try 内，任何断言失败都会经 finally 恢复受限状态，
    否则一次失败会把开发库的运行角色留在超级用户状态。
    """
    from psycopg import sql

    from app.roles import init_runtime_role

    over_privileged = sql.SQL(
        "ALTER ROLE {} WITH LOGIN SUPERUSER CREATEDB CREATEROLE INHERIT PASSWORD {}"
    ).format(sql.Identifier(RUNTIME_ROLE), sql.Literal(_SYNTHETIC_ROLE_SECRET))

    settings = _real_settings(pgpassword=_SYNTHETIC_ROLE_SECRET)
    try:
        with schema_connection.cursor() as cursor:
            cursor.execute(over_privileged)
        schema_connection.commit()
        before = _runtime_role_flags(schema_connection)
        # 前置状态必须真的“越权”，否则本测试只是重述一个本来就成立的状态。
        # 第 5 位是 rolbypassrls：该角色本就不得绕过 RLS，这里只提权其余四项。
        assert before[1:5] == (True,) * 4, f"前置提权未生效：{before}"

        init_runtime_role(settings)

        after = _runtime_role_flags(schema_connection)
        assert after[0] is True, "运行角色仍需可登录"
        assert after[1:] == (False,) * 5, f"已存在角色未被收紧：{after}"
        # 密码原值验证：用刚设置的合成密码真实登录一次。strip() 会去掉首尾空格，
        # 那样这里会以“认证失败”暴露，而不是悄悄把库里的密码改成另一个值。
        with psycopg.connect(
            host=settings.pghost,
            port=settings.pgport,
            dbname=settings.pgdatabase,
            user=RUNTIME_ROLE,
            password=_SYNTHETIC_ROLE_SECRET,
        ) as login:
            assert login.execute("SELECT current_user").fetchone()[0] == RUNTIME_ROLE
    finally:
        # 恢复不能依赖被测代码：收紧语句一旦回归，经初始化入口“恢复”同样不生效，
        # 会把开发库的运行角色留在超级用户状态。这里用迁移身份直接复位。
        real = _real_settings()
        with schema_connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    " NOINHERIT NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(RUNTIME_ROLE), sql.Literal(real.pgpassword))
            )
        schema_connection.commit()
        restored = _runtime_role_flags(schema_connection)
        assert restored == (True, False, False, False, False, False), f"恢复运行角色失败：{restored}"
