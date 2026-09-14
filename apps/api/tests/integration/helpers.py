"""集成测试共享的数据准备与清理工具。

不使用与 conftest 同名的模块：pytest 在根目录与子目录各有一个 conftest.py 时
按路径区分模块名，跨目录导入会互相覆盖；独立的 helpers 模块语义更明确。
"""
from __future__ import annotations

import os
import uuid

import psycopg


def migrator_connection() -> psycopg.Connection:
    """以迁移身份连接当前集成测试库。

    迁移角色是唯一有权跨租户写入测试夹具的身份；运行角色受 RLS 约束，
    无法在未建立租户上下文时插入数据。
    """
    return psycopg.connect(
        f"postgresql://{os.environ['MIGRATOR_USER']}:{os.environ['MIGRATOR_PASSWORD']}"
        f"@{os.environ.get('PGHOST', 'db')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ['PGDATABASE']}"
    )


def purge_workspace(connection: psycopg.Connection, workspace_id: uuid.UUID) -> None:
    """按依赖顺序删掉测试自建的工作空间及其全部从属数据。

    工作项对执行池是 RESTRICT，而工作空间到池是 CASCADE；同时存在两条删除路径时
    PostgreSQL 不保证先后，因此这里显式按序删除，不依赖级联。测试只清理自己
    创建的数据，不触碰开发库内容。
    """
    statements = [
        "DELETE FROM app.assertion_results WHERE workspace_id = %s",
        "DELETE FROM app.run_step_attempts WHERE workspace_id = %s",
        "DELETE FROM app.jobs WHERE workspace_id = %s",
        "DELETE FROM app.idempotency_records WHERE workspace_id = %s",
        "DELETE FROM app.audit_events WHERE workspace_id = %s",
        "DELETE FROM app.runs WHERE workspace_id = %s",
        "DELETE FROM app.case_assertions WHERE workspace_id = %s",
        "DELETE FROM app.case_versions WHERE workspace_id = %s",
        "DELETE FROM app.cases WHERE workspace_id = %s",
        "DELETE FROM app.folders WHERE workspace_id = %s",
        # 凭证链按引用方向自外向内删除。顺序不能颠倒：身份通过 current_set_id
        # 引用集合（NO ACTION），先删集合会被外键拒绝；而删除身份会级联清理
        # 其配置版本、集合与集合绑定。
        "DELETE FROM app.credential_use_grants WHERE workspace_id = %s",
        "DELETE FROM app.credential_profiles WHERE workspace_id = %s",
        "DELETE FROM app.credential_set_secret_versions WHERE workspace_id = %s",
        "DELETE FROM app.credential_sets WHERE workspace_id = %s",
        "DELETE FROM app.credential_profile_versions WHERE workspace_id = %s",
        "DELETE FROM app.secret_versions WHERE workspace_id = %s",
        "DELETE FROM app.secrets WHERE workspace_id = %s",
        "DELETE FROM app.environments WHERE workspace_id = %s",
        "DELETE FROM app.project_config_versions WHERE workspace_id = %s",
        "DELETE FROM app.runner_pool_project_grants WHERE workspace_id = %s",
        "DELETE FROM app.runner_pools WHERE workspace_id = %s",
        "DELETE FROM app.workspace_memberships WHERE workspace_id = %s",
        "DELETE FROM app.projects WHERE workspace_id = %s",
        "DELETE FROM app.workspaces WHERE id = %s",
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement, (workspace_id,))
    connection.commit()


def purge_user(connection: psycopg.Connection, user_id: uuid.UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM app.sessions WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM app.users WHERE id = %s", (user_id,))
    connection.commit()
