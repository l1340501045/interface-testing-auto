"""用例资产 S1 的纯合同：游标绑定与保存视图白名单。"""
from __future__ import annotations

import base64
import uuid

import pytest
from pydantic import ValidationError

from app.api.deps import Principal, ProjectScope
from app.api.errors import ApiError
from app.api.schemas import CaseViewFilters
from app.config import Settings
from app.services.case_assets import _decode_cursor, _encode_cursor


def _settings() -> Settings:
    return Settings(secret_key=base64.urlsafe_b64encode(b"asset-cursor-key-material-32byte").decode())


def _scope() -> ProjectScope:
    return ProjectScope(
        workspace_id=uuid.UUID("00000000-0000-4000-8000-000000000101"),
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000102"),
        role="viewer",
        principal=Principal(
            user_id=uuid.UUID("00000000-0000-4000-8000-000000000103"),
            username="cursor-user",
            display_name="游标用户",
            is_admin=False,
            session_id=uuid.UUID("00000000-0000-4000-8000-000000000104"),
        ),
    )


def test_cursor_is_bound_to_scope_filter_sort_and_canonical_signature() -> None:
    scope = _scope()
    payload = {
        "v": 1,
        "kind": "cases",
        "workspace_id": str(scope.workspace_id),
        "project_id": str(scope.project_id),
        "principal_id": str(scope.principal.user_id),
        "filter": "filter-a",
        "sort": "updated_desc",
        "position": ["2026-10-04T00:00:00+00:00", "00000000-0000-4000-8000-000000000105"],
    }
    token = _encode_cursor(_settings(), payload)
    assert _decode_cursor(
        _settings(), token, kind="cases", scope=scope,
        filter_digest="filter-a", sort="updated_desc",
    ) == payload["position"]

    for changed in (
        {"filter_digest": "filter-b", "sort": "updated_desc"},
        {"filter_digest": "filter-a", "sort": "name_asc"},
    ):
        with pytest.raises(ApiError) as rejected:
            _decode_cursor(_settings(), token, kind="cases", scope=scope, **changed)
        assert rejected.value.code == "invalid_cursor"

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ApiError) as rejected:
        _decode_cursor(
            _settings(), tampered, kind="cases", scope=scope,
            filter_digest="filter-a", sort="updated_desc",
        )
    assert rejected.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": 1, "folder": "exact"},
        {"schema_version": 1, "folder": "all", "include_descendants": True},
        {"schema_version": 1, "collection": "all", "sort": "recent_desc"},
        {"schema_version": 1, "cursor": "视图禁止保存分页游标"},
    ],
)
def test_saved_view_filters_reject_invalid_combinations_and_unknown_fields(value: dict) -> None:
    with pytest.raises(ValidationError):
        CaseViewFilters.model_validate(value)


def test_saved_view_filters_only_emit_versioned_query_conditions() -> None:
    filters = CaseViewFilters.model_validate(
        {
            "schema_version": 1,
            "q": "订单",
            "folder": "exact",
            "folder_id": "00000000-0000-4000-8000-000000000105",
            "include_descendants": True,
            "sort": "name_asc",
        }
    )
    assert filters.model_dump(mode="json") == {
        "schema_version": 1,
        "q": "订单",
        "method": None,
        "state": "active",
        "folder": "exact",
        "folder_id": "00000000-0000-4000-8000-000000000105",
        "include_descendants": True,
        "collection": "all",
        "sort": "name_asc",
    }
