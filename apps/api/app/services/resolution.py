"""S1 普通变量目录、同源绑定预览与不透明解析依据。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..kernel.request_spec import BindingEvent, RequestSpecError, prepare
from ..kernel.valueliteral import ValueLiteral, ValueLiteralError
from ..kernel.variables import (
    VariableResolutionError,
    build_resolver,
    variable_reference_for_name,
)
from ..models import Environment, EnvironmentConfigVersion, ProjectConfigVersion

_CONTEXT_TTL = timedelta(minutes=10)
_CONTEXT_PURPOSE = "resolution-context/v1"
_SOURCE_PURPOSE = "resolution-source/v1"
_LOCATION_PURPOSE = "resolution-location/v1"
_CONTEXT_FINGERPRINT_PURPOSE = "resolution-fingerprint/v1"


class ResolutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VariableState:
    merged: dict[str, dict[str, Any]]
    sources: dict[str, dict[str, Any]]
    overridden: dict[str, list[dict[str, Any]]]
    basis: dict[str, Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _hmac_hex(key: bytes, purpose: str, value: Any) -> str:
    return hmac.new(key, purpose.encode() + b"\0" + _canonical(value), hashlib.sha256).hexdigest()


def _source(level: str, resource_id: uuid.UUID, revision: int, value: object) -> dict:
    source = {
        "level": level,
        "resource_id": str(resource_id),
        "revision": revision,
    }
    try:
        if not isinstance(value, dict):
            raise ValueLiteralError("普通变量字面量必须是对象")
        ValueLiteral.from_dict(value)
    except ValueLiteralError:
        source.update(
            {"value": None, "unavailable_reason": "历史来源不是可展示的普通变量字面量"}
        )
    else:
        source.update({"value": dict(value), "unavailable_reason": None})
    return source


def load_variable_state(session: Session, environment: Environment) -> VariableState:
    """一次事务内读取两层变量及当前环境配置指针，并验证投影一致。"""
    project_version = session.scalar(
        select(ProjectConfigVersion)
        .where(ProjectConfigVersion.project_id == environment.project_id)
        .order_by(ProjectConfigVersion.version.desc())
        .limit(1)
    )
    config_version = session.scalar(
        select(EnvironmentConfigVersion).where(
            EnvironmentConfigVersion.id == environment.current_config_version_id,
            EnvironmentConfigVersion.workspace_id == environment.workspace_id,
            EnvironmentConfigVersion.project_id == environment.project_id,
            EnvironmentConfigVersion.environment_id == environment.id,
        )
    )
    if config_version is None or config_version.schema_version != 1:
        raise ResolutionError(
            "config_inconsistent", "环境当前配置版本缺失或结构不受支持，请联系管理员修复。"
        )
    snapshot = config_version.snapshot if isinstance(config_version.snapshot, dict) else {}
    snapshot_variables = snapshot.get("variables")
    if snapshot.get("schema_version") != 1 or snapshot_variables != (environment.variables or {}):
        raise ResolutionError(
            "config_inconsistent", "环境当前配置版本与配置投影不一致，请联系管理员修复。"
        )

    project_variables = dict(project_version.variables) if project_version is not None else {}
    environment_variables = dict(environment.variables or {})
    merged = dict(project_variables)
    merged.update(environment_variables)
    sources: dict[str, dict[str, Any]] = {}
    overridden: dict[str, list[dict[str, Any]]] = {}
    if project_version is not None:
        for name, value in project_variables.items():
            sources[name] = _source(
                "project", environment.project_id, project_version.version, value
            )
    for name, value in environment_variables.items():
        if name in sources:
            overridden[name] = [sources[name]]
        sources[name] = _source("environment", environment.id, environment.rev, value)

    return VariableState(
        merged=merged,
        sources=sources,
        overridden=overridden,
        basis={
            "project_variables_version": project_version.version if project_version else 0,
            "project_config_version_id": str(project_version.id) if project_version else None,
            "environment_rev": environment.rev,
            "environment_config_version": config_version.version,
            "environment_config_version_id": str(config_version.id),
        },
    )


def variable_context(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, environment: Environment
) -> dict:
    state = load_variable_state(session, environment)
    variables = []
    for name in sorted(state.merged):
        raw = state.merged[name]
        reference = variable_reference_for_name(name)
        # 当前写入口已经严格校验；这里仍在外部持久数据边界复核，不能把未知类型伪成字符串。
        try:
            if not isinstance(raw, dict):
                raise ValueLiteralError("普通变量字面量必须是对象")
            ValueLiteral.from_dict(raw)
        except ValueLiteralError:  # 精确错误不回显持久原值
            variables.append(
                {
                    "name": name,
                    "reference": reference,
                    "value": None,
                    "effective_source": None,
                    "overridden_sources": state.overridden.get(name, []),
                    "available_locations": [],
                    "restricted_body_types": [],
                    "unavailable_reason": "当前来源不是可插入的普通变量字面量",
                }
            )
            continue
        variables.append(
            {
                "name": name,
                "reference": reference,
                "value": dict(raw),
                "effective_source": state.sources[name],
                "overridden_sources": state.overridden.get(name, []),
                "available_locations": (
                    ["path", "query_value", "header_value", "body"]
                    if reference is not None
                    else []
                ),
                "restricted_body_types": (
                    ["form"] if reference is not None and any(char in name for char in "+%") else []
                ),
                "unavailable_reason": (
                    None
                    if reference is not None
                    else "变量名无法用旧 {{名称}} 语法完整还原，仅保留只读展示"
                ),
            }
        )
    return {
        "schema_version": 1,
        "scope": {
            "workspace_id": str(workspace_id),
            "project_id": str(project_id),
            "environment_id": str(environment.id),
        },
        "config_basis": state.basis,
        "variables": variables,
    }


def _event_location(key: bytes, request: dict, event: BindingEvent) -> dict:
    location = {
        name: value for name, value in event.location.items() if name != "raw_text"
    }
    kind = location.get("kind")
    if kind in {"query", "header"} and "row_id" not in location:
        field = "query_params" if kind == "query" else "headers"
        index = location.get("index")
        row = request[field][index] if isinstance(index, int) else {}
        location["input_fingerprint"] = _hmac_hex(
            key,
            _LOCATION_PURPOSE,
            {"kind": kind, "index": index, "row": row},
        )
    return location


def _issue(
    key: bytes,
    code: str,
    message: str,
    location: dict | None,
    *,
    identity: dict[str, Any] | None = None,
) -> dict:
    return {
        "issue_id": _hmac_hex(
            key,
            _LOCATION_PURPOSE,
            {"code": code, "location": location, "identity": identity},
        ),
        "code": code,
        "message": message,
        "action": "edit_request",
        "location": location,
    }


def _masked_target(prepared) -> dict:
    """普通请求的权威目标投影；预览 prepare 从未注入身份秘密。"""
    return {"method": prepared.method, "url": prepared.url}


def _slot_conflict_issues(key: bytes, request: dict, slots: list[dict]) -> list[dict]:
    """按正式 prepare 的 Header/Query 占位规则定位用户启用行冲突。"""
    issues: list[dict] = []
    for slot in slots:
        kind = slot.get("kind")
        name = slot.get("name")
        if kind not in {"header", "query"} or not isinstance(name, str):
            continue
        rows = request["headers" if kind == "header" else "query_params"]
        occurrence = 0
        for index, row in enumerate(rows):
            if not row.get("enabled", True):
                continue
            row_name = str(row.get("name") or "")
            matches = row_name.lower() == name.lower() if kind == "header" else row_name == name
            if not matches:
                continue
            location: dict[str, Any] = {"kind": kind, "field": "value"}
            if "row_id" in row:
                location["row_id"] = row["row_id"]
            else:
                location.update(
                    {
                        "index": index,
                        "occurrence": occurrence,
                        "input_fingerprint": _hmac_hex(
                            key,
                            _LOCATION_PURPOSE,
                            {"kind": kind, "index": index, "row": row},
                        ),
                    }
                )
            slot["status"] = "conflict"
            issues.append(
                _issue(
                    key,
                    "credential_slot_conflict",
                    f"请求已定义认证{('头' if kind == 'header' else '查询参数')}「{name}」，"
                    "环境身份不能覆盖该用户定义，请移除其中一处。",
                    location,
                )
            )
            occurrence += 1
    return issues


def _source_fingerprint(
    key: bytes, *, source_kind: str, source_id: uuid.UUID | None, request: dict, assertions: list[dict]
) -> str:
    return _hmac_hex(
        key,
        _SOURCE_PURPOSE,
        {
            "source_kind": source_kind,
            "source_id": str(source_id) if source_id else None,
            "request": request,
            "assertions": assertions,
        },
    )


def _sign_context(key: bytes, payload: dict) -> str:
    body = base64.urlsafe_b64encode(_canonical(payload)).decode().rstrip("=")
    signature = hmac.new(key, _CONTEXT_PURPOSE.encode() + b"\0" + body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def build_resolution(
    session: Session,
    settings: Settings,
    *,
    environment: Environment,
    principal_id: uuid.UUID,
    request: dict,
    assertions: list[dict],
    source_kind: str,
    source_id: uuid.UUID | None,
    auth: dict | None = None,
) -> dict:
    key = settings.load_secret_key()
    state = load_variable_state(session, environment)
    events: list[BindingEvent] = []
    bindings: list[dict] = []
    issues: list[dict] = []
    prepared = None
    try:
        resolver = build_resolver(
            [{"name": name, "value": value} for name, value in state.merged.items()]
        )
        prepared = prepare(
            request, environment.base_url, resolver, binding_events=events
        )
    except VariableResolutionError as error:
        if not any(not event.defined for event in events):
            issues.append(
                _issue(key, "binding_invalid", str(error), None)
            )
    except RequestSpecError as error:
        issues.append(
            _issue(
                key,
                "binding_invalid",
                f"请求绑定无法准备：{error}",
                {"kind": "body", "field": "body", "selector": []},
            )
        )

    for event in events:
        location = _event_location(key, request, event)
        if not event.defined:
            issues.append(
                _issue(
                    key,
                    "variable_undefined",
                    f"变量「{event.name}」不存在，请补充后重试。",
                    location,
                    identity={"name": event.name, "event_index": event.event_index},
                )
            )
            continue
        source = state.sources.get(event.name)
        if source is None or source.get("value") is None:
            issues.append(
                _issue(
                    key,
                    "binding_invalid",
                    f"变量「{event.name}」的当前来源不可用于普通绑定。",
                    location,
                    identity={"name": event.name, "event_index": event.event_index},
                )
            )
            continue
        bindings.append(
            {
                "binding_id": _hmac_hex(
                    key,
                    _LOCATION_PURPOSE,
                    {
                        "name": event.name,
                        "location": location,
                        "event_index": event.event_index,
                    },
                ),
                "reference": "{{" + event.name + "}}",
                "name": event.name,
                "location": location,
                "source": source,
                "overridden_sources": state.overridden.get(event.name, []),
                "value_type": event.value_type,
                "rendered_preview": event.rendered_preview,
            }
        )

    auth_payload = dict(auth or {
        "required": bool(request.get("auth_required")),
        "status": "none" if not request.get("auth_required") else "unavailable",
        "injection_slots": [],
        "requires_worker_verification": bool(request.get("auth_required")),
    })
    auth_payload["injection_slots"] = [dict(item) for item in auth_payload.get("injection_slots", [])]
    auth_payload["required"] = bool(request.get("auth_required"))
    issues.extend(_slot_conflict_issues(key, request, auth_payload["injection_slots"]))
    ordinary_ready = not issues
    ready = ordinary_ready and auth_payload["status"] in {"none", "ready"}
    source_fingerprint = _source_fingerprint(
        key,
        source_kind=source_kind,
        source_id=source_id,
        request=request,
        assertions=assertions,
    )
    resolution_context = None
    variable_sources = [
        {
            "name": name,
            "source": state.sources[name],
            "overridden_sources": state.overridden.get(name, []),
        }
        for name in sorted(state.sources)
    ]
    context_fingerprint = _hmac_hex(
        key,
        _CONTEXT_FINGERPRINT_PURPOSE,
        {
            "scope": {
                "workspace_id": str(environment.workspace_id),
                "project_id": str(environment.project_id),
                "environment_id": str(environment.id),
                "principal_id": str(principal_id),
            },
            "source_fingerprint": source_fingerprint,
            "config_basis": state.basis,
            "variable_sources": variable_sources,
            "bindings": bindings,
        },
    )
    if ordinary_ready:
        resolution_context = _sign_context(
            key,
            {
                "kind": "resolution",
                "schema_version": 1,
                "workspace_id": str(environment.workspace_id),
                "project_id": str(environment.project_id),
                "environment_id": str(environment.id),
                "principal_id": str(principal_id),
                "source_fingerprint": source_fingerprint,
                "config_basis": state.basis,
                "context_fingerprint": context_fingerprint,
                "expires_at": int((datetime.now(UTC) + _CONTEXT_TTL).timestamp()),
            },
        )
    return {
        "schema_version": 1,
        "scope": {
            "workspace_id": str(environment.workspace_id),
            "project_id": str(environment.project_id),
            "environment_id": str(environment.id),
        },
        "ready": ready,
        "ordinary_resolution": "invalid" if issues else "ready",
        "masked_target": _masked_target(prepared) if prepared is not None else None,
        "bindings": bindings,
        "issues": issues,
        "auth": auth_payload,
        "config_basis": state.basis,
        "context_fingerprint": context_fingerprint,
        "resolution_context": resolution_context,
        "_snapshot": {
            "schema_version": 1,
            "config_basis": state.basis,
            "variable_sources": variable_sources,
            "bindings": bindings,
            "context_fingerprint": context_fingerprint,
        },
        "_merged_variables": state.merged,
    }


def verify_resolution_context(
    token: str,
    settings: Settings,
    *,
    environment: Environment,
    principal_id: uuid.UUID,
    request: dict,
    assertions: list[dict],
    source_kind: str,
    source_id: uuid.UUID | None,
    basis: dict,
) -> str:
    """校验解析依据并返回受理时应写入快照的 context fingerprint。"""
    key = settings.load_secret_key()
    try:
        body, encoded_signature = token.split(".", 1)
        supplied = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        canonical_signature = base64.urlsafe_b64encode(supplied).decode().rstrip("=")
        expected = hmac.new(
            key, _CONTEXT_PURPOSE.encode() + b"\0" + body.encode(), hashlib.sha256
        ).digest()
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ResolutionError(
            "resolution_context_changed", "解析依据无效，请重新预览后发送。"
        ) from error
    if not isinstance(payload, dict):
        raise ResolutionError("resolution_context_changed", "解析依据无效，请重新预览后发送。")
    if canonical_signature != encoded_signature or not hmac.compare_digest(expected, supplied):
        raise ResolutionError("resolution_context_changed", "解析依据无效，请重新预览后发送。")
    expected_source = _source_fingerprint(
        key,
        source_kind=source_kind,
        source_id=source_id,
        request=request,
        assertions=assertions,
    )
    expected_fields = {
        "kind": "resolution",
        "schema_version": 1,
        "workspace_id": str(environment.workspace_id),
        "project_id": str(environment.project_id),
        "environment_id": str(environment.id),
        "principal_id": str(principal_id),
        "source_fingerprint": expected_source,
        "config_basis": basis,
    }
    if any(payload.get(name) != value for name, value in expected_fields.items()):
        raise ResolutionError("resolution_context_changed", "配置或请求已变化，请重新预览后发送。")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= int(datetime.now(UTC).timestamp()):
        raise ResolutionError("resolution_context_changed", "解析依据已过期，请重新预览后发送。")
    fingerprint = payload.get("context_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ResolutionError("resolution_context_changed", "解析依据无效，请重新预览后发送。")
    return fingerprint


def public_resolution(value: dict) -> dict:
    """剥离仅供协调器使用的内部值，返回公开 DTO。"""
    return {key: item for key, item in value.items() if not key.startswith("_")}


__all__ = [
    "ResolutionError",
    "build_resolution",
    "load_variable_state",
    "public_resolution",
    "variable_context",
    "verify_resolution_context",
]
