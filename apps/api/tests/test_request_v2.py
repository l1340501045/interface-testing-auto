"""请求格式 v2：稳定行、停用语义与 v1 兼容。"""
from __future__ import annotations

import uuid

import pytest

from app.kernel.assertion_spec import AssertionSpecError, validate_assertions
from app.kernel.request_spec import RequestSpecError, prepare, validate_request
from app.kernel.variables import VariableResolver
from app.services.executor import _build_request_roots
from app.services.run_coordinator import debug_snapshot_digest


def _row(
    *,
    row_id: str | None = None,
    name: str = "q",
    value: str = "value",
    enabled: bool = True,
    description: str = "",
) -> dict:
    return {
        "row_id": row_id or str(uuid.uuid4()),
        "name": name,
        "value": value,
        "enabled": enabled,
        "description": description,
    }


def _v2(*, query: list[dict] | None = None, headers: list[dict] | None = None) -> dict:
    return {
        "schema_version": 2,
        "method": "GET",
        "path": "/echo",
        "query_params": query or [],
        "headers": headers or [],
        "body_type": "none",
        "body": "",
    }


def _assertion(source: str, selector: list[dict]) -> dict:
    return {
        "id": "stable-row",
        "target_source": source,
        "selector": selector,
        "type": "exists",
        "parameters": {},
        "severity": "error",
    }


def test_v1_shape_stays_exact_and_rejects_unmarked_metadata() -> None:
    original = {
        "method": "get",
        "path": "echo",
        "query_params": [{"name": "a", "value": "1"}],
        "headers": [],
        "body_type": "none",
        "body": "ignored",
        "auth_required": False,
    }
    normalized = validate_request(original)
    assert normalized == {
        "method": "GET",
        "path": "/echo",
        "query_params": [{"name": "a", "value": "1"}],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    # 升级前固定样例的授权摘要；期望值是静态常量，不由新 normalizer 生成。
    assert debug_snapshot_digest(normalized, []) == (
        "44c1973fbb95b656d78b99c64fec0e4c5d80890c5edb527b4a0363e8ce33f2cd"
    )
    with pytest.raises(RequestSpecError, match="schema_version: 2"):
        validate_request({**original, "query_params": [_row()]})


def test_v2_requires_complete_unique_rows_and_enforces_limits() -> None:
    row = _row(description="说明")
    assert validate_request(_v2(query=[row]))["query_params"] == [row]

    with pytest.raises(RequestSpecError, match="row_id 重复"):
        validate_request(_v2(query=[row], headers=[{**row, "name": "X-Test"}]))
    with pytest.raises(RequestSpecError, match="description 超过 1024"):
        validate_request(_v2(query=[_row(description="字" * 1025)]))
    with pytest.raises(RequestSpecError, match="合计最多 500"):
        validate_request(_v2(query=[_row() for _ in range(501)]))
    with pytest.raises(RequestSpecError, match="enabled 必须是布尔值"):
        validate_request(_v2(query=[{**_row(), "enabled": "false"}]))
    for unsupported in (None, 1, True, 2.0):
        with pytest.raises(RequestSpecError, match="不支持的请求协议版本"):
            validate_request({**_v2(), "schema_version": unsupported})


def test_disabled_rows_are_removed_before_variable_resolution_and_description_is_inert() -> None:
    enabled = _row(name="kept", value="ok", description="{{missing_description}}")
    disabled = _row(name="disabled", value="{{missing_value}}", enabled=False)
    spec = validate_request(_v2(query=[disabled, enabled]))

    prepared = prepare(spec, "http://echo:8080", VariableResolver({}))

    assert prepared.query == [("kept", "ok")]
    assert prepared.query_row_indices == {enabled["row_id"]: 0}
    assert disabled["row_id"] not in prepared.query_row_indices
    assert "description" not in prepared.query_as_pairs()[0]


def test_row_locator_uses_the_actual_pair_index_and_missing_never_falls_back() -> None:
    disabled = _row(name="same", value="disabled", enabled=False)
    target = _row(name="same", value="actual")
    spec = validate_request(_v2(query=[disabled, target]))
    prepared = prepare(
        spec,
        "http://echo:8080",
        VariableResolver({}),
        injected_query=[("auth", "secret")],
    )
    roots = _build_request_roots(prepared, spec)
    selector = [
        {"kind": "row", "row_id": target["row_id"]},
        {"kind": "key", "key": "value"},
    ]

    assert roots.extract("request.query", selector) == (True, "actual")
    assert roots.coordinate("request.query", selector) == ("request.query", 0, "value")
    missing = [{"kind": "row", "row_id": str(uuid.uuid4())}]
    assert roots.extract("request.query", missing) == (False, None)
    assert roots.coordinate("request.query", missing) is None


def test_row_locator_is_jointly_validated_with_request_version_and_source() -> None:
    row = _row()
    selector = [{"kind": "row", "row_id": row["row_id"]}]
    normalized = validate_assertions([_assertion("request.query", selector)], _v2(query=[row]))
    assert normalized[0]["selector"] == selector

    with pytest.raises(AssertionSpecError, match="schema_version: 2"):
        validate_assertions([_assertion("request.query", selector)], {"method": "GET"})
    with pytest.raises(AssertionSpecError, match="只允许用于"):
        validate_assertions([_assertion("response.header", selector)], _v2(query=[row]))
    with pytest.raises(AssertionSpecError, match="第一步"):
        validate_assertions(
            [
                _assertion(
                    "request.query",
                    [{"kind": "index", "index": 0}, *selector],
                )
            ],
            _v2(query=[row]),
        )
