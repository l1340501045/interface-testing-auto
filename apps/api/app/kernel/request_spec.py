"""用例请求定义：结构校验与实际请求构造。

请求只保存方法、路径、查询参数、请求头和正文；**目标来源由环境决定**，
用例本身不携带绝对地址。这样同一个用例可以安全地投向不同环境，也不会
因为导入了一条 cURL 就绕过环境与执行池的目标白名单。

正文按原始文本保存与发送，JSON 数字不经过 JavaScript Number 或二进制浮点，
避免 9007199254740993 这类长整数在往返中变形。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit

from .lossless_json import LosslessJSONError, dumps, loads
from .variables import (
    VariableResolutionError,
    VariableResolver,
    has_variable_reference,
    variable_reference_count,
)

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_BODY_TYPES = {"none", "json", "text", "form"}
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_V2_ROWS = 500
_MAX_DESCRIPTION_CHARS = 1024

# 路径段里可以原样保留的字符：RFC 3986 的 pchar 加上路径分隔符。
_PATH_SAFE = "/%:@!$&*+,;=-._~"
# 查询项编码时额外保留 `*`：它是 RFC 3986 允许在查询里原样出现的子分隔符，而脱敏
# 掩码 `***` 只在证据 URL 里出现——保住它，读报告的人一眼就能看出这里被遮蔽过，
# 不必去解一次百分号编码。线上与证据共用这一个编码入口，不存在第二套编码规则。
_QUERY_SAFE = "*"

# RFC 9110 的 field-name 就是 token：不含分隔符与控制字符。
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class RequestSpecError(ValueError):
    """请求定义非法，属于配置错误，发送前即拒绝。"""


@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    content: bytes | None = None
    body_type: str = "none"
    raw_body: str = ""
    # 真正写进 URL 的查询参数（变量已解析、认证注入已并入），保持顺序与重复次数。
    query: list[tuple[str, str]] = field(default_factory=list)
    # 环境给出的 origin 前缀（已在构造时去掉尾斜杠），供按同一条编码路径重建 URL。
    base: str = ""
    # **用例自己的**路径：变量解析后的原值（未编码），不含环境基础路径。证据 URL 靠
    # 它和环境 `base` 重建，别处不得再自行拼接——两处拼接迟早会漂移。
    relative_path: str = ""
    # **完整最终路径**：环境基础路径 + 用例相对路径，变量已解析、不含查询串。
    # 断言来源 `request.path` 取的就是它。环境写成 `http://echo:8080/api/v1` 时线上
    # 请求的是 `/api/v1/echo`，断言若还核对 `/echo`，等于对着一个从未发出过的地址
    # 判真假：发对了反而失败，发错了反而通过。
    final_path: str = ""
    # `final_path` 在 URL 里的编码形态（不含查询串），与 `urlsplit(url).path`
    # 逐字符一致——环境带基础路径时也一致。
    url_path: str = ""
    # v2 用户行在最终实际集合中的稳定坐标。认证注入与自动 Content-Type 没有 row_id。
    query_row_indices: dict[str, int] = field(default_factory=dict)
    header_row_indices: dict[str, int] = field(default_factory=dict)

    def headers_as_pairs(self) -> list[dict[str, str]]:
        """保持重复请求头的顺序，供字段定位与证据脱敏使用。"""
        return [{"name": name, "value": value} for name, value in self.headers]

    def query_as_pairs(self) -> list[dict[str, str]]:
        """保持重复查询参数的顺序，供字段定位与证据脱敏使用。

        断言取值必须与线上同源：这里的每一项都是 URL 里实际发出去的那一条，
        而不是请求定义里未经变量解析、也未必包含认证注入的原始模板。
        """
        return [{"name": name, "value": value} for name, value in self.query]

    def body_text(self) -> str:
        """正文原文（变量解析后）；二进制内容不在此接口内出现。"""
        return self.raw_body

    def rebuild_url(self, path: str, query: list[tuple[str, str]]) -> str:
        """用给定的路径与查询项重建 URL，编码规则与发送完全一致。

        证据需要一份“先把秘密换掉、再编码”的 URL：直接对已经编码好的 `url` 做
        字符串替换挡不住必须编码的秘密（`a+b/c=` 上网后是 `a%2Bb%2Fc%3D`，原文串
        在 URL 里根本不存在）。秘密的编码形态由本地编码器决定，不靠猜——所以这里
        复用发送时的那一个编码入口，入参是**未编码**的路径与查询项。

        路径入参是**用例相对路径**（`relative_path`，脱敏后传入）：`base` 由本方法
        自己拼在前面。若传入 `final_path`（已含环境基础路径），基础路径会被拼两次。
        """
        return join_url(self.base, path, query)


def encode_query(pairs: list[tuple[str, str]]) -> str:
    """查询串编码：顺序与重复次数原样保留，不折叠成 dict。"""
    return urlencode(pairs, doseq=False, quote_via=quote_plus, safe=_QUERY_SAFE)


def join_url(base: str, path: str, query: list[tuple[str, str]]) -> str:
    """把 origin、路径与查询项拼成最终 URL；**发送与证据共用这一个入口**。

    两条出口各写一份拼接逻辑迟早会漂移：改动只落在其中一条，报告里的 URL 就不再
    等于真正发出去的那个。这里把“怎么编码”收成唯一实现，调用方只决定传入的路径与
    查询项是原文还是已脱敏的版本。
    """
    url = f"{base.rstrip('/')}{quote(path, safe=_PATH_SAFE)}"
    if query:
        url = f"{url}?{encode_query(query)}"
    return url


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RequestSpecError(f"{field_name} 必须是文本")
    return value


def _pairs(items: Any, field_name: str) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise RequestSpecError(f"{field_name} 必须是数组")
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RequestSpecError(f"{field_name} 的每一项必须是对象")
        extra = set(item) - {"name", "value"}
        if extra:
            raise RequestSpecError(
                f"无版本标记的 {field_name} 行包含 v1 不支持的字段："
                f"{'、'.join(sorted(extra))}；行元数据需要 schema_version: 2"
            )
        name = item.get("name")
        value = item.get("value", "")
        if not isinstance(name, str) or not name:
            raise RequestSpecError(f"{field_name} 的项缺少名称")
        if not isinstance(value, str):
            raise RequestSpecError(f"{field_name} 中 {name} 的值必须是文本")
        result.append({"name": name, "value": value})
    return result


def _v2_pairs(
    items: Any, field_name: str, seen_row_ids: set[str]
) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise RequestSpecError(f"{field_name} 必须是数组")
    result: list[dict[str, Any]] = []
    required = {"row_id", "name", "value", "enabled", "description"}
    for item in items:
        if not isinstance(item, dict):
            raise RequestSpecError(f"{field_name} 的每一项必须是对象")
        missing = required - set(item)
        extra = set(item) - required
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append(f"缺少 {'、'.join(sorted(missing))}")
            if extra:
                details.append(f"包含未知字段 {'、'.join(sorted(extra))}")
            raise RequestSpecError(f"{field_name} 的 v2 行字段不完整：{'；'.join(details)}")
        raw_row_id = item["row_id"]
        if not isinstance(raw_row_id, str):
            raise RequestSpecError(f"{field_name} 的 row_id 必须是 UUID 字符串")
        try:
            row_id = str(uuid.UUID(raw_row_id))
        except ValueError as error:
            raise RequestSpecError(f"{field_name} 的 row_id 不是有效 UUID") from error
        if raw_row_id.lower() != row_id:
            raise RequestSpecError(f"{field_name} 的 row_id 必须使用标准 UUID 格式")
        if row_id in seen_row_ids:
            raise RequestSpecError(f"请求内 row_id 重复：{row_id}")
        seen_row_ids.add(row_id)
        name = item["name"]
        value = item["value"]
        enabled = item["enabled"]
        description = item["description"]
        if not isinstance(name, str) or not name:
            raise RequestSpecError(f"{field_name} 的项缺少名称")
        if field_name == "headers" and _HEADER_NAME.fullmatch(name) is None:
            raise RequestSpecError("v2 请求头名称不是合法的 HTTP token")
        if not isinstance(value, str):
            raise RequestSpecError(f"{field_name} 中的值必须是文本")
        if not isinstance(enabled, bool):
            raise RequestSpecError(f"{field_name} 的 enabled 必须是布尔值")
        if not isinstance(description, str):
            raise RequestSpecError(f"{field_name} 的 description 必须是文本")
        if len(description) > _MAX_DESCRIPTION_CHARS:
            raise RequestSpecError(
                f"{field_name} 的 description 超过 {_MAX_DESCRIPTION_CHARS} 字符上限"
            )
        result.append(
            {
                "row_id": row_id,
                "name": name,
                "value": value,
                "enabled": enabled,
                "description": description,
            }
        )
    return result


def _json_variable_hint(body: str) -> str:
    """JSON 正文里的变量必须写在引号内，失败时给出可直接照抄的写法。

    绑定发生在解析**之后**，所以占位本身必须先是一段合法的 JSON 文本；不加引号的
    占位（`{"score":{{shared}}}`）在保存时就会被拒。这里不是放宽校验，而是把
    “数字变量该怎么写”说清楚：写在引号里，渲染后得到的仍然是数字。
    """
    if not has_variable_reference(body):
        return ""
    return (
        "。正文里的 `{{变量}}` 需要写在 JSON 字符串的引号内，例如 "
        '{"score":"{{shared}}"}；变量是数字时渲染后仍是数字，不必去掉引号。'
    )


def validate_request(spec: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化请求定义；不接受绝对地址或未知字段。"""
    if not isinstance(spec, dict):
        raise RequestSpecError("请求定义必须是对象")
    allowed = {
        "method",
        "path",
        "query_params",
        "headers",
        "body_type",
        "body",
        "imported_origin",
        "auth_required",
        "schema_version",
    }

    extra = set(spec) - allowed
    if extra:
        raise RequestSpecError(f"请求定义包含未知字段：{'、'.join(sorted(extra))}")

    schema_version = spec.get("schema_version")
    if "schema_version" in spec and (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 2
    ):
        raise RequestSpecError(f"不支持的请求协议版本：{schema_version!r}")

    method = _text(spec.get("method", "GET"), "method").upper()
    if method not in _METHODS:
        raise RequestSpecError(f"不支持的请求方法：{method}")

    path = _text(spec.get("path", "/"), "path")
    if "://" in path:
        raise RequestSpecError("请求路径不能是绝对地址，目标由环境决定")
    if not path.startswith("/"):
        path = "/" + path

    body_type = _text(spec.get("body_type", "none"), "body_type")
    if body_type not in _BODY_TYPES:
        raise RequestSpecError(f"不支持的正文类型：{body_type}")

    body = spec.get("body", "")
    if body_type == "none":
        body = ""
    else:
        body = _text(body, "body")
        if body_type == "json" and body.strip():
            try:
                loads(body)
            except LosslessJSONError as error:
                raise RequestSpecError(
                    f"JSON 正文无法解析：{error}{_json_variable_hint(body)}"
                ) from error
        if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
            raise RequestSpecError("正文超过 5 MiB 上限")

    if schema_version == 2:
        seen_row_ids: set[str] = set()
        query_params = _v2_pairs(spec.get("query_params"), "query_params", seen_row_ids)
        headers = _v2_pairs(spec.get("headers"), "headers", seen_row_ids)
        if len(query_params) + len(headers) > _MAX_V2_ROWS:
            raise RequestSpecError(f"v2 Query 与 Header 合计最多 {_MAX_V2_ROWS} 行")
    else:
        query_params = _pairs(spec.get("query_params"), "query_params")
        headers = _pairs(spec.get("headers"), "headers")

    normalized: dict[str, Any] = {
        "method": method,
        "path": path,
        "query_params": query_params,
        "headers": headers,
        "body_type": body_type,
        "body": body,
    }
    if schema_version == 2:
        normalized["schema_version"] = 2
    imported = spec.get("imported_origin")
    if imported is not None:
        normalized["imported_origin"] = _text(imported, "imported_origin")
    if _auth_required(spec) is True:
        normalized["auth_required"] = True
    return normalized


