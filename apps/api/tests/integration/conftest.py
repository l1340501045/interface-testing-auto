"""集成测试配置：真实 PostgreSQL 连接。

集成测试只在提供数据库连接信息时运行，使用独立数据库，不触碰开发库数据。
运行角色 app_runtime 与迁移角色分开，测试据此验证 RLS 与权限边界。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.integration


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"缺少 {name}，跳过集成测试")
    return value


@pytest.fixture(scope="session")
def migrator_url() -> str:
    user = _require("MIGRATOR_USER")
    password = _require("MIGRATOR_PASSWORD")
    host = os.environ.get("PGHOST", "db")
    port = os.environ.get("PGPORT", "5432")
    database = _require("PGDATABASE")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture(scope="session")
def runtime_url() -> str:
    user = os.environ.get("PGUSER", "app_runtime")
    password = _require("PGPASSWORD")
    host = os.environ.get("PGHOST", "db")
    port = os.environ.get("PGPORT", "5432")
    database = _require("PGDATABASE")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture
def seed() -> dict:
    """一次测试所需的租户数据标识，由测试自行写入并清理。"""
    return {"workspace_a": uuid.uuid4(), "workspace_b": uuid.uuid4()}
