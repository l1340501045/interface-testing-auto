"""断言取值：把运行中的请求与响应映射为各检查来源的取值。

入参来源（request.*）在发送前求值，出参来源（response.*）在收到响应后求值。
同一个来源名在本模块只有一处取值实现，入参断言、响应断言和试算共用它，
避免前后端或两个阶段出现不一致的字段定位语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fieldlocator import FieldLocatorError, locate
from .lossless_json import LosslessJSONError, loads
from .redaction import contains_secret

PRE_REQUEST_SOURCES = {"request.path", "request.query", "request.header", "request.body", "request.text"}
POST_RESPONSE_SOURCES = {
    "response.status",
    "response.elapsed",
    "response.header",
    "response.cookie",
    "response.body",
    "response.text",
}


@dataclass
class SourceRoots:
    """一次求值可用的取值来源；缺失的来源不在字典里。

    `omitted` 与“字典里没有”是两件事：没有表示该阶段本来就不提供这个来源；
    `omitted` 表示来源存在过、但内容被**明确省略**（例如残缺 JSON 正文），此时
    求值必须失败并说明原因，既不能退回原文做猜测性求值，也不能当成“来源不存在”。
    """

    roots: dict[str, Any] = field(default_factory=dict)
    omitted: dict[str, str] = field(default_factory=dict)

    def add_direct(self, source: str, value: Any) -> None:
        self.roots[source] = {"mode": "direct", "value": value}

    def add_tree(
        self, source: str, root: Any, *, row_indices: dict[str, int] | None = None
    ) -> None:
        self.roots[source] = {
            "mode": "tree",
            "root": root,
            "row_indices": dict(row_indices or {}),
        }

    def add_omitted(self, source: str, reason: str) -> None:
        """登记一个**被明确省略**的来源；不进入 `roots`，因此取不到值。"""
        self.omitted[source] = reason

    def has(self, source: str) -> bool:
        return source in self.roots

    def omitted_reason(self, source: str) -> str | None:
        return self.omitted.get(source)

    def extract(self, source: str, selector: list[dict]) -> tuple[bool, Any]:
        """返回 (found, value)；来源不可用或定位失败均为 found=False。"""
        entry = self.roots.get(source)
        if entry is None:
            return False, None
        if entry["mode"] == "direct":
            if selector:
                raise FieldLocatorError(f"来源 {source} 不接受字段定位")
            return True, entry["value"]
        try:
            result = locate(entry["root"], selector, entry.get("row_indices"))
        except FieldLocatorError:
            return False, None
        return result.found, result.value

    def coordinate(self, source: str, selector: list[dict]) -> tuple | None:
        """把一次取值解析成**真实结构坐标**：对象取字段名，数组取下标。

        敏感标记与敏感性判定必须用同一个坐标系。若标记按名字记录、判定却允许按
        下标定位，同一次取值就会落在两个坐标上，保护被绕过（`headers[0].value`
        与按名字取到的是同一个值，却只有后者被认成受保护位置）。这里与
        `structural_path` 共用同一套遍历，`repeat_key` 这类按名字的别名也先解析
        成它实际命中的下标，别名因此不再是与标记不同的第二套坐标。

        返回 None 表示这次定位在当前结构上解析不出来（字段缺失或类型不符）；调用
        方按缺失处理，本次取值不会带出任何秘密。
        """
        entry = self.roots.get(source)
        if entry is None:
            return None
        if entry["mode"] == "direct":
            return (source,) if not selector else None
        resolved = structural_path(entry["root"], selector, entry.get("row_indices"))
        if resolved is None:
            return None
        return (source, *resolved)


class NameValuePairs(list):
    """请求头／查询参数／响应头／Cookie：协议上就是 {name,value} 的有序可重复集合。

    这是**取值边界上的来源标记**，不是形状判断：只有 `pairs_root` 与
    `parse_cookie_root` 产出它。之所以要标记来源，是因为这类值允许用户写简写
    期望——整份数组、单个 {"name": n, "value": v}、或只写取值 "v"。而正文里的
    业务对象即使**恰好**也叫 name／value，也只是普通对象：若比较时按形状去猜，
    多出来的字段会被丢掉，两个只差一个字段的对象就会被判成相等。所以简写只认
    来源，不认形状。
    """


def pairs_root(pairs: list[dict[str, str]]) -> NameValuePairs:
    """请求头／查询参数／响应头以 {name,value} 数组呈现，支持重复键定位。"""
    return NameValuePairs({"name": item["name"], "value": item["value"]} for item in pairs)


def parse_cookie_root(set_cookie_headers: list[str]) -> NameValuePairs:
    """从响应头解析 Cookie，只取首个 name=value 段。"""
    cookies: NameValuePairs = NameValuePairs()
    for header in set_cookie_headers:
        first = header.split(";", 1)[0]
        name, separator, value = first.partition("=")
        if separator and name.strip():
            cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def json_root(text: str) -> Any:
    """把正文解析为无损 JSON 树；非 JSON 或解析失败返回 None。"""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return loads(stripped)
    except LosslessJSONError:
        return None


def normalize_for_compare(value: Any, compare_as: str | None) -> Any:
    """按 compare_as 把文本值显式转为数值节点；未声明则不隐式转换。"""
    if compare_as is None:
        return value
    from .lossless_json import NumberNode

    if isinstance(value, NumberNode):
        return value
    if isinstance(value, str):
        return NumberNode(text=value.strip())
    return value


def _scalar_text(value: Any) -> str | None:
    """取可参与“是否含秘密”判断的文本；容器与非文本标量返回 None。"""
    from .lossless_json import NumberNode

    if isinstance(value, str):
        return value
    if isinstance(value, NumberNode):
        return value.text
    return None


def _repeat_key_matches(arr: list, key: str) -> list[tuple[int, Any, Any]]:
    """同名重复项的真实位置：返回 (下标, 值坐标, 值)。

    值坐标是取到该重复项值所需的最后一步——字典形态是 `"value"`，两元素数组形态
    是下标 1。记下它，重复键这条别名才能与结构遍历落在同一个坐标上。
    """
    matches: list[tuple[int, Any, Any]] = []
    for index, item in enumerate(arr):
        if isinstance(item, dict) and item.get("name") == key:
            matches.append((index, "value", item.get("value")))
        elif isinstance(item, list) and len(item) >= 2 and item[0] == key:
            matches.append((index, 1, item[1]))
    return matches


def structural_path(
    root: Any, selector: list[dict], row_indices: dict[str, int] | None = None
) -> tuple | None:
    """按定位步骤走一遍真实结构，返回命中的字段名／下标序列。

    与 `locate` 用同一套取值语义（含 `repeat_key` 与 `get` 的缺省），只是额外把
    每一步落在结构里的真实位置记下来，供敏感标记与判定共用。解析不出来返回 None。
    """
    current = root
    path: list[Any] = []
    for step in selector:
        if not isinstance(step, dict):
            return None
        kind = step.get("kind")
        if kind == "key":
            key = step.get("key")
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
            path.append(key)
        elif kind == "index":
            try:
                index = int(step.get("index"))
            except (TypeError, ValueError):
                return None
            if not isinstance(current, list) or index < 0 or index >= len(current):
                return None
            current = current[index]
            path.append(index)
        elif kind == "repeat_key":
            if not isinstance(current, list):
                return None
            try:
                occurrence = int(step.get("occurrence", 0))
            except (TypeError, ValueError):
                return None
            matches = _repeat_key_matches(current, step.get("key"))
            if occurrence < 0 or occurrence >= len(matches):
                return None
            index, value_coord, value = matches[occurrence]
            current = value
            path.extend([index, value_coord])
        elif kind == "row":
            index = (row_indices or {}).get(step.get("row_id"))
            if index is None or not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
            path.append(index)
        else:
            return None
    return tuple(path)


def _walk_sensitive(node: Any, path: list, needles: list[str], found: set[tuple]) -> None:
    """按真实结构遍历，标注一切含秘密的取值坐标。

    对象按字段名、数组按下标，与 `structural_path` 同一坐标系。容器本身不在这里
    标注：包含秘密的祖先由 `is_sensitive_path` 的前缀判定覆盖，不必重复记录。

    **字段名也是取值的一部分**：秘密可能整个作为对象键出现
    （目标回显 `{"<凭据>": "ok"}`）。只看值会漏掉这种形态，于是“整对象相等”
    这类断言既求得了值、又把明文键写进结果——布尔结果本身就成了猜秘密的通道。
    因此键名命中时，把该键自身的坐标也标为敏感；父容器由前缀判定顺带覆盖。

    **判定用的是原文比较**，与证据写出共用 `contains_secret`：它按 JSON 转义解码后
    比较，因此 `response.text` 里写作 `\\u0061bc` 的回显同样会被标为敏感。识别与
    遮蔽若用了两套口径，就会出现“报告里已经遮蔽、断言的真假却仍能逐位猜出秘密”
    的通道——遮住结果却留下可比对的真假，比不遮更糟。
    """
    text = _scalar_text(node)
    if text is not None:
        if contains_secret(text, needles):
            found.add(tuple(path))
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = _scalar_text(key)
            if key_text is not None and contains_secret(key_text, needles):
                found.add(tuple(path + [key]))
            _walk_sensitive(value, path + [key], needles, found)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_sensitive(value, path + [index], needles, found)


def paths_containing(roots: SourceRoots, needles: list[str]) -> set[tuple]:
    """反查实际包含敏感值的取值坐标（来源 + 定位步骤）。

    秘密可能被目标原样回显：注入的认证头会出现在响应正文的某个字段里。只按“注入
    位置”标注敏感路径，回显字段就不在保护范围内——对它求值会把明文写进结果，而且
    在写结果之前布尔结果本身已经成为猜秘密的通道。因此按实际内容反查坐标：谁的取值
    里出现了秘密，谁就是受保护位置。包含秘密的祖先容器由前缀判定覆盖，不必另行标注。
    """
    found: set[tuple] = set()
    for source, entry in roots.roots.items():
        if entry["mode"] == "direct":
            _walk_sensitive(entry["value"], [source], needles, found)
        else:
            _walk_sensitive(entry["root"], [source], needles, found)
    return found


__all__ = [
    "POST_RESPONSE_SOURCES",
    "PRE_REQUEST_SOURCES",
    "SourceRoots",
    "json_root",
    "normalize_for_compare",
    "NameValuePairs",
    "pairs_root",
    "parse_cookie_root",
    "paths_containing",
    "structural_path",
]
