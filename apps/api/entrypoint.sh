#!/bin/sh
# 启动前初始化运行时角色并执行数据库迁移（迁移账号），随后以运行账号启动应用进程。
# 角色先于迁移创建，迁移才能对其授权；两者都不打印秘密。
# 只有管理 API 负责迁移（RUN_MIGRATIONS=1），worker 直接启动，避免多进程并发升级。
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ] && [ -n "${MIGRATOR_PASSWORD:-}" ]; then
  echo "初始化运行时角色..."
  python -m app.cli init-roles
  echo "执行数据库迁移..."
  python -m app.cli migrate
fi

exec "$@"