def _auth_required(spec: dict[str, Any]) -> bool:
    """请求是否必须使用当前环境的登录态；**只接受真正的布尔值**。

    缺省与 false 都表示沿用既有的“跟随环境”行为：环境配置了可用身份就注入，没有
    就按公开请求发送。只有显式 true 才把“必须带身份”变成硬约束。

    不能按 truthy 收下非布尔值：`"false"`、`"0"`、`0` 都是非空／非零的真值，把它们
    当成 true 会让一份本该匿名可用的请求在没有任何可用身份时被拒绝，而用户写下的
    内容明明是“不需要认证”。类型不符是配置错误，直接报出来。
    """
    value = spec.get("auth_required")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestSpecError(
            "auth_required 只能是布尔值 true 或 false；不接受文本或数字，"
            "以免把 \"false\" 这类真值当成需要认证。"
        )
    return value


def _resolve_pairs(
    pairs: list[dict[str, Any]], resolver: VariableResolver
) -> list[dict[str, Any]]:
    return [
        {
            "name": pair["name"],
            "value": resolver.resolve_text(pair["value"]),
            **({"row_id": pair["row_id"]} if "row_id" in pair else {}),
        }
        for pair in pairs
        if pair.get("enabled", True)
    ]


# —— 正文绑定 ——
#
# 正文按自身协议**逐字段**绑定，而不是整段文本替换：整段替换会把变量取值里的协议
# 字符当成结构本身。`{"score":"{{shared}}"}` 里的数字 1 曾变成字符串 "1"（数值断言
# 随后判类型不符），表单里的 `q={{value}}` 曾把取值中的 `&`、`=` 拆成两个字段——
# 两者都不是用户写错了模板，而是绑定落在了错误的位置。
#
# **不含变量引用的正文一律原文直发**：不解析、不重排、不改一个字节。用户调好的
# JSON 排版、键顺序与重复写法原样上线；这也保证“加变量之前一直正常”的用例不会
# 因为这次改动而变样。只有正文确实含变量时才走协议路径，并且那时正文会按协议重排。


