"""脱敏：秘密派生值在写库、写日志、写报告前统一遮蔽。

不依赖“字段名像不像秘密”来猜测。凡是本次运行解析出的凭证明文、以及明确的
敏感请求头（Authorization、Cookie 等），都以固定掩码替换后再拼装证据。
即便某个值被放进普通正文或自定义头，只要它等于已解析出的秘密，也会被遮蔽。

**形态无关**：秘密在证据里未必以原文出现。JSON 正文会把引号、反斜线与非 ASCII
写成转义序列（`ab"cd` 记作 `ab\\"cd`，`abc` 记作 `\\u0061bc`），此时字面替换一个
字符也命中不了。识别与遮蔽都走“先按 JSON 转义解码、再比较、最后把命中区间映射回
原文”这一条路径：按形态逐个穷举（原文、转义、大小写、宽度……）总会有下一个漏网
的写法，解码后比较则与写法无关。这个函数同时是断言敏感性判定的判定依据——只有一
处认识转义、另一处只认原文，就会留下“报告里遮了、但断言真假仍能猜出秘密”的通道。

原文命中与解码命中必须**合并**后一次性遮蔽：同一段文本里两种形态可以并存，命中
一种就返回会漏掉另一种，留下的还是完整凭据的可读副本。
"""
from __future__ import annotations

import re
from typing import Any

from .lossless_json import NumberNode

MASK = "***"

# 这些头的值一律不进入证据，与是否已解析出对应秘密无关。
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
}

