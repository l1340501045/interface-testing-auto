"""同一段文本里两种写法并存时的遮蔽：原文命中与转义命中必须合并。

合成凭据是字面量 `C:\\temp`（七个字符）。目标把同一处回显成
`C:\\temp | C:\\\\temp`：第一段就是凭据本身，第二段才是它的 JSON 转义写法
（`\\\\t` 解出反斜线 + `t`，才等于凭据）。此前的实现一旦在**解码后**找到命中就直接
返回，第一段那七个字符原样留在报告里——读报告的人不需要解码就拿到了凭据。

两处命中必须合成同一批区间、替换一次；转义层自身的映射（代理对合成成一段区间、
命中区间并回原文坐标）不能因为合并而退化。这里只验证纯方法，不连接数据库、不发送
请求。
"""
from __future__ import annotations

import json

from app.kernel.lossless_json import dumps, loads
from app.kernel.redaction import (
    MASK,
    _decode_escapes,
    _masked_spans,
    contains_secret,
    redact_text,
    redact_value,
)
from app.services.executor import OMITTED_INCOMPLETE_REDACTION, _stored_body

SECRET = "C:\\temp"
# 第一段是凭据原文，第二段是它的转义写法：两种命中落在同一段文本的不同位置。
MIXED = "C:\\temp | C:\\\\temp"
# 同一份内容的 JSON 正文形态：目标把这段文本放进 `echo` 字段。
MIXED_JSON_BODY = '{"echo":"C:\\temp | C:\\\\temp"}'


def _recoverable(text: str, secret: str) -> bool:
    """文本里能否还原出凭据：原文可读，或按 JSON 转义解码后可读。"""
    if secret in text:
        return True
    decoded, _ = _decode_escapes(text)
    return secret in decoded


# —— 前提：两种写法确实同时存在 ——


def test_the_fixture_really_contains_both_hits_in_different_places() -> None:
    """两种命中同时存在且互不重叠，否则这个用例证明不了“合并”。"""
    assert SECRET in MIXED, "原文命中的前提"
    literal = (MIXED.index(SECRET), MIXED.index(SECRET) + len(SECRET))

    spans = _masked_spans(MIXED, [SECRET])

    assert spans, "转义命中的前提"
    assert all(start >= literal[1] for start, _ in spans), "两处命中不得落在同一段上"


# —— 遮蔽：两处都要遮，且只遮一次 ——


def test_both_writings_are_masked_in_a_single_pass() -> None:
    cleaned = redact_text(MIXED, [SECRET])

    assert SECRET not in cleaned
    assert cleaned.count(MASK) == 2, "两处各遮一次：只遮转义那处正是原来的漏法"
    assert not _recoverable(cleaned, SECRET)


def test_the_judgement_agrees_with_the_masking() -> None:
    """判定与遮蔽同一口径：遮完之后这段文本不再算“含凭据”。"""
    assert contains_secret(MIXED, [SECRET]) is True
    assert contains_secret(redact_text(MIXED, [SECRET]), [SECRET]) is False


def test_surrogate_pair_hits_still_map_back_after_the_merge() -> None:
    """合并不得破坏代理对映射：非 BMP 字符写成两个 `\\uXXXX`，命中仍要还原成原文。"""
    secret = "🔐token07"
    escaped = json.dumps(secret)[1:-1]
    assert "\\ud83d\\udd10" in escaped, "前提：该字符确实被写成代理对"

    cleaned = redact_text(f"{secret} | {escaped}", [secret])

    assert not _recoverable(cleaned, secret)
    assert cleaned.count(MASK) == 2


def test_adjacent_hits_do_not_produce_overlapping_masks() -> None:
    """两处命中紧挨着时合并成一段掩码，不会互相覆盖成错位的残字。"""
    cleaned = redact_text(f"{SECRET}{SECRET}", [SECRET])
    assert cleaned == MASK


# —— 入库：纯文本出口与结构化出口都要过同一道自查 ——


def test_a_plain_text_body_keeps_no_readable_credential() -> None:
    """真正的纯文本走文本出口：两种写法都在同一遍里遮掉。"""
    result = _stored_body(f"前缀 {MIXED} 后缀", [SECRET])

    assert result.format == "text"
    assert result.omitted_reason is None
    assert result.text.count(MASK) == 2
    assert not _recoverable(result.text, SECRET)


def test_a_json_body_whose_rewrite_rebuilds_the_writing_is_omitted() -> None:
    """回写是一次**重新转义**：验不过的正文整段省略，不放半份证据出去。"""
    result = _stored_body(MIXED_JSON_BODY, [SECRET])

    assert result.text is None
    assert result.omitted_reason == OMITTED_INCOMPLETE_REDACTION


def test_the_omission_is_about_the_rewrite_not_a_missed_mask() -> None:
    """定位：结构化脱敏本身是有效的，残余来自“制表符又被转义回 `\\t`”。"""
    rewritten = dumps(redact_value(loads(MIXED_JSON_BODY), [SECRET]))

    # 字段值里已经没有凭据：命中的是转义写法那一处，遮的就是它。
    assert not _recoverable(loads(rewritten)["echo"], SECRET)
    # 但回写把制表符转义回 `\t`，文本层重新拼出了凭据的字符序列。
    assert SECRET in rewritten


def test_a_body_whose_writings_are_both_masked_is_still_stored() -> None:
    """省略只落在“验不过”的正文上：正常回显（只在值里出现）照常入库。"""
    body = json.dumps({"echo": SECRET}, ensure_ascii=False)

    result = _stored_body(body, [SECRET])

    assert result.text is not None
    assert result.omitted_reason is None
    assert loads(result.text) == {"echo": MASK}
