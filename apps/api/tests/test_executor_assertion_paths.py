"""执行器把断言配置还原为内核保护坐标的行为测试。

内核按“取值来源 + 定位步骤”判断是否触到受保护值；执行器若一律传空路径，
这条保护实际永远不会生效。这里只验证纯映射，不连接数据库、不发送请求。
"""
from __future__ import annotations

import pytest

from app.kernel.assertions import AssertionConfigError
from app.services.executor import _assertion_path


def test_direct_source_maps_to_source_name() -> None:
    assert _assertion_path({"target_source": "response.status"}) == ("response.status",)


def test_key_and_index_steps_are_part_of_the_path() -> None:
    item = {
        "target_source": "response.body",
        "selector": [
            {"kind": "key", "key": "data"},
            {"kind": "index", "index": 0},
            {"kind": "key", "key": "id"},
        ],
    }
    assert _assertion_path(item) == ("response.body", "data", 0, "id")


def test_repeat_key_uses_the_key_not_the_occurrence() -> None:
    """同名槽位的每一次出现都必须落在保护范围内。

    若把序号写进坐标，用户给第二处同名头部配断言就能绕过保护。
    """
    item = {
        "target_source": "request.header",
        "selector": [{"kind": "repeat_key", "key": "Authorization", "occurrence": 1}],
    }
    assert _assertion_path(item) == ("request.header", "Authorization")


def test_unknown_step_is_a_config_error() -> None:
    with pytest.raises(AssertionConfigError):
        _assertion_path({"target_source": "response.body", "selector": [{"kind": "wildcard"}]})
