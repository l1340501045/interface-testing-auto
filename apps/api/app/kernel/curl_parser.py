"""cURL 导入解析：把 curl 命令文本安全解析为规范化请求草稿。

只做词法解析，不执行 Shell、不发起请求、不读文件。解析遵循“不改变用户请求语义”
与“不确定即拒绝”两条边界：显式方法不被 -d 覆盖；引用文件、multipart、未知带参
选项、多个 URL 一律标记为不可发送，而不是静默生成等价性错误的草稿。

认证信息（Authorization 头、Cookie、URL userinfo、-u）不写入普通草稿，只产出
待配置的认证提示，避免明文凭证落进用例正文。
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# 完全不影响请求定义的选项：安全忽略。
_NO_ARG_FLAGS = {
    "-s", "--silent", "-S", "--show-error", "-v", "--verbose",
    "-i", "--include", "-g", "--globoff", "-N", "--no-buffer",
    "--compressed", "-k", "--insecure", "-f", "--fail",
    "--no-progress-meter", "-#", "--progress-bar",
}

# 带一个参数、会改变实际连接目标或信任链的选项。静默忽略它们会得到“看起来一样、
# 实际发往别处”的草稿，因此必须消费其参数并标记为不可发送。
_CONNECTION_ALTERING_ARG_OPTIONS = {
    "--resolve", "--connect-to", "--host", "--unix-socket",
    "--proxy", "-x", "--interface",
    "--cacert", "--cert", "--key",
}

# 带一个参数、只在客户端生效、不改变请求定义的选项：准确消费，避免参数被当成 URL。
_CLIENT_ONLY_ARG_OPTIONS = {
    "-o", "--output", "-w", "--write-out",
    "-m", "--max-time", "--connect-timeout", "--retry", "--retry-delay",
    "-c", "--cookie-jar", "--limit-rate", "--max-redirs", "--proto",
}

# 会改变发送行为、首版不建模的选项：标记为不可发送，但不丢弃整条命令。
_UNSUPPORTED_FLAGS = {
    "-L", "--location", "--location-trusted",
    "-T", "--upload-file",
    "--http2", "--http1.1", "--http1.0",
}

# 不得以明文写入普通草稿的认证相关请求头。
_SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-auth-token", "x-csrf-token",
}


class CurlParseError(ValueError):
    """cURL 文本无法解析或无法保证等价。"""


@dataclass
class CurlDraft:
    method: str = "GET"
    scheme: str = ""
    host: str = ""
    path: str = ""
    query_params: list[dict[str, str]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    body_type: str = "none"  # none / json / text / form
    body: str = ""
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    auth_hint: dict[str, Any] | None = None

    @property
    def sendable(self) -> bool:
        """存在无法等价表达的部分时为 False，禁止保存后发送。"""
        return not self.unsupported

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url(),
            "query_params": self.query_params,
            "headers": self.headers,
            "body_type": self.body_type,
            "body": self.body,
            "warnings": self.warnings,
            "unsupported": self.unsupported,
            "sendable": self.sendable,
            "auth_hint": self.auth_hint,
        }

    def url(self) -> str:
        if not self.host:
            return ""
        return urlunsplit((self.scheme, self.host, self.path, "", ""))


def _strip_line_continuations(text: str) -> str:
    r"""删掉外层 shell 续行（反斜杠＋换行），其余字符原样保留。

    浏览器复制出来的 cURL 每行以反斜杠续接，而 `shlex` 的 posix 模式把反斜杠当作
    转义字符：`\`＋换行会变成 token 里的一个字面换行，含换行的那一段就被当成另一个
    请求地址，整条多行命令导入失败。这里只补上 shlex 不建模的续行，不做整段替换、
    也不过滤空白 token，交给 shlex 的仍是它本来就处理的结构：

    - 单引号内反斜杠是字面量，其中的 `\`＋换行属于数据，必须保留；
    - 双引号内 `\"` 仍转义（不结束字符串），但 `\`＋换行在 shell 里就是续行，要删；
    - 反斜杠按奇偶配对：偶数个后面跟换行不是续行，换行留在原处当分隔符；
    - 只认两种换行：LF，或粘贴自 Windows 的完整 CRLF；裸 CR 不是换行，属于正文。

    续行后的缩进只在引号外当分隔符；引号内的缩进是正文，照原样保留。

    只建模这一层：不执行 shell，不做命令替换、变量展开，也不处理 PowerShell／cmd
    的续行写法。
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    size = len(text)
    while index < size:
        char = text[index]

        # 单引号内没有转义，下一个单引号就是结束；里面的反斜杠一律是字面量。
        if quote == "'":
            if char == "'":
                quote = None
            out.append(char)
            index += 1
            continue

        if char == "\\":
            # 逐对消费，不回头重扫反斜杠段：当前位置的反斜杠紧邻换行（LF，或完整
            # CRLF）才是续行，与它一起删掉并回到引号状态判断；否则把反斜杠和它转义
            # 的下一个字符原样搬走（`\"` 因此不会在这里被误判成字符串结束）。
            # 非续行走两个字符、续行走两个或三个，扫描因此是线性的一次遍历。
            tail = text[index + 1 : index + 2]
            if tail == "\n":
                index += 2
                continue
            if tail == "\r" and text[index + 2 : index + 3] == "\n":
                index += 3
                continue
            out.append(char)
            index += 1
            if index < size:
                out.append(text[index])
                index += 1
            continue

        if quote == '"':
            if char == '"':
                quote = None
        elif char in ("'", '"'):
            quote = char
        out.append(char)
        index += 1
    return "".join(out)


