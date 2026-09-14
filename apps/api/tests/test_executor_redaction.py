"""执行器的脱敏出口与入库正文：错误出口、转义回显与截断顺序。

两条根因都在“脱敏发生在什么时机、覆盖了哪些位置”：

- 错误出口（发送前拒绝、准入拒绝）此前只对成功路径与断言结果脱敏。注入解析之后
  的失败（请求头传输校验、变量解析、策略拒绝）会把异常文本原样写进报告与数据库，
  秘密值一旦出现在异常里就等于落库。
- 入库正文此前是“先按字节截断、再按原文串替换”。秘密在 JSON 里以转义形态出现时
  原文串根本匹配不到；秘密横跨截断边界时前半段会留在报告里。

这里只验证纯方法，不连接数据库、不发送请求。残缺正文的整段省略与服务端信封字段
名不受脱敏破坏，见 `test_body_omission.py` 与 `test_executor_selector_redaction.py`；
同一段文本里两种写法并存时的遮蔽见 `test_redaction_representations.py`。
"""
from __future__ import annotations

import json

import pytest

from app.kernel.redaction import MASK
from app.services.executor import (
    _MAX_STORED_BODY_BYTES,
    _sanitize,
    _sanitize_evidence,
    _stored_body,
)

# 同时含引号与反斜线：JSON 回显时形态与原文不同，正是“按原文替换”漏掉的那一类。
SECRET = 'ab"cd\\ef-秘密'


def _json_literal(secret: str) -> str:
    """秘密出现在 JSON 字符串里时的写法（引号与反斜线各带一层转义）。

    直接拼原文会得到一个**非法**的 JSON 文档，结构化脱敏根本走不到——那样的用例
    只能证明文本出口的行为，证明不了无损结构这条路。
    """
    return json.dumps(secret, ensure_ascii=False)[1:-1]


# —— 错误出口：任何写进报告／数据库的文本都要过同一道脱敏 ——


def test_sanitize_masks_plaintext_and_escaped_forms() -> None:
    escaped = json.dumps(SECRET, ensure_ascii=False)[1:-1]
    message = f"注入值 {SECRET} 无法传输；JSON 形态为 \"{escaped}\""
    cleaned = _sanitize(message, [SECRET])
    assert SECRET not in cleaned
    assert escaped not in cleaned
    assert MASK in cleaned


def test_sanitize_keeps_the_actionable_part_of_the_message() -> None:
    """脱敏不能把错误信息变成无用信息：位置、数量与错误码要留着。"""
    message = "请求头 X-Demo-Token 的值包含 11 个无法按 HTTP 头传输的字符"
    assert _sanitize(message, [SECRET]) == message


def test_sanitize_ignores_empty_secrets() -> None:
    """空串在 replace 里会匹配所有位置，必须跳过。"""
    assert _sanitize("正常文本", ["", SECRET]) == "正常文本"


def test_sanitize_evidence_masks_values_but_keeps_the_envelope_field_names() -> None:
    """证据信封的字段名是服务端常量：遮蔽它们会把信封改成机器读不懂的形状。"""
    evidence = {
        "method": "GET",
        "headers": [{"name": SECRET, "value": f"Bearer {SECRET}"}],
        "body": {"nested": [SECRET]},
        "status": 200,
    }
    cleaned = _sanitize_evidence(evidence, [SECRET])
    assert json.dumps(cleaned, ensure_ascii=False).count(SECRET) == 0
    assert cleaned["status"] == 200, "非文本取值不因脱敏改变"
    assert cleaned["headers"][0] == {"name": MASK, "value": f"Bearer {MASK}"}