def _bind_body(body_type: str, template: str, resolver: VariableResolver) -> str:
    """按正文类型绑定变量；每种类型保持自己协议的字段语义。"""
    if body_type == "none":
        return ""
    if body_type == "json":
        return _bind_json_body(template, resolver)
    if body_type == "form":
        return _bind_form_body(template, resolver)
    # 纯文本内部没有结构可依赖，沿用既有的整段文本替换语义。
    return resolver.resolve_text(template)


def _bind_json_body(body: str, resolver: VariableResolver) -> str:
    """JSON 正文：无损解析 → 逐字段绑定 → 无损序列化。

    解析与序列化都走无损工具，数字全程以十进制原文流转，不经过 JavaScript
    Number 或二进制浮点，`9007199254740993` 不会在往返中变成相邻整数。
    """
    if not has_variable_reference(body):
        return body
    try:
        parsed = loads(body)
    except LosslessJSONError as error:
        raise RequestSpecError(f"JSON 正文无法解析：{error}") from error
    return dumps(_bind_json_node(parsed, resolver, "$"))


def _bind_json_node(value: Any, resolver: VariableResolver, path: str) -> Any:
    """递归绑定一个 JSON 节点。

    `path` 只由正文自己写的键与下标组成，**不含变量取值**：渲染后的键会被变量
    改写，而错误信息要能脱开变量内容独立阅读。
    """
    if isinstance(value, str):
        # 字符串位置同时覆盖两种情况：整体恰为一个变量时保留其类型，嵌在更长
        # 文本里时仍是字符串，并由序列化按 JSON 规则转义引号与反斜线。
        return resolver.resolve_structured(value)
    if isinstance(value, list):
        return [
            _bind_json_node(item, resolver, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        bound: dict[str, Any] = {}
        # 渲染后的键 -> 正文里写的原键，报错只引用原键。
        sources: dict[str, str] = {}
        for key, item in value.items():
            rendered = resolver.resolve_text(key)
            if rendered in bound:
                raise RequestSpecError(
                    f"JSON 正文的对象键在变量渲染后重名：{sources[rendered]!r} 与 "
                    f"{key!r} 渲染成了同一个键（位置 {path}）。两个键会写进同一个"
                    "字段，后一个会静默盖掉前一个；请改用不同的键，或让变量取值"
                    "不再相同。"
                )
            sources[rendered] = key
            bound[rendered] = _bind_json_node(item, resolver, f"{path}.{key}")
        return bound
    # 数字节点、布尔与 null 不来自变量：原样保留，类型因此不会在往返中改变。
    return value


def _bind_form_body(body: str, resolver: VariableResolver) -> str:
    """表单正文：按表单协议解析成有序、可重复的字段，逐字段绑定后写回。

    取值里的 `&`、`=`、`+`、空格与中文在写回时按协议编码，因此不会改变字段边界，
    也不会把一项拆成两项；重复字段保持原来的顺序与出现次数。
    """
    if not has_variable_reference(body):
        return body
    fields = parse_qsl(body, keep_blank_values=True)
    _reject_altered_form_references(body, fields)
    bound = [
        (resolver.resolve_text(name), resolver.resolve_text(value))
        for name, value in fields
    ]
    # 与查询参数共用同一条编码入口：表单与查询串的转义规则必须完全一致。
    return encode_query(bound)


def _reject_altered_form_references(body: str, fields: list[tuple[str, str]]) -> None:
    """变量引用被表单解码改写时显式失败，不按字面量静默发出去。

    `+` 解码成空格、`%XX` 解码成字节之后，`{{...}}` 可能已不再是变量引用
    （`{{a+b}}` → `{{a b}}`，模式不再匹配）。这时逐字段替换会“什么也没替换”地
    把模板原文当成取值发给目标，用户看到的是一份自己没写过的正文。
    """
    decoded = "".join(name + value for name, value in fields)
    if variable_reference_count(decoded) < variable_reference_count(body):
        raise RequestSpecError(
            "表单正文里的变量引用含表单编码会改变含义的字符（`+` 会解码成空格、"
            "`%` 会解码成字节），按字段替换匹配不到它。请改用不含这两个字符的变量名，"
            "或把这条引用放到其它正文类型里。"
        )


def _validate_wire_headers(pairs: list[tuple[str, str]]) -> None:
    """拒绝无法按 HTTP 线上格式表示的名称或值。

    这是“可发送性”的最后一道关卡，覆盖用户输入、环境变量、已导入的请求头以及
    认证注入值。不在发送前拦住会有两类后果，且都不是配置错误：非 ASCII 值会让
    httpx 抛 `UnicodeEncodeError`，它不是 `httpx.HTTPError`，会穿过执行器的网络
    错误处理并让 worker 在执行中途崩溃；CR／LF 与 NUL 由 httpx 原样并入请求头，
    等到 h11 组包才报错，被归类为“网络中断、副作用不明”——用户只是写错一个
    字符，却得到“结果不明，需人工确认”，且该次运行不会再有自动结论。

    值的允许范围按传输实现的实际契约（ASCII）而不是 RFC 的理论上限
    （obs-text 0x80–0xFF）来定：后者在 httpx 这里同样会抛 UnicodeEncodeError，
    放行它等于把同一个崩溃留在原地。正文不受此限制：JSON 文本按 UTF-8 携带
    中文是正常用法。

    **异常不得回显值本身**：这些校验同样作用于认证注入值，而这里的错误会被
    `_fail_without_send` 原样写进运行报告与数据库。带着明文值的报错等于把秘密
    从“请求里”搬到了“报告里”——一条专门用来保护秘密的通道反而成了泄露口。
    因此只报名称、非法字符个数与首个非法码位：足够定位与修复，不含值内容。
    """
    for name, value in pairs:
        if not _HEADER_NAME.match(name):
            raise RequestSpecError(
                f"请求头名称不是合法的字段名，只能使用字母、数字和 !#$%&'*+-.^_`|~；"
                f"实际名称长度 {len(name)} 个字符（名称不回显，请对照配置检查）。"
            )
        illegal = [char for char in value if char != "\t" and not (" " <= char <= "~")]
        if illegal:
            raise RequestSpecError(
                f"请求头 {name} 的值包含 {len(illegal)} 个无法按 HTTP 头传输的字符"
                f"（首个是码位 U+{ord(illegal[0]):04X}）；值本身不回显，以免把认证值"
                "写进错误信息与运行报告。中文等非 ASCII 内容请放在正文或查询参数中。"
            )


def prepare(
    spec: dict[str, Any],
    base_url: str,
    resolver: VariableResolver,
    injected_headers: list[tuple[str, str]] | None = None,
    injected_query: list[tuple[str, str]] | None = None,
) -> PreparedRequest:
    """把已校验的请求定义解析为可发送请求；变量未定义在此处失败。

    认证注入和用户配置的请求头／查询参数走**同一条组装路径**：注入值先并进来、
    再一起编码上线，因此断言取值、证据脱敏与真实请求看到的是同一份内容。若让注入
    只改变取值来源而不发送，断言核对的会是一个根本没发出去的虚构值。
    """
    method = spec["method"]
    path = resolver.resolve_text(spec["path"])
    if "://" in path:
        raise RequestSpecError("变量解析后路径成为绝对地址，已阻止发送")

    query = _resolve_pairs(spec["query_params"], resolver)
    headers = _resolve_pairs(spec["headers"], resolver)

    if injected_query:
        existing = {item["name"] for item in query}
        for name, value in injected_query:
            if name in existing:
                raise RequestSpecError(
                    f"请求已包含 {name} 查询参数，认证注入与已配置的查询参数冲突，请先移除该项。"
                )
            query.append({"name": name, "value": value})

    body_type = spec["body_type"]
    body = _bind_body(body_type, spec["body"], resolver)

    base = base_url.rstrip("/")
    query_pairs = [(item["name"], item["value"]) for item in query]
    query_row_indices = {
        item["row_id"]: index for index, item in enumerate(query) if "row_id" in item
    }
    # 重复参数必须保留原始顺序与出现次数，不能折叠成 dict。
    url = join_url(base, path, query_pairs)
    # 完整最终路径与最终 URL 共用**同一份**数据、同一条拼接规则：环境基础路径取
    # `base` 自己的路径段，再加上用例相对路径。断言与证据因此不会各拼一遍——两个
    # 出口各拼一次，改了一处漏了另一处，报告里的地址就不再等于线上发出去的那个。
    base_path = urlsplit(base).path

    final_headers = [(item["name"], item["value"]) for item in headers]
    header_row_indices = {
        item["row_id"]: index for index, item in enumerate(headers) if "row_id" in item
    }
    content: bytes | None = None
    if body_type == "json":
        content = body.encode("utf-8")
        if not any(name.lower() == "content-type" for name, _ in final_headers):
            final_headers.append(("Content-Type", "application/json"))
    elif body_type == "form":
        content = body.encode("utf-8")
        if not any(name.lower() == "content-type" for name, _ in final_headers):
            final_headers.append(("Content-Type", "application/x-www-form-urlencoded"))
    elif body_type == "text":
        content = body.encode("utf-8")

    if injected_headers:
        existing = {name.lower() for name, _ in final_headers}
        for name, value in injected_headers:
            if name.lower() in existing:
                raise RequestSpecError(
                    f"请求已包含 {name} 头，认证注入与已配置的请求头冲突，请先移除该头。"
                )
            final_headers.append((name, value))

    _validate_wire_headers(final_headers)

    return PreparedRequest(
        method=method,
        url=url,
        headers=final_headers,
        content=content,
        body_type=body_type,
        raw_body=body,
        query=query_pairs,
        base=base,
        relative_path=path,
        final_path=f"{base_path}{path}",
        url_path=f"{base_path}{quote(path, safe=_PATH_SAFE)}",
        query_row_indices=query_row_indices,
        header_row_indices=header_row_indices,
    )


__all__ = [
    "PreparedRequest",
    "RequestSpecError",
    "VariableResolutionError",
    "encode_query",
    "join_url",
    "prepare",
    "validate_request",
]
