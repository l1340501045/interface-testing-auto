"""请求路径与 URL 证据：准备之后的路径口径、编码与安全重建。

两处根因都在“取的是哪一份路径／哪一份 URL”：

- 断言来源 `request.path` 取的是**准备之前**的请求模板。路径里带变量时
  （`/{{operation}}`），实际发出去的是 `/echo`，断言却在校对 `/{{operation}}`——
  核对的是一个从未出现在线上的字符串，发对了反而判失败。
- 请求证据里的 URL 取的是**已经编码**的 `prepared.url`，再做原文替换。秘密里含
  `+`／`/`／`=` 时上线形态是 `a%2Bb%2Fc%3D`，原文串一个字符都不在 URL 里，报告
  留下的是一份 URL 解码就能还原的副本。
- 断言来源先前只取了**用例自己的**路径。环境把基础路径写在 `base_url` 里时
  （`http://echo:8080/api/v1`），线上发的是 `/api/v1/echo`，断言却还在核对
  `/echo`：又是对着一个从未发出过的地址判真假。

三者共用同一条修复思路：从 `PreparedRequest` 拿真实数据（用例相对路径、完整最终
路径、未编码的查询项），编码只走一个入口，各出口都不自行拼接。纯方法测试：不连接
数据库、不发送请求。
"""
from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from app.kernel.redaction import MASK
from app.kernel.request_spec import prepare
from app.kernel.variables import ValueLiteral, VariableResolutionError, VariableResolver
from app.services.executor import _build_request_roots, _evidence_url

_BASE = "http://echo:8080"
# 环境把网关前缀写在 base_url 里：线上路径必然带这一段，断言与证据都得跟上。
_BASE_WITH_PATH = "http://echo:8080/api/v1"
# 四个字符各自会撞上不同的编码规则：`+` 与空格在查询里编码不同、`/` 与 `=` 是分隔符。
ENCODED_SECRET = "a+b/c= d"


def _resolver(values: dict[str, str] | None = None) -> VariableResolver:
    return VariableResolver(
        {name: ValueLiteral.from_dict({"type": "string", "text": text}) for name, text in (values or {}).items()}
    )


def _spec(**overrides: object) -> dict:
    spec = {
        "method": "GET",
        "path": "/echo",
        "query_params": [],
        "headers": [],
        "body_type": "none",
        "body": "",
    }
    spec.update(overrides)
    return spec


# —— 路径：用例相对路径与完整最终路径是两个字段 ——


def test_resolved_path_is_exposed_after_variable_resolution() -> None:
    prepared = prepare(
        _spec(path="/{{operation}}"), _BASE, _resolver({"operation": "echo"})
    )
    assert prepared.relative_path == "/echo"
    # 环境没有基础路径时两者相等：完整路径就是用例路径，不是“另一套拼法”。
    assert prepared.final_path == "/echo"
    assert prepared.url_path == "/echo"
    assert prepared.url == f"{_BASE}/echo"


def test_request_path_root_uses_the_prepared_path_not_the_template() -> None:
    """`request.path` 必须是实际发出去的那一条，不是请求定义里的模板。"""
    spec = _spec(path="/{{operation}}")
    prepared = prepare(spec, _BASE, _resolver({"operation": "echo"}))

    roots = _build_request_roots(prepared, spec)

    assert roots.roots["request.path"]["value"] == "/echo"


def test_url_path_is_the_encoded_path_component_without_query() -> None:
    """`url_path` 的语义要明确：最终 URL 的路径段（编码后），不含查询串。"""
    prepared = prepare(
        _spec(path="/数据/{{name}}", query_params=[{"name": "q", "value": "1 2"}]),
        _BASE,
        _resolver({"name": "报表"}),
    )
    assert prepared.relative_path == "/数据/报表"
    assert prepared.final_path == "/数据/报表"
    assert prepared.url_path == "/%E6%95%B0%E6%8D%AE/%E6%8A%A5%E8%A1%A8"
    assert urlsplit(prepared.url).path == prepared.url_path
    assert urlsplit(prepared.url).query == "q=1+2"


# —— 环境基础路径：既不能丢，也不能拼两遍 ——


def test_environment_base_path_is_part_of_the_asserted_path() -> None:
    """环境写在 `base_url` 里的前缀属于最终路径：断言核对的正是线上那一条。"""
    spec = _spec(path="/{{operation}}")
    prepared = prepare(spec, _BASE_WITH_PATH, _resolver({"operation": "echo"}))

    # 用例相对路径仍然只是用例自己的那一段：证据 URL 靠它重建才不会被拼两遍。
    assert prepared.relative_path == "/echo"
    assert prepared.final_path == "/api/v1/echo"
    assert prepared.url_path == "/api/v1/echo"
    assert prepared.url == f"{_BASE_WITH_PATH}/echo"
    assert urlsplit(prepared.url).path == prepared.url_path

    roots = _build_request_roots(prepared, spec)

    # 这里的 `/echo` 是一个从未在线上出现过的地址：核对它等于发对了反而失败。
    assert roots.roots["request.path"]["value"] == "/api/v1/echo"


def test_evidence_url_prepends_the_base_path_exactly_once() -> None:
    """证据 URL 传的是用例相对路径：重建后与线上一致，前缀不多不少。"""
    prepared = prepare(_spec(path="/{{operation}}"), _BASE_WITH_PATH, _resolver({"operation": "echo"}))

    url = _evidence_url(prepared, [])

    assert url == prepared.url == f"{_BASE_WITH_PATH}/echo"
    assert "/api/v1/api/v1" not in url, "传错字段会把基础路径拼成两份"