def _split(text: str) -> list[str]:
    try:
        return shlex.split(_strip_line_continuations(text), posix=True)
    except ValueError as error:
        raise CurlParseError(f"cURL 文本引号不完整：{error}") from error


def _find_command(tokens: list[str]) -> int:
    for idx, token in enumerate(tokens):
        if token == "curl":
            return idx
    if tokens and tokens[0].endswith("curl"):
        return 0
    raise CurlParseError("未找到 curl 命令")


def _classify_body(headers: list[dict[str, str]], raw_body: str) -> str:
    content_type = ""
    for header in headers:
        if header["name"].lower() == "content-type":
            content_type = header["value"].lower()
            break
    if not raw_body:
        return "none"
    if "application/json" in content_type:
        return "json"
    if "application/x-www-form-urlencoded" in content_type:
        return "form"
    stripped = raw_body.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    return "text"


def _add_header(draft: CurlDraft, raw: str) -> None:
    name, _, value = raw.partition(":")
    name = name.strip()
    if name.lower() in _SENSITIVE_HEADER_NAMES:
        # 认证头不落入普通草稿：只产出待配置提示，值被丢弃。
        draft.auth_hint = {"type": "header", "header_name": name, "pending": True}
        draft.warnings.append(
            f"认证头 {name} 已识别，值未保存；请在身份配置中绑定凭证。"
        )
        return
    draft.headers.append({"name": name, "value": value.strip()})


def _consume_value(tokens: list[str], index: int, option: str) -> tuple[str, int]:
    if index + 1 >= len(tokens):
        raise CurlParseError(f"{option} 后缺少参数")
    return tokens[index + 1], index + 1


def _apply_body(draft: CurlDraft, body_parts: list[str]) -> None:
    raw_body = "&".join(body_parts)
    draft.body = raw_body
    draft.body_type = _classify_body(draft.headers, raw_body)


