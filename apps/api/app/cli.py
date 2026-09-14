"""引导与运维命令：生成主密钥、创建首位管理员、执行迁移。

运行时机：首次初始化。以迁移账号（超级用户）连接数据库，创建首位
平台管理员及其默认工作空间；主密钥只写本地文件，绝不打印或提交。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import User, Workspace, WorkspaceMembership
from .security import hash_password, new_fernet_key

_MIN_PASSWORD_LEN = 10


def _migrator_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.migrator_url())
    return Session(bind=engine)


def _ensure_secret_key(settings) -> None:
    if settings.secret_key.strip():
        return
    path = Path(settings.secret_key_path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    key = new_fernet_key()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(key)
    print(f"已生成主密钥：{path}")


def _read_password(password_stdin: bool) -> str:
    """密码只来自本机输入：交互终端或标准输入，不经过命令行参数与日志。"""
    while True:
        if password_stdin:
            password = sys.stdin.readline().rstrip("\n")
            if password == "":
                raise SystemExit("标准输入未提供密码")
        else:
            password = getpass.getpass("管理员密码：")
        if len(password) < _MIN_PASSWORD_LEN:
            if password_stdin:
                raise SystemExit(f"密码至少 {_MIN_PASSWORD_LEN} 位")
            print(f"密码至少 {_MIN_PASSWORD_LEN} 位，请重试。")
            continue
        if password_stdin:
            return password
        if getpass.getpass("再次输入密码：") != password:
            print("两次输入不一致，请重试。")
            continue
        return password


def _create_admin(
    session: Session,
    *,
    username: str | None = None,
    display_name: str | None = None,
    password_stdin: bool = False,
) -> None:
    existing = session.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1))
    if existing is not None:
        print("已存在平台管理员，跳过创建。")
        return

    if not password_stdin:
        username = input("管理员登录名 [admin]：").strip() or "admin"
        display_name = input("管理员显示名 [平台管理员]：").strip() or "平台管理员"
    username = (username or "admin").strip()
    display_name = (display_name or "平台管理员").strip()
    password = _read_password(password_stdin)

    workspace = Workspace(name="默认工作空间", status="active")
    session.add(workspace)
    session.flush()

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        is_admin=True,
        status="active",
    )
    session.add(user)
    session.flush()

    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin"))
    session.commit()
    print(f"已创建管理员账号 {username}，请勿丢失密码。")


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    print("数据库迁移完成。")


def _init_roles() -> None:
    from .roles import init_runtime_role

    print(init_runtime_role(get_settings()))


def _list_pools() -> None:
    """运维命令：列出各项目的执行池 id，供配置 worker 的 WORKER_POOL_IDS。

    使用迁移账号读取，因为运行角色受 RLS 约束、未建立租户上下文时看不到任何行。
    该命令只输出池 id、名称与所属项目 id，不涉及秘密。
    """
    from .models import Project, RunnerPool, RunnerPoolProjectGrant

    session = _migrator_session()
    try:
        rows = session.execute(
            select(
                RunnerPool.id,
                RunnerPool.name,
                RunnerPool.status,
                RunnerPoolProjectGrant.project_id,
                Project.name,
            )
            .join(RunnerPoolProjectGrant, RunnerPoolProjectGrant.pool_id == RunnerPool.id)
            # 排序键必须来自已连接的表，否则 PostgreSQL 报 missing FROM-clause entry。
            .join(Project, Project.id == RunnerPoolProjectGrant.project_id)
            .where(RunnerPoolProjectGrant.status == "active")
            .order_by(Project.name, RunnerPool.name)
        ).all()
        if not rows:
            print("尚未创建执行池：请先在页面或接口中创建项目，系统会为项目建立默认执行池。")
            return
        print("执行池（把 id 配置到 worker 的 WORKER_POOL_IDS，多个用逗号分隔）：")
        for pool_id, name, status, project_id, project_name in rows:
            print(f"  {pool_id}  {name}  状态={status}  项目={project_name}（{project_id}）")
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="接口测试平台引导与运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap", help="生成主密钥并创建首位管理员")
    bootstrap.add_argument("--username", help="管理员登录名；仅与 --password-stdin 一起使用")
    bootstrap.add_argument("--display-name", help="管理员显示名；仅与 --password-stdin 一起使用")
    bootstrap.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取密码，供本机脚本化初始化；不会写入日志或命令行",
    )
    sub.add_parser("migrate", help="执行数据库迁移到最新版本")
    sub.add_parser("init-roles", help="初始化无 RLS 绕过权限的运行时角色")
    sub.add_parser("list-pools", help="列出执行池 id，用于配置 worker 的 WORKER_POOL_IDS")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "init-roles":
        _init_roles()
        return 0

    if args.command == "list-pools":
        _list_pools()
        return 0

    if args.command == "migrate":
        _migrate()
        return 0

    if args.command == "bootstrap":
        _ensure_secret_key(settings)
        session = _migrator_session()
        try:
            _create_admin(
                session,
                username=args.username,
                display_name=args.display_name,
                password_stdin=args.password_stdin,
            )
        finally:
            session.close()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
