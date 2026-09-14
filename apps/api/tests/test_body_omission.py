"""响应正文的省略策略：格式或完整性无法确认的正文整段不写，也不参与求值。

根因是“解析失败退回文本替换”这条出口。目标返回的正文本身可能是**残缺**的
（`{"token":"abc`，凭据是 `abcdef`），此时按原文替换查不到完整秘密，文本出口就
把带凭据前缀的片段原样放进了报告。判据必须是“这段正文能不能被**完整**处理”，
而不是“有没有命中完整秘密”：没拿全的正文，谁也无法证明缺的部分不含凭据。

省略必须是**明确**的：报告里要读得出为什么这段没有正文（`body_omitted_reason`），
并且断言不得再读到那段可疑原文——否则会出现“报告里没有、断言真假却能逐位猜出
秘密”的通道。状态码与非敏感响应头是另一批来源，照常可用。

这里只验证纯方法，不连接数据库、不发送请求。
"""
from __future__ import annotations

import httpx

from app.kernel.assertion_inputs import SourceRoots, paths_containing
from app.kernel.assertions import AssertionContext
from app.kernel.request_spec import prepare
from app.kernel.variables import VariableResolver
from app.services.executor import (
    OMITTED_UNPARSABLE_JSON,
    _build_response_roots,
    _declares_json,
    _evaluate_assertions,
    _expects_json_body,
    _stored_body,
)

_BASE = "http://echo:8080"
# 凭据本身是完整的；正文只回显了它的前三个字符——这正是“查完整秘密查不到”的那类。
SECRET = "abcdef"
TRUNCATED_JSON = '{"token":"abc'
SPEC = {
    "method": "GET",
    "path": "/echo",
    "query_params": [],
    "headers": [],
    "body_type": "none",
    "body": "",
}


def _prepared():
    return prepare(SPEC, _BASE, VariableResolver({}))


def _response(body: str, content_type: str | None) -> httpx.Response:
    headers = {"content-type": content_type} if content_type else {}
    return httpx.Response(200, headers=headers, content=body.encode("utf-8"))


# —— 省略判据：能不能完整处理，而不是有没有命中完整秘密 ——


def test_natively_truncated_json_body_is_omitted_entirely() -> None:
    """目标自己返回的残缺 JSON：凭据只剩前三个字符，也必须整段省略。"""
    result = _stored_body(TRUNCATED_JSON, [SECRET])

    assert result.text is None
    assert result.omitted_reason == OMITTED_UNPARSABLE_JSON
    assert result.format is None


def test_body_declared_as_json_must_parse_even_if_it_does_not_look_like_json() -> None:
    """目标声明 JSON 却给出无法解析的内容：声明本身就是可靠的格式上下文。"""
    result = _stored_body("abcdef", [SECRET], json_expected=True)

    assert result.text is None
    assert result.omitted_reason == OMITTED_UNPARSABLE_JSON


def test_json_document_shape_alone_is_enough_to_omit() -> None:
    """`Content-Type` 写错成 text/plain 也挡不住省略：形态已经是没处理完的 JSON。"""
    result = _stored_body('["abc', [SECRET], json_expected=False)

    assert result.text is None
    assert result.omitted_reason == OMITTED_UNPARSABLE_JSON


def test_a_truncated_escape_writing_in_a_broken_body_never_reaches_the_report() -> None:
    """残缺正文里的转义写法同样整段省略，不是“遮掉那一处再放行其余”。"""
    body = '{"token":"\\u0061bc","next":'

    result = _stored_body(body, ["abc"])

    assert result.text is None
    assert result.omitted_reason == OMITTED_UNPARSABLE_JSON


def test_genuine_plain_text_is_still_stored_with_its_format_context() -> None:
    """真正的纯文本照常入库，并带上“凭什么按文本处理”的上下文。"""
    body = "前缀 abcdef 后缀，路径 C:\\temp"

    result = _stored_body(body, [SECRET])

    assert result.omitted_reason is None
    assert result.format == "text"
    assert SECRET not in result.text
    assert "C:\\temp" in result.text, "与秘密无关的普通文本不因脱敏改变"


def test_empty_body_is_not_treated_as_a_broken_json_document() -> None:
    """空正文没有“没处理完”的部分：它是空，不是可疑片段。"""
    result = _stored_body("", [SECRET], json_expected=True)

    assert result.text == ""
    assert result.omitted_reason is None