def parse(text: str) -> CurlDraft:
    tokens = _split(text)
    tokens = tokens[_find_command(tokens) + 1 :]

    draft = CurlDraft()
    url: str | None = None
    explicit_method = False
    saw_data = False
    body_parts: list[str] = []
    urls_seen: list[str] = []
    extra_query: list[str] = []

    def record_url(candidate: str) -> None:
        nonlocal url
        urls_seen.append(candidate)
        if url is None:
            url = candidate

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # —— 方法：只有显式指定才锁定，-d 不得覆盖 ——
        if token in ("-X", "--request"):
            value, i = _consume_value(tokens, i, token)
            draft.method = value.upper()
            explicit_method = True
        elif token.startswith("--request="):
            draft.method = token.split("=", 1)[1].upper()
            explicit_method = True
        elif token.startswith("-X") and len(token) > 2:
            draft.method = token[2:].upper()
            explicit_method = True

        # —— 请求头：认证头不落明文 ——
        elif token in ("-H", "--header"):
            value, i = _consume_value(tokens, i, token)
            _add_header(draft, value)
        elif token.startswith("--header="):
            _add_header(draft, token.split("=", 1)[1])

        # —— 正文：区分字面量与文件引用 ——
        elif token in ("-d", "--data", "--data-ascii", "--data-binary"):
            value, i = _consume_value(tokens, i, token)
            if value.startswith("@"):
                draft.unsupported.append(
                    f"{token} 引用文件（{value}），首版不支持读取文件正文；请改为粘贴文本正文。"
                )
            else:
                saw_data = True
                body_parts.append(value)
        elif token == "--data-raw":
            value, i = _consume_value(tokens, i, token)
            saw_data = True
            body_parts.append(value)
        elif token.startswith("--data-raw="):
            saw_data = True
            body_parts.append(token.split("=", 1)[1])
        elif token.startswith(("--data=", "--data-ascii=", "--data-binary=")):
            value = token.split("=", 1)[1]
            if value.startswith("@"):
                draft.unsupported.append(
                    f"{token.split('=')[0]} 引用文件（{value}），首版不支持读取文件正文。"
                )
            else:
                saw_data = True
                body_parts.append(value)

        # —— 表单 / multipart：首版不支持，明确标记且不可发送 ——
        elif token in ("-F", "--form"):
            value, i = _consume_value(tokens, i, token)
            if "=@" in value or "=<" in value:
                draft.warnings.append("文件字段暂不支持，已跳过；请改为手动填写。")
            # 一条命令可带多个 -F，同样的结论只提示一次，避免用户看到重复条目。
            message = "multipart/form-data（-F）首版不支持，不能等价导入；请改用 JSON 或表单正文。"
            if message not in draft.unsupported:
                draft.unsupported.append(message)
            draft.method = "POST" if not explicit_method else draft.method
        elif token.startswith("--form="):
            draft.unsupported.append("multipart/form-data（--form）首版不支持。")
        elif token.startswith("-F") and len(token) > 2:
            draft.unsupported.append("multipart/form-data（-F）首版不支持。")

        # —— 用户显式指定的请求头：忠实转为请求头 ——
        elif token in ("-A", "--user-agent"):
            value, i = _consume_value(tokens, i, token)
            draft.headers.append({"name": "User-Agent", "value": value})
        elif token in ("-e", "--referer"):
            value, i = _consume_value(tokens, i, token)
            draft.headers.append({"name": "Referer", "value": value})

        # —— 查询参数：--url-query 会追加参数，忽略即改变请求 ——
        elif token == "--url-query":
            value, i = _consume_value(tokens, i, token)
            extra_query.append(value)
        elif token.startswith("--url-query="):
            extra_query.append(token.split("=", 1)[1])

        # —— 认证：只留提示，不留明文 ——
        elif token in ("-u", "--user"):
            _value, i = _consume_value(tokens, i, token)
            draft.auth_hint = {"type": "basic", "pending": True}
            draft.warnings.append("Basic 认证已识别，请到身份配置中绑定凭证，不直接导入明文。")
        elif token in ("-b", "--cookie"):
            _value, i = _consume_value(tokens, i, token)
            draft.auth_hint = {"type": "cookie", "pending": True}
            draft.warnings.append("Cookie 已识别，值未保存；请在身份配置中绑定凭证。")
        elif token.startswith("--url="):
            record_url(token.split("=", 1)[1])
        elif token == "--url":
            value, i = _consume_value(tokens, i, token)
            record_url(value)
        elif token in _CONNECTION_ALTERING_ARG_OPTIONS:
            # 消费参数但明确不可发送：导入它会产生与用户命令不同的实际目标。
            _value, i = _consume_value(tokens, i, token)
            draft.unsupported.append(
                f"选项 {token} 会改变实际连接目标或信任链，首版不建模；无法保证等价。"
            )
        elif token in _CLIENT_ONLY_ARG_OPTIONS:
            _value, i = _consume_value(tokens, i, token)
        elif token in _NO_ARG_FLAGS:
            pass
        elif token in _UNSUPPORTED_FLAGS:
            draft.unsupported.append(f"选项 {token} 会改变发送行为，首版不建模；无法保证等价。")
        elif token.startswith("-") and token not in ("-", "--"):
            # 未知选项无法判断是否消耗参数，继续解析可能把参数当成 URL；
            # 按“无法保证等价即拒绝导入”处理，不生成可发送的错误草稿。
            raise CurlParseError(
                f"未知选项 {token}，无法确认等价语义，已拒绝导入；请移除此选项后重试。"
            )
        else:
            record_url(token)

        i += 1

    if len(urls_seen) > 1:
        raise CurlParseError("一次只支持导入单个 URL，检测到多个请求地址。")
    if url is None:
        raise CurlParseError("未找到请求 URL")

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise CurlParseError("仅支持 http/https 目标")
    if parts.username or parts.password:
        # URL userinfo 属凭证，不入草稿。
        draft.auth_hint = {"type": "basic", "pending": True}
        draft.warnings.append("URL 中的用户名/密码已识别，值未保存；请在身份配置中绑定凭证。")
    draft.scheme = parts.scheme
    draft.host = parts.hostname or ""
    if parts.port:
        draft.host = f"{draft.host}:{parts.port}"
    draft.path = parts.path or "/"
    draft.query_params = [
        {"name": name, "value": value}
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    # --url-query 追加的参数排在 URL 自带参数之后，与 curl 的拼接顺序一致。
    for raw in extra_query:
        draft.query_params.extend(
            {"name": name, "value": value}
            for name, value in parse_qsl(raw.lstrip("?&"), keep_blank_values=True)
        )

    if body_parts:
        _apply_body(draft, body_parts)
    # 仅当用户未显式指定方法时，-d 才隐含 POST。
    if saw_data and not explicit_method and draft.method == "GET":
        draft.method = "POST"

    return draft
