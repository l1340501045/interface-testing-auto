"""统一 API 错误：稳定错误码、中文信息、trace_id。

错误分类遵循后端错误处理规范：输入配置错误、未认证／越权、业务断言失败、
网络失败、平台故障、取消和结果不明分开；API 边界只转换已知错误，
不向客户端泄露堆栈、SQL 或凭证。
"""
from __future__ import annotations

import contextvars
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def get_trace_id() -> str:
    return _trace_id.get()


class ApiError(Exception):
    """带稳定错误码的业务异常，HTTP 状态由调用点声明。"""

    def __init__(
        self, status_code: int, code: str, message: str, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(400, code, message)


def unauthorized(message: str = "请先登录") -> ApiError:
    return ApiError(401, "unauthorized", message)


def forbidden(code: str, message: str) -> ApiError:
    return ApiError(403, code, message)


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def too_many_requests(message: str = "请求过于频繁，请稍后重试") -> ApiError:
    return ApiError(429, "rate_limited", message)


def _payload(code: str, message: str) -> dict:
    return {"code": code, "message": message, "trace_id": get_trace_id()}


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace = request.headers.get("X-Trace-Id") or new_trace_id()
        set_trace_id(trace)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={**_payload(exc.code, exc.message), **exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = f"请求参数不合法：{location or '请求体'} {first.get('msg', '')}".strip()
        return JSONResponse(status_code=422, content=_payload("invalid_request", message))

    @app.exception_handler(IntegrityError)
    async def handle_integrity(_request: Request, exc: IntegrityError) -> JSONResponse:
        # 唯一的约束冲突按冲突返回；其他完整性错误由平台故障处理。
        text = str(getattr(exc, "orig", exc))
        if "duplicate key" in text or "unique" in text:
            return JSONResponse(status_code=409, content=_payload("conflict", "存在重复数据，请刷新后重试"))
        return JSONResponse(status_code=500, content=_payload("platform_error", "数据库约束校验失败"))

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=500, content=_payload("platform_error", "数据库操作失败，请稍后重试"))

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=_payload("platform_error", "服务内部错误"))
