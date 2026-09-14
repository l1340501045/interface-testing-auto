"""运行时数据库角色初始化：与固定业务迁移分离。

不使用 DO $$ 匿名块：密码中合法的 $$ 会提前结束美元引用块，导致语法错误或
把秘密放进错误的 SQL。这里改用 psycopg 的 sql.Identifier / sql.Literal 组合
DDL，由驱动负责标识符与字面量的安全编码；语句本身不打印，避免秘密出现在日志。

角色创建先于迁移执行，迁移只负责建表、RLS 与授权。平台引导操作使用迁移身份
（超级用户），因此普通业务 RLS 不再保留跨租户的 is_admin 放行。
"""
from __future__ import annotations

import psycopg
from psycopg import sql

from .config import Settings

RUNTIME_ROLE = "app_runtime"
BROKER_ROLE = "app_job_broker"


def build_role_statements(role_exists: bool, password: str) -> list[sql.Composed]:
    """构造角色初始化语句，供执行与离线验证共用。

    密码经 sql.Literal 编码；语句中不含 DO $$ 块，因此密码里的 $$ 或单引号
    都不会改变语句结构。
    """
    role = sql.Identifier(RUNTIME_ROLE)
    statements: list[sql.Composed] = []
    if role_exists:
        if password:
            statements.append(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    role, sql.Literal(password)
                )
            )
    elif password:
        statements.append(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(role, sql.Literal(password))
        )
    else:
        statements.append(sql.SQL("CREATE ROLE {} WITH NOLOGIN").format(role))
    # 已有角色的路径也要收紧属性。只在新建时限制、而对已存在角色只改密码，会把
    # 一个原本是超级用户或可建库的旧角色原样留下，初始化那句“非超级用户”就没有
    # 依据了。这里统一把受限属性写成无条件语句。
    statements.append(
        sql.SQL(
            "ALTER ROLE {} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
        ).format(role)
    )
    return statements


def build_broker_statements(role_exists: bool) -> list[sql.Composed]:
    """工作项领取代理角色：不可登录，只用于承载跨租户领取函数。

    运行角色 app_runtime 保持 NOBYPASSRLS；跨租户的原子领取被收敛为一个
    SECURITY DEFINER 函数，而不是给它一个宽泛的绕过权限。
    """
    role = sql.Identifier(BROKER_ROLE)
    statements: list[sql.Composed] = []
    if not role_exists:
        statements.append(sql.SQL("CREATE ROLE {} WITH NOLOGIN").format(role))
    # 与运行角色同理：已存在角色同样收紧基础属性。BYPASSRLS 是该角色的既定职责
    # （承载跨租户领取函数），单独显式设置，不作为“创建时的默认”被继承下来。
    statements.append(
        sql.SQL(
            "ALTER ROLE {} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS"
        ).format(role)
    )
    return statements


def init_runtime_role(settings: Settings) -> str:
    """确保运行时角色存在且无 RLS 绕过能力。返回给调用方展示的结果说明。"""
    # 密码必须按原值使用：strip() 会改掉合法密码里的首尾空格，于是数据库里存的
    # 与 .env 里存的不是同一个值，直到应用连接时才以“认证失败”暴露出来。
    password = settings.pgpassword
    if not password:
        raise RuntimeError(
            "未配置应用运行时密码（PGPASSWORD）：请先运行 make init 生成本机秘密，"
            "否则只会建出一个永远无法登录的角色。"
        )

    with psycopg.connect(
        host=settings.pghost,
        port=settings.pgport,
        dbname=settings.pgdatabase,
        user=settings.migrator_user,
        password=settings.migrator_password or None,
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)
        ).fetchone()
        broker_exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (BROKER_ROLE,)
        ).fetchone()
        with connection.cursor() as cursor:
            for statement in build_role_statements(bool(exists), password):
                cursor.execute(statement)
            for statement in build_broker_statements(bool(broker_exists)):
                cursor.execute(statement)
        connection.commit()

    return (
        f"运行时角色 {RUNTIME_ROLE} 已就绪（NOBYPASSRLS，非超级用户）；"
        f"领取代理角色 {BROKER_ROLE} 已就绪（不可登录）。"
    )
