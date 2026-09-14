"""pytest 配置：测试发现、路径与共享夹具注册。

共享夹具在 `integration/harness.py` 定义，这里只把它们注册进 conftest 命名空间，
由 pytest 自动发现。测试模块不再写 `from harness import account`：那样得到的模块级
名字会被测试函数的同名参数遮蔽，静态检查按“重定义未使用的导入”报错，也容易让人
误以为参数来自导入而不是 pytest 注入。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 使测试可从仓库根导入 app 包，并让 integration 下的 harness／helpers 可被导入。
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "integration"))

from harness import account, client, project  # noqa: E402, F401 - 注册夹具

__all__ = ["account", "client", "project"]
