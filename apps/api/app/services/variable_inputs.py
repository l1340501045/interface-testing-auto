"""影响请求输入的普通变量：合并规则与授权摘要。

凭证授权按用例版本签发，而版本模板里的 `{{...}}` 由项目／环境普通变量在运行时
解析。同一份版本模板可以因为变量不同而指向完全不同的请求，因此授权必须把这些
输入一并绑定：摘要不同即视为另一份请求，原授权不得继续使用。

摘要只含普通变量，秘密不参与；合并规则与执行时使用的规则必须完全同一份代码，
否则“授权时算的是一个值、执行时算的是另一个值”，绑定就落空了。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Environment, ProjectConfigVersion


def merged_variables(session: Session, environment: Environment) -> dict:
    """项目普通变量与环境普通变量合并；环境优先，秘密不参与。"""
    project_version = session.scalar(
        select(ProjectConfigVersion)
        .where(ProjectConfigVersion.project_id == environment.project_id)
        .order_by(ProjectConfigVersion.version.desc())
        .limit(1)
    )
    merged: dict = dict(project_version.variables) if project_version else {}
    merged.update(environment.variables or {})
    return merged


def input_digest(variables: dict) -> str:
    """变量输入的稳定摘要：排序、保留 UTF-8 原文，同一份输入必得同一摘要。"""
    return hashlib.sha256(
        json.dumps(variables, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def environment_input_digest(session: Session, environment: Environment) -> str:
    """某个环境当前生效的输入摘要，供授权签发与执行两处共用。"""
    return input_digest(merged_variables(session, environment))


def frozen_snapshot_digest(snapshot_variables: dict | None) -> str:
    """运行创建时冻结进快照的那批变量的摘要——本次运行准备请求实际使用的输入。

    与 `environment_input_digest` 必须共用同一个 `input_digest`：签发授权时冻的是
    “当时算出来的摘要”，执行时校验的是“这次真正要用的那批变量”，只有两边同一套
    规则，绑定才成立。各写一份实现，两边会在格式或合并顺序上悄悄分叉。
    """
    return input_digest(snapshot_variables or {})