def test_json_media_type_detection_covers_suffixes_and_ignores_parameters() -> None:
    assert _declares_json("application/json") is True
    assert _declares_json("Application/JSON; charset=utf-8") is True
    assert _declares_json("application/vnd.api+json") is True
    assert _declares_json("text/json") is True
    assert _declares_json("text/plain") is False
    assert _declares_json("application/jsonx") is False
    assert _declares_json(None) is False


# —— 用例的 JSON 预期：类型头缺失或写错时，格式上下文来自条件本身 ——

# 残缺的 JSON **字符串根**：既不以 `{`／`[` 开头，也不是一份能解析的文档。目标把它
# 标成 text/plain（或干脆不带类型头）时，只看类型头和首字符就会把它当纯文本放行。
TRUNCATED_STRING_ROOT = '"abc'


def test_only_enabled_conditions_on_the_parsed_body_express_a_json_expectation() -> None:
    """只有**启用中**、且落在解析后正文上的条件才算表达 JSON 预期。"""
    assert _expects_json_body([_assertion("response.body", "exists")]) is True
    assert _expects_json_body([_assertion("response.text", "contains")]) is False
    disabled = {**_assertion("response.body", "exists"), "enabled": False}
    assert _expects_json_body([disabled]) is False, "显式禁用的条件不表达任何预期"
    assert _expects_json_body([]) is False


def test_a_condition_on_the_parsed_body_omits_a_body_that_cannot_be_parsed() -> None:
    """用例对 `response.body` 提了条件：这份正文就该是 JSON，解析不了就整段省略。"""
    result = _stored_body(TRUNCATED_STRING_ROOT, [SECRET], json_expected=True)

    assert result.text is None
    assert result.omitted_reason == OMITTED_UNPARSABLE_JSON
    assert result.format is None


def test_the_same_body_without_a_json_expectation_is_handled_as_plain_text() -> None:
    """没有 JSON 预期时判据不变：确实的纯文本仍按文本出口处理（不扩大扫描范围）。"""
    result = _stored_body(TRUNCATED_STRING_ROOT, [SECRET], json_expected=False)

    assert result.format == "text"
    assert result.omitted_reason is None


def test_the_omission_does_not_depend_on_the_secret_being_present() -> None:
    """带凭证与不带凭证都是同一条判据：省略的是“没拿全的正文”，不是“命中了秘密”。"""
    with_secret = _stored_body(TRUNCATED_STRING_ROOT, [SECRET], json_expected=True)
    without_secret = _stored_body(TRUNCATED_STRING_ROOT, [], json_expected=True)

    assert with_secret.omitted_reason == without_secret.omitted_reason
    assert with_secret.text is None and without_secret.text is None


def test_a_typed_body_with_a_wrong_media_type_is_omitted_end_to_end() -> None:
    """端到端：类型头写错、正文是残缺字符串根时，两个正文来源一起省略，状态码仍可评估。"""
    stored = _stored_body(TRUNCATED_STRING_ROOT, [SECRET], json_expected=True)
    roots = _build_response_roots(
        _prepared(),
        SPEC,
        _response(TRUNCATED_STRING_ROOT, "text/plain"),
        TRUNCATED_STRING_ROOT,
        3,
        stored,
    )

    assert roots.omitted_reason("response.text") == OMITTED_UNPARSABLE_JSON
    assert roots.omitted_reason("response.body") == OMITTED_UNPARSABLE_JSON
    records = _evaluate_assertions(
        [
            # 配置在解析后正文上的条件无法求值：错误，而不是静默跳过。
            _assertion("response.body", "exists", selector=[{"kind": "key", "key": "token"}]),
            # 安全来源（状态码）不受正文省略影响。
            _assertion(
                "response.status", "equals", {"expected": {"type": "number", "text": "200"}}
            ),
        ],
        "post_response",
        roots,
        AssertionContext(),
    )
    assert [record.outcome.status for record in records] == ["error", "passed"]
    assert records[0].outcome.reason_code == "source_omitted"
    assert records[0].outcome.actual is None, "不允许把被省略的原文带进结果"


def test_a_body_without_a_type_header_still_honours_the_json_expectation() -> None:
    """目标连类型头都不带：格式上下文只剩用例的条件，判据同样成立。"""
    stored = _stored_body(TRUNCATED_STRING_ROOT, [SECRET], json_expected=True)
    roots = _build_response_roots(
        _prepared(), SPEC, _response(TRUNCATED_STRING_ROOT, None), TRUNCATED_STRING_ROOT, 3, stored
    )

    assert roots.omitted_reason("response.body") == OMITTED_UNPARSABLE_JSON