def test_empty_base_path_leaves_the_relative_path_alone() -> None:
    prepared = prepare(_spec(path="/echo"), _BASE, _resolver())

    assert prepared.final_path == prepared.relative_path == "/echo"
    assert prepared.url_path == "/echo"


def test_trailing_slash_on_the_base_does_not_repeat_or_drop_the_prefix() -> None:
    """环境写成 `…/api/v1/` 时前缀仍只有一份，且断言与线上取值一致。"""
    prepared = prepare(_spec(path="/echo"), f"{_BASE_WITH_PATH}/", _resolver())

    assert prepared.final_path == "/api/v1/echo"
    assert prepared.url == f"{_BASE_WITH_PATH}/echo"
    assert unquote(prepared.url_path) == prepared.final_path


def test_base_path_and_variable_are_both_present_in_the_final_path() -> None:
    """基础路径与变量同时存在：两段都在，顺序为“环境前缀 + 用例路径”。"""
    spec = _spec(path="/v2/{{resource}}/{{id}}")
    prepared = prepare(
        spec, _BASE_WITH_PATH, _resolver({"resource": "users", "id": "42"})
    )

    assert prepared.relative_path == "/v2/users/42"
    assert prepared.final_path == "/api/v1/v2/users/42"
    assert _build_request_roots(prepared, spec).roots["request.path"]["value"] == (
        "/api/v1/v2/users/42"
    )
    assert _evidence_url(prepared, []) == f"{_BASE_WITH_PATH}/v2/users/42"


def test_encoded_path_keeps_the_base_path_prefix() -> None:
    """编码后仍带前缀：`url_path` 与最终 URL 的路径段逐字符一致。"""
    prepared = prepare(_spec(path="/数据"), _BASE_WITH_PATH, _resolver())

    assert prepared.url_path == "/api/v1/%E6%95%B0%E6%8D%AE"
    assert prepared.url_path == urlsplit(prepared.url).path
    assert unquote(prepared.url_path) == prepared.final_path == "/api/v1/数据"


def test_unresolved_variable_still_fails_before_prepare_returns() -> None:
    """路径里的变量未定义时依旧在发送前失败，不因新增字段而放宽。"""
    with pytest.raises(VariableResolutionError, match="未定义变量"):
        prepare(_spec(path="/{{operation}}"), _BASE, _resolver())


# —— URL 证据：先按敏感性处理，再编码 ——


def test_evidence_url_rebuild_matches_the_wire_exactly_without_secrets() -> None:
    """没有秘密时重建结果必须与线上逐字符相同：普通参数不能因脱敏而走样。"""
    prepared = prepare(
        _spec(
            path="/echo",
            query_params=[
                {"name": "tag", "value": "a b&c=d"},
                {"name": "tag", "value": "第二个"},
                {"name": "中文", "value": "值"},
            ],
        ),
        _BASE,
        _resolver(),
    )
    assert _evidence_url(prepared, []) == prepared.url


def test_evidence_url_masks_a_secret_that_only_exists_encoded() -> None:
    """秘密含 `+`／`/`／`=`／空格时必须先遮蔽再编码，解码也还原不出来。"""
    prepared = prepare(
        _spec(
            path="/echo",
            query_params=[{"name": "tag", "value": "普通"}, {"name": "tag", "value": "重复"}],
        ),
        _BASE,
        _resolver(),
        injected_query=[("api_key", ENCODED_SECRET)],
    )
    assert ENCODED_SECRET not in prepared.url, "线上本来就是编码形态，为此用例的前提"

    url = _evidence_url(prepared, [ENCODED_SECRET])

    assert ENCODED_SECRET not in url
    assert ENCODED_SECRET not in unquote(url), "解码不得还原出秘密"
    assert not any(part == ENCODED_SECRET for _, part in parse_qsl(urlsplit(url).query))
    # 认证值被整段遮蔽，掩码在查询串里可读；其他参数（含重复项与顺序）原样保留。
    assert parse_qsl(urlsplit(url).query) == [
        ("tag", "普通"),
        ("tag", "重复"),
        ("api_key", MASK),
    ]


def test_evidence_url_masks_a_secret_inside_the_path_too() -> None:
    """路径里出现秘密时同样遮蔽，遮蔽后再编码，不留可解码的副本。"""
    prepared = prepare(
        _spec(path="/echo/{{token}}"), _BASE, _resolver({"token": ENCODED_SECRET})
    )
    url = _evidence_url(prepared, [ENCODED_SECRET])
    assert ENCODED_SECRET not in unquote(url)
    assert MASK in unquote(url)


def test_evidence_url_keeps_ordinary_query_values_intact() -> None:
    """脱敏只动秘密：相邻的普通参数（含重复与顺序）不因重建而改变。"""
    prepared = prepare(
        _spec(
            path="/echo",
            query_params=[
                {"name": "page", "value": "2"},
                {"name": "filter", "value": "name=张三&city=北京"},
            ],
        ),
        _BASE,
        _resolver(),
        injected_query=[("api_key", ENCODED_SECRET)],
    )
    url = _evidence_url(prepared, [ENCODED_SECRET])
    assert parse_qsl(urlsplit(url).query, keep_blank_values=True) == [
        ("page", "2"),
        ("filter", "name=张三&city=北京"),
        ("api_key", MASK),
    ]