# JSON 字符串里合法的转义序列（RFC 8259 §7）。只认这些：`C:\temp` 这样的普通文本
# 不会因为出现一个反斜线就被当成转义改写。
_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _decode_escapes(text: str) -> tuple[str, list[tuple[int, int]]]:
    """按 JSON 转义规则解码，并记下每个解码字符对应的原文区间。

    区间是“映射回原文”的依据：命中的是解码后的内容，要遮蔽的却是原文里那段
    `\\u0061bc`。代理对（非 BMP 字符写作两个 `\\uXXXX`）合成一个字符，两段区间
    合成一段——否则一个 emoji 秘密会永远匹配不上。
    """
    decoded: list[str] = []
    origins: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        match = _ESCAPE.match(text, index) if text[index] == "\\" else None
        if match is None:
            decoded.append(text[index])
            origins.append((index, index + 1))
            index += 1
            continue
        body = match.group()[1:]
        if body.startswith("u"):
            code = int(body[1:], 16)
            if 0xD800 <= code <= 0xDBFF:
                nxt = _ESCAPE.match(text, match.end())
                if nxt is not None and nxt.group()[1:2] == "u":
                    low = int(nxt.group()[2:], 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        combined = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        decoded.append(chr(combined))
                        origins.append((index, nxt.end()))
                        index = nxt.end()
                        continue
            decoded.append(chr(code))
        else:
            decoded.append(_SIMPLE_ESCAPES[body])
        origins.append((index, match.end()))
        index = match.end()
    return "".join(decoded), origins


def _masked_spans(text: str, secrets: list[str]) -> list[tuple[int, int]]:
    """在“解码后”的内容里定位秘密，把命中区间映射回原文坐标。

    没有反斜线就没有转义层，直接返回空并交给原文命中——这条短路既省掉无谓的解码，
    也保证普通文本（不含反斜线）的遮蔽行为与从前逐字节一致。这里只负责“转义那一
    层”的命中，调用方必须把它与原文命中合并后再替换。
    """
    if "\\" not in text:
        return []
    needles = [secret for secret in secrets if secret]
    if not needles:
        return []
    decoded, origins = _decode_escapes(text)
    if decoded == text:
        # 这段文本里没有可解码的转义序列，解码结果与原文逐字符相同，
        # 原文替换已经覆盖全部命中，不必再走一遍区间映射。
        return []
    spans: list[tuple[int, int]] = []
    for needle in needles:
        start = decoded.find(needle)
        while start != -1:
            spans.append((origins[start][0], origins[start + len(needle) - 1][1]))
            start = decoded.find(needle, start + 1)
    return spans


def _apply_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """按原文区间替换掩码；区间先合并再从后往前替换，偏移不会互相影响。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = text
    for start, end in reversed(merged):
        result = f"{result[:start]}{MASK}{result[end:]}"
    return result


def _literal_spans(text: str, needles: list[str]) -> list[tuple[int, int]]:
    """原文里每一处命中的区间；调用方必须先滤掉空串（空串匹配所有位置）。"""
    spans: list[tuple[int, int]] = []
    for needle in needles:
        start = text.find(needle)
        while start != -1:
            spans.append((start, start + len(needle)))
            start = text.find(needle, start + 1)
    return spans


def _replace_secrets(text: str, secrets: list[str]) -> str:
    """原文命中与转义命中**合并后一次性**遮蔽。

    同一段文本里两种形态可以同时出现：`C:\\temp | C:\\\\temp` 里第一段是原样回显，
    第二段才是 JSON 转义写法。只按解码结果遮蔽会漏掉第一段，只按原文替换又会漏掉
    第二段——留在这两处的是**完整凭据的可读副本**，报告解一次就还原出来了。因此
    两种命中合成同一批区间再替换，而不是“命中一种就返回”。
    """
    needles = [secret for secret in secrets if secret]
    if not needles:
        return text
    spans = _literal_spans(text, needles) + _masked_spans(text, needles)
    if not spans:
        return text
    return _apply_spans(text, spans)


def contains_secret(text: str, secrets: list[str]) -> bool:
    """文本是否**以任何形态**还原得出秘密（原文或 JSON 转义）。

    断言取值来源的敏感性标记与证据写出共用这一个判断：不同源就会出现“报告里遮蔽
    过了，但断言的真假仍能逐位猜出秘密”的通道——遮蔽与判定必须看到同一批命中。
    """
    for secret in secrets:
        if secret and secret in text:
            return True
    return bool(_masked_spans(text, secrets))


def value_contains_secret(value: Any, secrets: list[str]) -> bool:
    """结构里是否存在还原后等于秘密的文本；对象键与数字词法一视同仁。

    数字按原始词法比较：纯数字的秘密被写成 JSON 数字时不会经过字符串分支，
    只扫字符串会把它整条漏掉。
    """
    if isinstance(value, str):
        return contains_secret(value, secrets)
    if isinstance(value, NumberNode):
        return contains_secret(value.text, secrets)
    if isinstance(value, dict):
        return any(
            contains_secret(str(key), secrets) or value_contains_secret(val, secrets)
            for key, val in value.items()
        )
    if isinstance(value, list):
        return any(value_contains_secret(item, secrets) for item in value)
    return False


def redact_text(value: str, secrets: list[str]) -> str:
    return _replace_secrets(value, secrets)


def redact_pairs(
    pairs: list[tuple[str, str]] | list[dict[str, str]], secrets: list[str]
) -> list[dict[str, str]]:
    """把请求头／查询参数转为可入库形式，敏感头与秘密值均被遮蔽。

    名称同样要脱敏：秘密并不只出现在值里，目标完全可能把凭据回显成**字段名**
    （`{"<凭据>": "ok"}`）。只遮值不遮名，等于把同一份秘密从值的通道挪到了名的
    通道，报告里仍然读得到。

    返回的是**未编码**的项：这里只负责“哪些是秘密”，编码交给发送与证据共用的
    那一个 URL 入口，先遮蔽再编码，秘密就不会以百分号编码的形态躲过替换。
    """
    output: list[dict[str, str]] = []
    for item in pairs:
        if isinstance(item, dict):
            name, value = item.get("name", ""), item.get("value", "")
        else:
            name, value = item[0], item[1]
        masked = MASK if name.lower() in _SENSITIVE_HEADERS else _replace_secrets(value, secrets)
        output.append({"name": _replace_secrets(name, secrets), "value": masked})
    return output


def redact_value(value: Any, secrets: list[str]) -> Any:
    """递归遮蔽结构化值中的秘密文本；不含秘密的数字节点按原词法保留。

    键与值一视同仁：秘密可能整个作为对象键出现，只处理值会让它在报告与数据库里
    原样留存。遮蔽键名不改变结构（键只是被换成掩码），不影响长度之外的任何语义。
    数字的精度必须留住（9007199254740993 不能在往返中变形），所以只有**确实等于
    秘密**的数字词法才被遮蔽——那已经不是精度问题，而是明文本身。
    """
    if isinstance(value, str):
        return _replace_secrets(value, secrets)
    if isinstance(value, NumberNode):
        return MASK if contains_secret(value.text, secrets) else value
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _replace_secrets(str(key), secrets): redact_value(val, secrets)
            for key, val in value.items()
        }
    return value


__all__ = [
    "MASK",
    "contains_secret",
    "redact_pairs",
    "redact_text",
    "redact_value",
    "value_contains_secret",
]