# —— 省略之后：断言不得再读到那段可疑原文，安全来源照常可用 ——


def _assertion(
    source: str, operator: str, params: dict | None = None, selector: list[dict] | None = None
) -> dict:
    return {
        "id": "a-1",
        "target_source": source,
        "selector": selector or [],
        "type": operator,
        "parameters": params or {},
        "enabled": True,
        "severity": "error",
    }


_CONTENT_TYPE_SELECTOR = [
    {"kind": "repeat_key", "key": "content-type", "occurrence": 0},
]


def test_omitted_body_sources_are_not_readable_by_assertions() -> None:
    stored = _stored_body(TRUNCATED_JSON, [SECRET])
    roots = _build_response_roots(
        _prepared(), SPEC, _response(TRUNCATED_JSON, "application/json"), TRUNCATED_JSON, 3, stored
    )

    # 正文来源被明确省略，而不是“不存在”：报告里读得出原因。
    assert roots.omitted_reason("response.text") == OMITTED_UNPARSABLE_JSON
    assert roots.omitted_reason("response.body") == OMITTED_UNPARSABLE_JSON
    assert not roots.has("response.text")
    assert not roots.has("response.body")

    records = _evaluate_assertions(
        [
            _assertion("response.text", "contains", {"expected": "abc"}),
            _assertion("response.body", "not_exists"),
        ],
        "post_response",
        roots,
        AssertionContext(),
    )
    for record in records:
        assert record.outcome.status == "error"
        assert record.outcome.reason_code == "source_omitted"
        assert record.outcome.actual is None, "不允许把被省略的原文带进结果"


def test_safe_sources_still_evaluate_when_the_body_is_omitted() -> None:
    """状态码与非敏感响应头不含正文内容，省略正文不影响它们照常求值。"""
    stored = _stored_body(TRUNCATED_JSON, [SECRET])
    roots = _build_response_roots(
        _prepared(), SPEC, _response(TRUNCATED_JSON, "application/json"), TRUNCATED_JSON, 3, stored
    )

    records = _evaluate_assertions(
        [
            _assertion(
                "response.status", "equals", {"expected": {"type": "number", "text": "200"}}
            ),
            _assertion(
                "response.header",
                "equals",
                {"expected": {"type": "string", "text": "application/json"}},
                selector=_CONTENT_TYPE_SELECTOR,
            ),
        ],
        "post_response",
        roots,
        AssertionContext(),
    )
    assert [record.outcome.status for record in records] == ["passed", "passed"]


def test_a_readable_body_keeps_its_sources_available_and_protected() -> None:
    """没被省略时来源照常可用；回显凭据的位置仍受保护，断言只能被拒绝。"""
    body = '{"token":"abcdef"}'
    stored = _stored_body(body, [SECRET])
    roots = _build_response_roots(
        _prepared(), SPEC, _response(body, "application/json"), body, 3, stored
    )

    assert roots.omitted_reason("response.text") is None
    assert roots.has("response.body")

    ctx = AssertionContext()
    ctx.sensitive_paths |= paths_containing(roots, [SECRET])
    records = _evaluate_assertions(
        [
            # 命中受保护坐标：拒绝求值，而不是把明文写进结果或“猜一个真假”。
            _assertion("response.text", "contains", {"expected": "abcdef"}),
            _assertion(
                "response.body",
                "equals",
                {"expected": {"type": "string", "text": "abcdef"}},
                selector=[{"kind": "key", "key": "token"}],
            ),
            # 兄弟字段不含凭据，照常求值——保护只落在真正含有它的坐标上。
            _assertion(
                "response.status", "equals", {"expected": {"type": "number", "text": "200"}}
            ),
        ],
        "post_response",
        roots,
        ctx,
    )
    assert [record.outcome.status for record in records] == ["error", "error", "passed"]
    assert all(
        record.outcome.reason_code == "policy_rejected" for record in records[:2]
    )


def test_omitted_sources_do_not_turn_into_a_silent_pass() -> None:
    """省略既不能当通过，也不能当作“来源不存在”被跳过。"""
    roots = SourceRoots()
    roots.add_omitted("response.text", OMITTED_UNPARSABLE_JSON)

    records = _evaluate_assertions(
        [_assertion("response.text", "not_contains", {"expected": "abc"})],
        "post_response",
        roots,
        AssertionContext(),
    )

    assert records[0].outcome.status == "error"
    assert records[0].outcome.reason_code == "source_omitted"
