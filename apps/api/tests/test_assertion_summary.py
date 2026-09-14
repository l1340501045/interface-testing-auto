"""运行终态汇总：必需条件没跑完就不能算通过。

多条件默认**全部满足**。汇总只按“有没有至少一条响应条件被执行过”判定时，同一行上
两条同级（严重级别 error）条件可以出现一条 passed、一条 skipped，
而整体仍是 passed——一次没做完的检查看起来像是通过了。这里锁定的是汇总语义本身，
不是某一个条件类型的补丁：

- 有必需条件未完成评估 → 未校验（`completed_unchecked`），并给出原因；
- 显式禁用的条件不参与（它不表达任何预期，不能把检查拖成未校验）；
- 严重级别 warning 的条件跳过不阻塞（非必需）；
- 只有入参断言或没有断言 → 未校验（原因仍为空，保持既有语义）；
- 真正失败、真配置错误各自保持自身语义，不被这条新规则吞掉。

这里只验证纯函数，不连接数据库、不发送请求。
"""
from __future__ import annotations

from app.kernel.assertions import AssertionOutcome
from app.services.executor import AssertionRecord, _required_unchecked, _summarize


def _record(
    status: str,
    *,
    severity: str = "error",
    reason_code: str | None = None,
    phase: str = "post_response",
) -> AssertionRecord:
    return AssertionRecord(
        {
            "id": f"{phase}-{status}-{severity}",
            "target_source": "response.body",
            "selector": [],
            "type": "exists",
            "parameters": {},
            "enabled": True,
            "severity": severity,
        },
        phase,
        AssertionOutcome(status, reason_code, message=""),
    )


_EXECUTED = ("passed", "failed", "error")


# —— 必需条件未完成评估 ——


def test_a_skipped_required_condition_is_never_a_pass() -> None:
    """一条必需条件 passed、另一条同级条件 skipped：整体不得是 passed。"""
    outcome, reason, error_code = _summarize([], [_record("passed"), _record("skipped")])

    assert outcome == "completed_unchecked"
    assert reason == "assertion"
    assert error_code == "assertion_not_evaluated"


def test_a_skipped_required_input_condition_is_never_a_pass_either() -> None:
    """入参侧的必需条件同理：只按响应侧统计会漏掉这一半。"""
    outcome, _, _ = _summarize(
        [_record("skipped", phase="pre_request")],
        [_record("passed")],
    )

    assert outcome == "completed_unchecked"


def test_the_unchecked_condition_is_reported_with_its_reason_code() -> None:
    """跳过的那条本身仍保留自己的原因码，报告里读得到是哪一类来源不可用。"""
    records = [_record("passed"), _record("skipped", reason_code="source_unavailable")]

    assert _required_unchecked(records) is True
    assert records[1].outcome.reason_code == "source_unavailable"


def test_all_conditions_executing_and_passing_is_still_a_pass() -> None:
    """全部条件正常求值通过：照常 passed，不受新规则影响。"""
    outcome, reason, error_code = _summarize([], [_record("passed"), _record("passed")])

    assert (outcome, reason, error_code) == ("passed", None, None)


def test_a_warning_condition_being_skipped_does_not_block_the_pass() -> None:
    """warning 不是必需条件：跳过它不影响终态。"""
    outcome, _, _ = _summarize(
        [],
        [_record("passed"), _record("skipped", severity="warning")],
    )

    assert outcome == "passed"


def test_only_input_assertions_keeps_the_existing_unchecked_semantics() -> None:
    """只有入参断言或没有断言：仍是未校验，原因保持为空（既有语义不变）。"""
    assert _summarize([_record("passed", phase="pre_request")], []) == (
        "completed_unchecked",
        None,
        None,
    )
    assert _summarize([], []) == ("completed_unchecked", None, None)


def test_disabled_conditions_never_enter_the_records() -> None:
    """显式禁用的条件在求值前就被过滤，因此不会把一次正常检查拖成未校验。"""
    outcome, _, _ = _summarize([], [_record("passed")])

    assert _required_unchecked([]) is False
    assert outcome == "passed"


# —— 既有语义不被新规则吞掉 ——


def test_a_real_failure_is_still_a_failure() -> None:
    """失败优先于“未完成评估”：一条失败已是确定结论，不该被降级成未校验。"""
    outcome, reason, error_code = _summarize(
        [], [_record("failed", reason_code="value_mismatch"), _record("skipped")]
    )

    assert (outcome, reason, error_code) == ("failed", "assertion", "value_mismatch")


def test_a_configuration_error_outranks_everything_else() -> None:
    """求值报错（配置或数据问题）优先于失败与未校验。"""
    outcome, reason, error_code = _summarize(
        [],
        [
            _record("error", reason_code="config_invalid"),
            _record("failed"),
            _record("skipped"),
        ],
    )

    assert (outcome, reason, error_code) == ("error", "configuration", "config_invalid")


def test_a_warning_failure_does_not_block() -> None:
    """非阻塞失败照旧放行，未校验规则不应顺手把 warning 当成阻塞项。"""
    outcome, _, _ = _summarize([], [_record("passed"), _record("failed", severity="warning")])

    assert outcome == "passed"
