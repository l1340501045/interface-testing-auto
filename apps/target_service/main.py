"""受控目标服务：为闭环验收提供确定行为的被测接口。

只用于本机开发与验收，不使用任何用户数据。提供回显、指定状态码、指定耗时、
大整数与小数回显等确定行为，便于验证真实 HTTP、无损数值和各类断言；不提供
任意代理、转发或本机文件读取能力。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets as py_secrets
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI(title="受控目标服务", docs_url=None, openapi_url=None)

BIG_INTEGER = "9007199254740993"

# 同一份镜像可以起多个受控目标实例（例如两个测试环境各指一个）。实例名随响应
# 返回，验收才能证明“两个环境确实各自发到了自己的服务”，而不是同一个服务被
# 两次调用后由环境名推断出来的结论。
SERVICE_NAME = os.environ.get("TARGET_SERVICE_NAME", "controlled-target")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.api_route(
    "/echo",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def echo(request: Request) -> JSONResponse:
    """回显请求方法、查询参数、请求头与正文，便于断言入参与出参。

    正文 JSON 解析成功时额外以 ``json`` 字段返回解析后的结构，被测响应因此
    有可定位的字段树；解析失败则只有 ``body_text``，不伪造空对象。
    """
    body = await request.body()
    text = body.decode("utf-8", errors="replace")
    query: list[dict[str, str]] = [
        {"name": name, "value": value} for name, value in request.query_params.multi_items()
    ]
    payload: dict = {
        "service": SERVICE_NAME,
        "method": request.method,
        "path": request.url.path,
        "query": query,
        "headers": [{"name": name, "value": value} for name, value in request.headers.items()],
        "body_text": text,
        "body_size": len(body),
    }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        payload["json"] = parsed
    return JSONResponse(payload)


@app.api_route("/form", methods=["POST", "PUT", "PATCH"])
async def form_fields(request: Request) -> JSONResponse:
    """按表单协议把正文解码成**有序、可重复**的字段列表。

    表单正文必须在服务端解码后逐项核对：只回显原文的话，“取值里的 `&` 被当成了
    字段分隔符”与“没有被当成分隔符”在文本上一模一样，断言分辨不出来。这里用与
    被测实现同一份协议解析（`parse_qsl`），返回的项数与顺序就是目标真正读到的。
    """
    text = (await request.body()).decode("utf-8", errors="replace")
    fields = [
        {"name": name, "value": value}
        for name, value in parse_qsl(text, keep_blank_values=True)
    ]
    return JSONResponse(
        {"service": SERVICE_NAME, "fields": fields, "field_count": len(fields)}
    )


@app.get("/status/{code}")
def status_code(code: int) -> JSONResponse:
    """按路径返回指定状态码，用于状态码断言与失败证据。"""
    if code < 100 or code > 599:
        return JSONResponse({"error": "状态码必须在 100 到 599 之间"}, status_code=400)
    return JSONResponse({"code": code}, status_code=code)


@app.get("/delay/{milliseconds}")
async def delay(milliseconds: int) -> JSONResponse:
    """按毫秒延迟响应，用于耗时断言与超时分类。"""
    if milliseconds < 0 or milliseconds > 60000:
        return JSONResponse({"error": "延迟必须在 0 到 60000 毫秒之间"}, status_code=400)
    await asyncio.sleep(milliseconds / 1000)
    return JSONResponse({"delayed_ms": milliseconds})


@app.get("/numbers")
def numbers() -> JSONResponse:
    """返回长整数与多位小数，验证 JSON 原文无损往返。"""
    return JSONResponse(
        {
            "big_integer": int(BIG_INTEGER),
            "big_integer_text": BIG_INTEGER,
            "decimal": 0.1,
            "sum": 0.30000000000000004,
            "amount": 0,
        }
    )


@app.get("/require-header")
def require_header(request: Request) -> JSONResponse:
    """要求携带 X-Demo-Token 请求头，用于认证注入的正反验证。"""
    token = request.headers.get("X-Demo-Token")
    if not token:
        return JSONResponse({"error": "缺少 X-Demo-Token 请求头"}, status_code=401)
    return JSONResponse({"token_echo": token})


# 受控服务认可的凭据位置。只认这几个位置，且**绝不回显收到的值**。
_CREDENTIAL_LOCATIONS = (
    ("header", "authorization"),
    ("header", "x-api-key"),
    ("header", "x-demo-token"),
    ("query", "api_key"),
)


@app.get("/secure/profile")
def secure_profile(request: Request) -> JSONResponse:
    """在服务端判定凭据是否正确，只返回结论与非敏感元数据。

    验收“秘密确实被注入到真实请求里”时不能用普通断言去比较秘密：那要么把秘密抄进
    用例、要么让秘密出现在运行报告里。正确做法是由受控服务在服务端比对，返回认证
    结论，以及不可逆的指纹前缀、凭据长度与命中位置——足以证明“用的是这一份凭据、
    它到了这个位置”，却不泄露凭据本身。
    """
    expected = os.environ.get("TARGET_EXPECTED_TOKEN", "")
    if expected == "":
        return JSONResponse({"error": "受控服务未配置 TARGET_EXPECTED_TOKEN"}, status_code=503)

    for where, name in _CREDENTIAL_LOCATIONS:
        raw = request.headers.get(name) if where == "header" else request.query_params.get(name)
        if raw is None:
            continue
        scheme = "raw"
        value = raw
        if where == "header" and name == "authorization" and raw[:7].lower() == "bearer ":
            scheme = "bearer"
            value = raw[7:]
        if py_secrets.compare_digest(value, expected):
            return JSONResponse(
                {
                    "authenticated": True,
                    "service": SERVICE_NAME,
                    "location": f"{where}.{name}",
                    "scheme": scheme,
                    "credential_length": len(value),
                    "credential_fingerprint": hashlib.sha256(value.encode()).hexdigest()[:12],
                }
            )
    return JSONResponse({"authenticated": False, "reason": "凭据缺失或不匹配"}, status_code=401)


@app.get("/slow-body")
def slow_body() -> PlainTextResponse:
    """返回纯文本，用于“响应不是 JSON”时的断言行为验证。"""
    return PlainTextResponse(f"controlled-target text {int(time.time())}")


@app.get("/secure/keyed")
def secure_keyed(request: Request) -> JSONResponse:
    """把收到的凭据当作对象**键**回显，验证“键名与值同等受保护”。

    凭据不只可能出现在字段值里：目标把 `{"<凭据>": "ok"}` 这样以凭据为键的对象
    返回时，只按值判断敏感的实现在这里就是睁眼瞎——整对象断言既求得了值，掩码也
    盖不住键名。这个接口只用于验收这一种形态。

    反射强度与 `/require-header` 相同（缺头才 401，有值即回显），只是把凭据放在
    键的位置；两者都是仅本机可达的受控测试替身，不提供按内容检索或转发能力。
    """
    token = request.headers.get("x-demo-token")
    if not token:
        return JSONResponse({"error": "缺少 X-Demo-Token 请求头"}, status_code=401)
    return JSONResponse({token: "ok"})