def test_sanitize_evidence_survives_a_credential_equal_to_an_envelope_word() -> None:
    """凭据恰好等于 `body`／`name` 这类信封字段名时，信封必须原样可读。"""
    evidence = {
        "method": "GET",
        "url": "http://echo:8080/body",
        "headers": [{"name": "X-Trace", "value": "name"}],
        "body": "body",
    }
    cleaned = _sanitize_evidence(evidence, ["body", "name"])
    # 字段名一个不动，取值正常遮蔽：报告里读得出这是哪一条请求的证据。
    assert list(cleaned) == ["method", "url", "headers", "body"]
    assert list(cleaned["headers"][0]) == ["name", "value"]
    assert cleaned["headers"][0]["name"] == "X-Trace"
    assert cleaned["headers"][0]["value"] == MASK
    assert cleaned["body"] == MASK


# —— 入库正文：先脱敏，再序列化／截断 ——


def test_json_escaped_echo_cannot_be_recovered_from_the_stored_body() -> None:
    """目标按 JSON 转义回显的秘密，必须能从最终报告中“解不出来”。"""
    body = json.dumps({"echo": SECRET, "other": 1}, ensure_ascii=False)
    assert SECRET not in body, "转义形态下原文串本来就不在正文里"

    result = _stored_body(body, [SECRET])

    assert result.truncated is False
    assert result.omitted_reason is None
    assert result.format == "json"
    assert json.loads(result.text) == {"echo": MASK, "other": 1}
    assert SECRET not in result.text


def test_escaped_secret_used_as_an_object_key_is_also_removed() -> None:
    body = json.dumps({SECRET: "ok"}, ensure_ascii=False)
    result = _stored_body(body, [SECRET])
    assert json.loads(result.text) == {MASK: "ok"}


def test_stored_body_keeps_long_numbers_lossless() -> None:
    """脱敏走的是无损结构，回写不能把长整数降级成浮点。"""
    body = '{"big":9007199254740993,"token":"' + _json_literal(SECRET) + '"}'
    result = _stored_body(body, [SECRET])
    assert result.text is not None, "合法 JSON 不该走省略出口"
    assert "9007199254740993" in result.text
    assert SECRET not in result.text


def test_body_without_secrets_is_stored_verbatim() -> None:
    """没命中秘密时保持原文与原始格式，证据与线上一致。"""
    body = '{"a":1,  "b": 2}'
    assert _stored_body(body, [SECRET]).text == body


def test_body_that_is_not_json_falls_back_to_text_replacement() -> None:
    """既没被声明为 JSON、也不以 JSON 开头，才是真正的纯文本。"""
    result = _stored_body(f"前缀 {SECRET} 后缀", [SECRET])
    assert result.text == f"前缀 {MASK} 后缀"
    assert result.format == "text"
    assert result.omitted_reason is None


def _json_with_secret_at(offset: int, secret: str) -> str:
    """构造一段合法 JSON，使秘密在正文里的起始字节正好落在 offset，且超出入库上限。"""
    head = '{"pad":"'
    middle = '","k":"'
    tail = '","tail":""}'
    written = _json_literal(secret)
    pad = offset - len(head.encode()) - len(middle.encode())
    assert pad >= 0
    return f"{head}{'a' * pad}{middle}{written}{tail}"


@pytest.mark.parametrize("shift", [-1, 0, 1])
def test_secret_straddling_the_truncation_boundary_leaves_no_fragment(shift: int) -> None:
    """秘密横跨截断边界时，报告里不得留下它的任何片段。

    “先截断、再替换”在这里必然漏：截断按字节切，秘密的后半段被切掉，替换再也
    匹配不到完整秘密，前半段就原样留在了报告里。脱敏在前，切到的只是掩码。
    """
    written = _json_literal(SECRET)
    size = len(written.encode())
    offset = _MAX_STORED_BODY_BYTES - size // 2 + shift
    body = _json_with_secret_at(offset, SECRET)

    result = _stored_body(body, [SECRET])

    assert result.truncated is True
    # 报告里的正文是截断后的 JSON，解不出完整结构；因此按片段扫描：
    # 任何长度 ≥3 的秘密子串出现，都说明秘密以某种形式留了下来。
    # 原文与转义两种写法都要扫——只扫一种，另一种就能从扫不到的缝里漏出去。
    forms = {SECRET, written}
    fragments = {form[i : i + 3] for form in forms for i in range(len(form) - 2)}
    leaked = sorted(fragment for fragment in fragments if fragment in result.text)
    assert leaked == [], f"报告正文中残留秘密片段：{leaked}"
    assert MASK in result.text, "掩码应落在截断保留的那一段里"


