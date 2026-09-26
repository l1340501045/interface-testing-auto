"""断言定位信息的脱敏：固定字段名与枚举不能跟着用户内容一起被遮蔽。

根因是“把整个定位步骤当普通结构递归遮蔽”。步骤是判别联合
（`{"kind":"key","key":…}`／`{"kind":"index","index":…}`／
`{"kind":"repeat_key","key":…,"occurrence":…}`），其中 `kind`、`index`、
`occurrence` 是服务端常量与结构位置。合法凭证恰好等于 `key` 这类固定词时，递归
遮蔽会把 `{"kind":"key","key":"name"}` 改成 `{"kind":"***","***":"name"}`：报告里
既读不出核对的是哪个字段，机器也再解析不出这条定位。

遮蔽必须**按契约**落在用户／响应派生的那一格上：`key` 是字段名，可能是秘密本身，
照常遮蔽；数字索引与序号的语义原样保留。这里同时验证真正的落库出口
（`_persist_assertion_results`），而不是只测一个辅助函数。

这里不连接数据库：`AssertionResult` 是普通 ORM 对象，用一个只记录 `add` 的会话
替身即可拿到即将落库的那一份，无需真实事务。
"""
from __future__ import annotations

import uuid

from app.kernel.assertion_inputs import SourceRoots, pairs_root, paths_containing
from app.kernel.assertions import AssertionContext, AssertionOutcome
from app.kernel.fieldlocator import locate
from app.kernel.lossless_json import loads
from app.services.executor import (
    AssertionRecord,
    JobClaim,
    _evaluate_assertions,
    _persist_assertion_results,
    _sanitize_selector,
    describe_selector,
)

SECRET = "key"
# 固定词：凭证恰好等于它们时，任何一格都不许被改掉。
FIXED_WORDS = ["key", "kind", "index", "occurrence", "name", "value"]


def _step(kind: str, **extra: object) -> dict:
    return {"kind": kind, **extra}


