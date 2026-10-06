from __future__ import annotations

import uuid

import pytest

from app.kernel.request_spec import prepare, validate_request
from app.kernel.variables import VariableResolutionError, build_resolver, variable_references
from app.services.debug_context import (
    build_resolution_proof,
    read_resolution_proof,
    stamp_resolution_proof,
)
from app.services.resolution import _masked_target


def test_variable_reference_offsets_can_be_projected_to_browser_utf16() -> None:
    text = "😀前{{地区.代码}}后"
    reference = variable_references(text)[0]
    assert reference.name == "地区.代码"
    assert text[reference.start : reference.end] == "{{地区.代码}}"
    assert len(text[: reference.start].encode("utf-16-le")) // 2 == 3


def test_resolution_proof_requires_new_snapshot_and_exact_worker_stamp() -> None:
    key = b"k" * 32
    workspace_id, project_id, principal_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    snapshot = {
        "request": {"method": "GET", "path": "/{{id}}"},
        "variables": {"id": {"type": "number", "text": "9007199254740993"}},
        "resolution": {"schema_version": 1, "context_fingerprint": "context-hmac"},
    }
    proof = build_resolution_proof(
        key,
        workspace_id=workspace_id,
        project_id=project_id,
        principal_id=principal_id,
        snapshot=snapshot,
    )
    assert proof is not None
    evidence = stamp_resolution_proof({"method": "GET"}, proof)
    assert read_resolution_proof(
        evidence,
        snapshot,
        key,
        workspace_id=workspace_id,
        project_id=project_id,
        principal_id=principal_id,
    ) == {
        "schema_version": 1,
        "guard": "ordinary_binding_enforced_v1",
        "context_fingerprint": "context-hmac",
        "binding_fingerprint": proof["binding_fingerprint"],
    }
    changed = {**snapshot, "variables": {"id": {"type": "number", "text": "1"}}}
    assert (
        read_resolution_proof(
            evidence,
            changed,
            key,
            workspace_id=workspace_id,
            project_id=project_id,
            principal_id=principal_id,
        )
        is None
    )
    assert build_resolution_proof(
        key,
        workspace_id=workspace_id,
        project_id=project_id,
        principal_id=principal_id,
        snapshot={"request": {}, "variables": {}},
    ) is None


def test_prepare_binding_events_have_stable_global_occurrence_indices() -> None:
    missing_events = []
    missing_request = validate_request(
        {
            "method": "POST",
            "path": "/echo",
            "body_type": "json",
            "body": '{"x":"{{缺失甲}}-{{缺失乙}}-{{缺失甲}}"}',
        }
    )
    with pytest.raises(VariableResolutionError):
        prepare(
            missing_request,
            "http://echo:8080",
            build_resolver([]),
            binding_events=missing_events,
        )
    assert [event.name for event in missing_events] == ["缺失甲", "缺失乙", "缺失甲"]
    assert [event.event_index for event in missing_events] == [0, 1, 2]
    assert len({str(event.location) for event in missing_events}) == 1

    resolver = build_resolver(
        [{"name": "重复", "value": {"type": "string", "text": "v"}}]
    )
    form_events = []
    prepare(
        validate_request(
            {
                "method": "POST",
                "path": "/{{重复}}",
                "body_type": "form",
                "body": "first={{重复}}&second={{重复}}",
            }
        ),
        "http://echo:8080",
        resolver,
        binding_events=form_events,
    )
    assert [event.event_index for event in form_events] == [0, 1, 2]
    assert [event.name for event in form_events] == ["重复", "重复", "重复"]


def test_masked_target_is_the_exact_prepared_ordinary_url() -> None:
    request = validate_request(
        {
            "method": "GET",
            "path": "/echo/{{路径}}",
            "query_params": [
                {"name": "dup", "value": "{{查询}}"},
                {"name": "dup", "value": ""},
                {"name": "中文", "value": "前{{查询}}后"},
            ],
            "body_type": "none",
            "body": "",
        }
    )
    resolver = build_resolver(
        [
            {"name": "路径", "value": {"type": "string", "text": "子 路径"}},
            {"name": "查询", "value": {"type": "string", "text": "甲 乙&?"}},
        ]
    )
    prepared = prepare(request, "http://echo:8080/base", resolver)
    target = _masked_target(prepared)
    assert target == {"method": "GET", "url": prepared.url}
    assert target["url"].startswith("http://echo:8080/base/echo/")
    assert target["url"].count("dup=") == 2
    assert "dup=&" in target["url"]
