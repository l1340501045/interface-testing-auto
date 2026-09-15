"""恢复分支不得产生来源证明：保护未执行的尝试没有 context。

对应 R1 第 3 项。旧执行器留下“已写发送意图、没有结果”的历史记录后，新执行器重新领取
时直接判 interrupted——这条路径**没有走到**本轮的环境冻结与必需认证判定，请求也从未受
它们约束。若通用持久化函数无条件盖上语义标记，这份报告就会给出一个它没有依据的结论：
读者会以为这次执行是受保护的，而实际上什么都没检查过。

这里同时锁定两个方向：
- 未执行的恢复分支：`context` 必须是 null；
- 真正跑过保护的运行：`context` 必须存在（否则“保守”就退化成“永不提供证明”）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness import create_case, create_environment, get_report, publish_case, start_run

pytestmark = pytest.mark.integration


def _echo_request() -> dict:
    return {"method": "GET", "path": "/echo", "body_type": "none", "body": ""}


def test_recovered_run_without_protection_has_no_context(
    client: TestClient, account: dict, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧意图恢复出的 interrupted 记录：报告不得带 context。"""
    from app.db import get_session_factory
    from app.services import executor

    environment = create_environment(client, account, project, name="恢复无证明环境")
    case = create_case(
        client, account, project, name="恢复用例", request=_echo_request()
    )
    version = publish_case(client, account, project, case["id"], side_effect="write")
    run = start_run(
        client,
        account,
        project,
        {"environment_id": environment["id"], "case_version_id": version["id"]},
    )

    # 造出“意图已写、结果未落”的历史：模拟旧执行器在这一步之后退出。
    real_send = executor._send

    class _Crash(BaseException):
        """真实进程被杀不会被业务 except 捕获，因此继承 BaseException。"""

    def crash_after_send(settings, pin, prepared):
        real_send(settings, pin, prepared)
        raise _Crash("worker killed after the request reached the target")

    from test_execution_flow import _claim, _execute, _expire_lease, _settings

    monkeypatch.setattr(executor, "_send", crash_after_send)
    claim = _claim(project["pool_id"])
    assert claim is not None
    with pytest.raises(_Crash):
        executor.execute_claim(get_session_factory(), _settings(), claim)

    _expire_lease(run["id"])
    monkeypatch.setattr(executor, "_send", real_send)
    replay_claim = _claim(project["pool_id"], worker_id="it-context-worker-2")
    assert replay_claim is not None, "租约过期后应能被重新领取"
    assert _execute(replay_claim, "it-context-worker-2") == "interrupted"

    report = get_report(client, account, project, run["id"])
    assert report["run"]["outcome"] == "interrupted"
    # 关键断言：恢复分支没有执行本轮保护，因此给不出来源证明。
    assert report["context"] is None, (
        "未执行保护的恢复尝试不得提供 context；"
        "否则报告会用一条没有依据的标记把这次执行说成受过保护"
    )


def test_run_that_evaluated_protection_does_have_context(
    client: TestClient, account: dict, project: dict
) -> None:
    """真正走完保护的运行必须给出 context：保守不等于永不提供证明。"""
    environment = create_environment(client, account, project, name="正常证明环境")
    snapshot = {"request": _echo_request(), "assertions": []}
    run = start_run(
        client, account, project, {"environment_id": environment["id"], "debug_snapshot": snapshot}
    )

    from test_credentials_flow import _run_once

    assert _run_once(project["pool_id"]) in ("passed", "completed_unchecked")
    report = get_report(client, account, project, run["id"])
    assert report["context"] is not None
    assert report["context"]["environment"]["id"] == environment["id"]
    # 语义标记是内部实现细节，不得出现在公开报告里。
    assert "__platform_guard" not in str(report)
