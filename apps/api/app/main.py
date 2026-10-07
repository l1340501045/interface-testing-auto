"""管理 API 入口。

保留 `/health` 供开发容器健康检查；业务接口统一挂在 `/api/v1` 下。
长耗时的被测请求不由本进程发送，本进程只负责鉴权、编排与持久化。
"""
import logging
import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .api.errors import install_error_handlers
from .api.routes import assets, auth, cases, credentials, projects, runs, services

app = FastAPI(title="接口自动化测试与巡检平台", docs_url="/api/docs", openapi_url="/api/openapi.json")
logger = logging.getLogger(__name__)
DEVELOPMENT_MESSAGE = "后端开发服务已就绪"

install_error_handlers(app)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(credentials.router, prefix="/api/v1")
app.include_router(cases.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(assets.router, prefix="/api/v1")
app.include_router(services.router, prefix="/api/v1")


@app.get("/health")
def health() -> JSONResponse:
    try:
        with psycopg.connect(
            host=os.environ["PGHOST"],
            dbname=os.environ["PGDATABASE"],
            user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            connect_timeout=3,
        ) as connection:
            connection.execute("SELECT 1").fetchone()
    except (psycopg.Error, KeyError) as error:
        logger.warning("开发数据库检查失败，类别=%s", type(error).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unavailable", "message": "数据库暂不可用"},
        )
    return JSONResponse(
        content={"status": "ok", "database": "connected", "message": DEVELOPMENT_MESSAGE}
    )
