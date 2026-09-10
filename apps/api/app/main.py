"""开发环境连通性入口；尚不包含接口测试业务功能。"""
import logging
import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="接口测试平台开发环境")
logger = logging.getLogger(__name__)
DEVELOPMENT_MESSAGE = "后端开发服务已就绪"


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
