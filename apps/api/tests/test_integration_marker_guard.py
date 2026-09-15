"""测试分类守卫：集成测试必须带集成标记。

`make check-api` 跑的是 `pytest tests -m "not integration"`，连的是**默认开发库**；
`make check-integration` 才连 `interface_testing_it`。两者靠标记分流，而标记是每个模块
自己写的——新加一个集成模块时漏写一行，它就会被核心门禁选中并对着开发库建账号、建项目、
真实执行请求。门禁本身成了数据污染源，而且不会有任何提示：测试全绿。

这条守卫把“漏写标记”变成一次可复现的失败。它只检查文件是否声明了标记，不重述实现。
"""
from __future__ import annotations

import ast
from pathlib import Path

_INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"


def _has_integration_marker(tree: ast.Module) -> bool:
    """模块顶层是否有 `pytestmark = pytest.mark.integration`。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [item.id for item in node.targets if isinstance(item, ast.Name)]
        if "pytestmark" not in targets:
            continue
        # 只认单标记形态：多标记列表也允许，只要其中出现 integration。
        for item in ast.walk(node.value):
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "integration"
                and isinstance(item.value, ast.Attribute)
                and item.value.attr == "mark"
            ):
                return True
    return False


def test_every_integration_module_declares_the_marker() -> None:
    """集成目录下的每个测试模块都必须带集成标记。"""
    modules = sorted(_INTEGRATION_DIR.glob("test_*.py"))
    assert modules, "集成测试目录里没有找到测试模块，守卫本身需要更新"

    missing = sorted(
        module.name
        for module in modules
        if not _has_integration_marker(ast.parse(module.read_text(encoding="utf-8")))
    )
    assert missing == [], (
        "以下集成测试模块缺少 pytestmark = pytest.mark.integration，"
        f"会被核心门禁选中并写入开发库：{missing}"
    )