class _RecordingSession:
    """只收集 `add` 的对象；`flush` 是空操作。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        pool_id=uuid.uuid4(),
        fencing_token=1,
        attempt=1,
    )


def _persist(assertions: list[dict], secrets: list[str]) -> list[dict]:
    """把断言走一遍真实落库出口，返回每条即将写入的 `target`。"""
    session = _RecordingSession()
    records = [
        AssertionRecord(item, "post_response", AssertionOutcome("failed", "not_equal"))
        for item in assertions
    ]
    _persist_assertion_results(session, _claim(), uuid.uuid4(), records, secrets)
    return [item.target for item in session.added]


def _assertion(selector: list[dict], source: str = "response.body") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "type": "equals",
        "target_source": source,
        "selector": selector,
        "parameters": {},
    }


# —— 固定字段名与枚举不受影响 ——


def test_selector_survives_a_credential_equal_to_a_fixed_word() -> None:
    """凭证恰好是 `key`：定位协议必须原样可读，不能被遮成 `{"***":"name"}`。"""
    selector = [
        _step("key", key="name"),
        _step("index", index=0),
        _step("repeat_key", key="item", occurrence=1),
    ]

    cleaned = _sanitize_selector(selector, ["key", "kind", "index", "occurrence"])

    assert cleaned == selector


def test_persisted_target_keeps_the_locating_contract_intact() -> None:
    """落库出口：固定词凭证不得把 `kind`／字段名砸成掩码。"""
    selector = [_step("key", key="name"), _step("index", index=2)]

    target = _persist([_assertion(selector)], ["key", "kind", "index"])[0]

    assert target["selector"] == selector
    # 可读路径是给人看的展示串，不受定位协议约束，照常遮蔽秘密字段名。
    assert target["path"] == "name[2]"


def test_numeric_index_and_occurrence_keep_their_semantics() -> None:
    """索引与序号是结构位置，不是可读文本：凭证等于 `0` 时也不能被替换。"""
    selector = [_step("index", index=0), _step("repeat_key", key="tag", occurrence=0)]

    cleaned = _sanitize_selector(selector, ["0"])

    assert cleaned[0]["index"] == 0
    assert cleaned[1]["occurrence"] == 0


def test_every_kind_enum_value_is_preserved() -> None:
    for kind in ("key", "index", "repeat_key"):
        selector = [_step(kind, key="f", index=1, occurrence=1)]
        cleaned = _sanitize_selector(selector, FIXED_WORDS)
        assert cleaned[0]["kind"] == kind, f"定位步骤类型被遮蔽：{kind}"


# —— 用户／响应派生的字段名照常遮蔽 ——


def test_a_field_name_that_is_a_secret_is_masked() -> None:
    """目标把凭据回显成字段名时，遮蔽落在 `key` 这一格上。"""
    selector = [_step("key", key=SECRET)]

    cleaned = _sanitize_selector(selector, [SECRET])

    assert cleaned == [{"kind": "key", "key": "***"}]


def test_persisted_target_masks_the_derived_key_but_keeps_its_neighbours() -> None:
    """同一条断言里，秘密键位被遮蔽，兄弟断言与固定字段毫发无损。"""
    secret_selector = [_step("key", key=SECRET)]
    safe_selector = [_step("key", key="token"), _step("index", index=0)]

    targets = _persist([_assertion(secret_selector), _assertion(safe_selector)], [SECRET])

    assert targets[0]["selector"] == [{"kind": "key", "key": "***"}]
    assert targets[1]["selector"] == safe_selector
    assert targets[1]["path"] == "token[0]"


def test_repeated_key_that_is_a_secret_is_masked_without_losing_the_occurrence() -> None:
    selector = [_step("repeat_key", key=SECRET, occurrence=3)]

    cleaned = _sanitize_selector(selector, [SECRET])

    assert cleaned == [{"kind": "repeat_key", "key": "***", "occurrence": 3}]


def test_unknown_fields_are_treated_as_user_content() -> None:
    """契约之外的多余字段按用户内容处理：宁可多遮，不能让它成为漏出口。"""
    selector = [_step("key", key="token", note=SECRET)]

    cleaned = _sanitize_selector(selector, [SECRET])

    assert cleaned == [{"kind": "key", "key": "token", "note": "***"}]


def test_mask_escaped_writing_of_a_derived_key_is_covered() -> None:
    """字段名以转义形态回显时同样遮蔽：形态由统一的脱敏原语决定。"""
    selector = [_step("key", key="\\u006bey")]  # 解码后是 `key`

    cleaned = _sanitize_selector(selector, [SECRET])

    assert cleaned == [{"kind": "key", "key": "***"}]


# —— 脱敏后的定位仍然可用 ——


def test_masked_selector_still_locates_and_unmasked_is_not_required_to() -> None:
    """遮蔽后的步骤仍是合法定位：`kind`／`index` 在，机器照样能解析这条路径。"""
    tree = loads('{"data":{"items":[{"id":1},{"id":2}]}}')
    selector = [_step("key", key="data"), _step("key", key="items"), _step("index", index=1)]

    cleaned = _sanitize_selector(selector, FIXED_WORDS)

    assert cleaned == selector
    assert locate(tree, cleaned).value["id"].text == "2"
    assert describe_selector(cleaned) == "data.items[1]"


# —— v2 稳定行：显示、结构身份与敏感策略 ——


def test_persisted_row_targets_stay_distinct_and_cover_whole_or_orphaned_rows() -> None:
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    assertions = [
        _assertion(
            [_step("row", row_id=first), _step("key", key="value")],
            "request.query",
        ),
        _assertion(
            [_step("row", row_id=second), _step("key", key="value")],
            "request.query",
        ),
        _assertion([_step("row", row_id=first)], "request.query"),
        # 孤立与否由实际 SourceRoots 决定；报告仍必须保留原稳定目标。
        _assertion([_step("row", row_id=str(uuid.uuid4()))], "request.query"),
        _assertion([_step("key", key="data"), _step("index", index=2)]),
        _assertion([_step("repeat_key", key="tag", occurrence=1)]),
    ]

    targets = _persist(assertions, [])

    assert targets[0]["path"] == f"row({first}).value"
    assert targets[1]["path"] == f"row({second}).value"
    assert targets[0]["path"] != targets[1]["path"]
    assert targets[2]["path"] == f"row({first})"
    assert targets[3]["path"].startswith("row(")
    assert targets[4]["path"] == "data[2]"
    assert targets[5]["path"] == ".tag#1"


def test_persisted_valid_row_id_survives_secret_substring_but_dynamic_fields_do_not() -> None:
    row_id = "11111111-1111-4111-8111-111111111111"
    selector = [_step("row", row_id=row_id, note="1111"), _step("key", key="1111")]

    target = _persist([_assertion(selector, "request.query")], ["1111"])[0]

    assert target["selector"][0] == {"kind": "row", "row_id": row_id, "note": "***"}
    assert target["selector"][1] == {"kind": "key", "key": "***"}
    assert uuid.UUID(target["selector"][0]["row_id"]) == uuid.UUID(row_id)


def test_invalid_unvalidated_row_id_is_still_treated_as_dynamic_text() -> None:
    cleaned = _sanitize_selector([_step("row", row_id="secret-row")], ["secret"])
    assert cleaned == [{"kind": "row", "row_id": "***-row"}]


def test_assertion_on_a_sensitive_value_through_row_alias_is_policy_rejected() -> None:
    row_id = str(uuid.uuid4())
    roots = SourceRoots()
    roots.add_tree(
        "request.query",
        pairs_root([{"name": "token", "value": "credential-secret"}]),
        row_indices={row_id: 0},
    )
    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(roots, ["credential-secret"])
    assertion = {
        **_assertion(
            [_step("row", row_id=row_id), _step("key", key="value")],
            "request.query",
        ),
        "type": "exists",
        "parameters": {},
        "severity": "error",
        "enabled": True,
    }

    records = _evaluate_assertions([assertion], "pre_request", roots, ctx)

    assert records[0].outcome.status == "error"
    assert records[0].outcome.reason_code == "policy_rejected"
