"""历史未校验兼容解释的保守判据。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.routes.runs import _run_steps_out
from app.models import Run, RunStepAttempt


def _run(**overrides) -> Run:
    values = {
        "state": "finished",
        "outcome": "completed_unchecked",
        "target_type": "case_version",
        "reason_category": None,
    }
    values.update(overrides)
    return Run(**values)


def _attempt(number: int = 1, **overrides) -> RunStepAttempt:
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "step_key": "main",
        "attempt_no": number,
        "state": "finished",
        "outcome": "error",
        "send_intent_at": now,
        "started_at": now,
        "finished_at": now,
        "response": {"status": 503},
        "error_code": None,
        "elapsed_ms": 5,
    }
    values.update(overrides)
    return RunStepAttempt(**values)


def test_only_final_eligible_main_attempt_gets_interpretation() -> None:
    earlier = _attempt(1)
    final = _attempt(2)

    projected = _run_steps_out(_run(), [earlier, final])

    assert projected[0].interpretation is None
    assert projected[1].outcome == "error"
    assert projected[1].interpretation is not None
    assert projected[1].interpretation.outcome == "completed_unchecked"


@pytest.mark.parametrize(
    ("run_overrides", "attempt_overrides"),
    [
        ({"state": "running"}, {}),
        ({"outcome": "failed"}, {}),
        ({"target_type": "scenario"}, {}),
        ({"reason_category": "network"}, {}),
        ({}, {"step_key": "cleanup"}),
        ({}, {"state": "sending"}),
        ({}, {"outcome": None}),
        ({}, {"send_intent_at": None}),
        ({}, {"response": {"error": "transport"}}),
        ({}, {"response": {"status": True}}),
        ({}, {"response": {"status": 99}}),
        ({}, {"error_code": "read_timeout"}),
    ],
)
def test_incomplete_or_conflicting_evidence_is_not_interpreted(
    run_overrides: dict, attempt_overrides: dict
) -> None:
    projected = _run_steps_out(_run(**run_overrides), [_attempt(**attempt_overrides)])
    assert projected[0].interpretation is None


def test_direct_unchecked_value_is_not_marked_as_legacy() -> None:
    projected = _run_steps_out(_run(), [_attempt(outcome="completed_unchecked")])
    assert projected[0].outcome == "completed_unchecked"
    assert projected[0].interpretation is None


def test_ambiguous_attempt_numbers_disable_all_interpretations() -> None:
    projected = _run_steps_out(_run(), [_attempt(1), _attempt(1)])
    assert all(item.interpretation is None for item in projected)
