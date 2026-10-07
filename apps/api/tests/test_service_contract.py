from __future__ import annotations

import pytest

from app.api.errors import ApiError
from app.api.request_contract import require_service_capability
from app.kernel.request_spec import ServiceSpecError, validate_request


def _request(**extra):
    return {
        "method": "GET",
        "path": "/echo",
        "query_params": [],
        "headers": [],
        "body_type": "none",
        "body": "",
        **extra,
    }


def test_default_request_remains_byte_shape_without_service_fields() -> None:
    normalized = validate_request(_request())
    assert "service_contract" not in normalized
    assert "service_key" not in normalized


@pytest.mark.parametrize("row_schema", [None, 2])
def test_named_service_contract_is_orthogonal_to_row_schema(row_schema: int | None) -> None:
    payload = _request(
        service_contract=1,
        service_key="svc_22222222222222222222222222222222",
    )
    if row_schema == 2:
        payload.update(schema_version=2, query_params=[])
    normalized = validate_request(payload)
    assert normalized["service_contract"] == 1
    assert normalized["service_key"].startswith("svc_")
    assert normalized.get("schema_version") == row_schema


@pytest.mark.parametrize(
    "extra",
    [
        {"service_contract": 1},
        {"service_key": "svc_33333333333333333333333333333333"},
        {"service_contract": True, "service_key": "svc_44444444444444444444444444444444"},
        {"service_contract": 2, "service_key": "svc_55555555555555555555555555555555"},
        {"service_contract": 1, "service_key": "default"},
        {"service_contract": 1, "service_key": "{{服务}}"},
    ],
)
def test_invalid_named_service_presence_is_rejected(extra: dict) -> None:
    with pytest.raises(ServiceSpecError):
        validate_request(_request(**extra))


def test_service_capability_is_independent_and_exact() -> None:
    assert require_service_capability(None, needed=False) is False
    assert require_service_capability("1", needed=True) is True
    with pytest.raises(ApiError) as rejected:
        require_service_capability("2", needed=True)
    assert (rejected.value.status_code, rejected.value.code) == (
        409,
        "service_contract_required",
    )