# —— 转义形态：能否判断“这里有秘密”必须先看解码后的内容 ——
#
# 此前的判定是“秘密原文是否出现在正文文本里”，只有命中才走结构化脱敏。秘密
# `abc` 被回显成 `{"token":"\u0061bc"}` 时原文串一个字符都不在，判定直接放行，
# 结构化脱敏根本没被调用——最终报告里的文本用 json.loads 一解就还原出 `abc`。

ESCAPED_SECRET = "abc"


def test_unicode_escaped_secret_in_a_valid_json_body_is_removed() -> None:
    body = '{"token":"\\u0061bc"}'
    assert ESCAPED_SECRET not in body, "原文串确实不在正文里，这正是漏掉的原因"

    result = _stored_body(body, [ESCAPED_SECRET])

    assert json.loads(result.text) == {"token": MASK}


def test_every_escape_writing_of_the_same_secret_is_covered() -> None:
    """引号／反斜线转义与全 `\\uXXXX` 转义（含大写十六进制）都是同一份秘密。"""
    secret = 'a"b\\c中'
    plain = json.dumps(secret, ensure_ascii=False)[1:-1]  # a\"b\\c中
    ascii_escaped = json.dumps(secret)[1:-1]  # a\"b\\c\u4e2d
    upper_escaped = "a\\u0022b\\u005Cc\\u4E2D"  # 全 u 转义 + 大写十六进制
    body = '{"plain":"' + plain + '","ascii":"' + ascii_escaped + '","upper":"' + upper_escaped + '"}'

    result = _stored_body(body, [secret])

    assert json.loads(result.text) == {"plain": MASK, "ascii": MASK, "upper": MASK}


def test_surrogate_pair_escape_is_covered() -> None:
    """非 BMP 字符在 JSON 里写成两个 `\\uXXXX`，合成一个字符后才匹配得上。"""
    secret = "🔐token07"
    body = json.dumps({"t": secret})
    assert "\\ud83d\\udd10" in body, "前提：该字符确实被写成代理对"

    result = _stored_body(body, [secret])

    assert json.loads(result.text) == {"t": MASK}


def test_numeric_secret_written_as_a_json_number_is_removed() -> None:
    """纯数字的秘密落在 JSON 数字上，不会走字符串分支，只扫字符串会整条漏掉。"""
    body = '{"pin":123456,"other":123457}'

    result = _stored_body(body, ["123456"])

    assert json.loads(result.text) == {"pin": MASK, "other": 123457}
    assert "123456" not in result.text


def test_long_numbers_stay_lossless_while_a_numeric_secret_is_removed() -> None:
    body = '{"pin":123456,"big":9007199254740993}'

    result = _stored_body(body, ["123456"])

    assert "9007199254740993" in result.text, "与秘密无关的长整数不能被动到"


def test_backslashes_that_are_not_escapes_do_not_rewrite_the_text() -> None:
    """只有合法 JSON 转义才参与解码，普通文本不因为出现反斜线而被改写。"""
    for body in [r"C:\temp\abc", r"正则 \d+ 与 \u12 都不是转义", '{"path":"C:\\\\temp"}']:
        assert _stored_body(body, ["zzz"]).text == body


def test_escapes_that_do_not_decode_to_a_secret_are_preserved() -> None:
    body = json.dumps({"path": "C:\\temp"}, ensure_ascii=False)
    assert _stored_body(body, ["zzz"]).text == body
