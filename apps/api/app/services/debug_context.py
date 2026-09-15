"""调试运行的来源证明：不透明关联标记与执行器语义标记。

报告的 `context` 用来回答“这个结果是按哪份配置、哪份输入产生的”。它必须由**服务端
冻结的对象**生成，不能重新去读当前环境——环境在入队之后可能已被改动，重新读出来的
是另一份配置，用它标历史结果等于把“曾经记录的配置”当成“实际执行依据”。

只有已经执行了本轮环境冻结与必需认证保护的那一版执行器写下的记录才给得出这个
结论。判据是执行器自己写在步骤证据里的语义标记，不是入队 API 的版本声明：入队成功
只说明请求被受理，不说明保护真的跑过。

## 为什么标记是 HMAC 而不是内容摘要

正文里有敏感字段时，报告会把它们的**值**遮蔽掉，但内容摘要本身仍然是一个稳定的
比对入口：摘要相同就说明两次的正文一字不差。于是任何能读到报告的人，都可以拿一段
低熵候选（手机号、身份证号、固定口令、固定 JSON 前缀）离线算出摘要去撞——遮蔽了值，
却留下了判断“是不是这一个”的能力。跨主体比对同理：同一份内容在两个主体下算出同一
个摘要，就能把它当作关联键把两边串起来。

因此这里不用裸 SHA：

- 用服务端已有的受保护主密钥做 **HMAC-SHA256**，没有密钥就造不出候选摘要，离线
  猜测不成立；
- 每个用途一个**独立域标签**，两类标记不能互相换算，也不能与其它已存在的摘要互换；
- 标记**绑定 workspace、project 与发起主体**，同一份内容在不同主体下得到不同标记，
  跨主体比对不成立，报告因此不会变成跨主体的探测接口。

HMAC 只用于这两类**对外关联标记**。用途授权仍用原有的 `debug_snapshot_digest`：
换掉它会让已签发的授权全部失配，而它本来就只在授权流程内部使用，不对外返回。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

# 执行器在步骤证据里留下的语义标记。
#
# 键名带前缀，避免与请求证据自身的字段重名；报告读取时会把这一项剥离，不把它当作
# 用户可见的请求内容。改动词义（例如又加了一层保护）时必须换新值：旧记录的证明范围
# 与新记录不同，不能让两者共用一个标记而看起来等价。
GUARD_SEMANTICS_KEY = "__platform_guard"
GUARD_SEMANTICS = "environment_frozen_auth_enforced_v1"

# 用途域标签。改动词义时必须换新值：换了标签等于换了算法，旧标记不再匹配——这正是
# 需要的语义，而不是靠比较字符串去猜。
PURPOSE_SNAPSHOT = "debug-context/snapshot/v1"
PURPOSE_INPUT = "debug-context/input/v1"
# 主密钥标识自己的域，与上面两个都不同，避免把标记本身当成密钥标识。
PURPOSE_KEY_ID = "debug-context/key-id/v1"

_CONTEXT_BINDING_KEY = "context_binding"


@dataclass(frozen=True)
class ContextBinding:
    """冻结在运行快照里的关联标记；`key_id` 只用于判断主密钥是否已轮换。"""

    key_id: str
    snapshot_fingerprint: str
    input_fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "input_fingerprint": self.input_fingerprint,
        }


def key_id(key: bytes) -> str:
    """主密钥的稳定标识：只暴露“是不是同一把密钥”，不暴露密钥内容。

    主密钥轮换后，历史记录上的标记是用旧密钥算的，与当前密钥下的任何输入都不再可能
    相等。没有这个标识就只能两难：要么猜着复用旧值（把两把密钥下的结论混在一起），
    要么每次重新计算（结果必然不等，但读起来像“内容变了”）。有了它，轮换能被明确
    识别，报告可以如实降级为“历史记录”。
    """
    return hmac.new(key, PURPOSE_KEY_ID.encode(), hashlib.sha256).hexdigest()[:16]


def build_context_binding(
    key: bytes,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    principal_id: uuid.UUID,
    request: dict[str, Any],
    assertions: list[dict[str, Any]],
    variables: dict[str, Any],
) -> ContextBinding:
    """构造运行来源的关联标记。

    `snapshot_fingerprint` 只覆盖规范化请求与断言（“这份执行内容是什么”），
    `input_fingerprint` 再加上普通变量（“用哪批输入解析出来的”）。两者都绑定作用域
    与主体，且**都不含**认证注入值或密文：注入值从来不在请求定义里，密文也不参与。
    """
    scope = {
        "workspace_id": str(workspace_id),
        "project_id": str(project_id),
        "principal_id": str(principal_id),
    }
    snapshot_payload = {"request": request, "assertions": assertions}
    input_payload = {**snapshot_payload, "variables": variables}
    return ContextBinding(
        key_id=key_id(key),
        snapshot_fingerprint=_fingerprint(key, PURPOSE_SNAPSHOT, scope, snapshot_payload),
        input_fingerprint=_fingerprint(key, PURPOSE_INPUT, scope, input_payload),
    )


def _fingerprint(
    key: bytes, purpose: str, scope: dict[str, str], payload: dict[str, Any]
) -> str:
    """带用途域与作用域的 HMAC-SHA256；作用域参与消息，主体不同则标记不同。"""
    message = json.dumps(
        {"purpose": purpose, "scope": scope, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def read_context_binding(snapshot: dict[str, Any] | None) -> ContextBinding | None:
    """从冻结快照里取出关联标记；缺失或形状不对时返回 None。"""
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get(_CONTEXT_BINDING_KEY)
    if not isinstance(raw, dict):
        return None
    values = (raw.get("key_id"), raw.get("snapshot_fingerprint"), raw.get("input_fingerprint"))
    if not all(isinstance(item, str) and item for item in values):
        return None
    return ContextBinding(
        key_id=values[0], snapshot_fingerprint=values[1], input_fingerprint=values[2]
    )


def binding_is_current(binding: ContextBinding, key: bytes) -> bool:
    """这份标记是否由**当前**主密钥产生。

    不相等只说明主密钥轮换过，不说明内容变化。此时既不重算（重算出来的是当前密钥下
    的值，贴到旧记录上就是替它编一个没有依据的结论），也不猜测两者是否对应，直接按
    历史记录展示。
    """
    return binding.key_id == key_id(key)


def stamp_guard_semantics(evidence: dict[str, Any]) -> dict[str, Any]:
    """在请求证据上盖执行器语义标记；非对象证据原样返回。"""
    if not isinstance(evidence, dict):
        return evidence
    return {**evidence, GUARD_SEMANTICS_KEY: GUARD_SEMANTICS}


def strip_guard_semantics(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    """剥掉内部标记，返回用户可见的请求证据。"""
    if not isinstance(evidence, dict):
        return evidence
    return {key: value for key, value in evidence.items() if key != GUARD_SEMANTICS_KEY}


def has_guard_semantics(evidence: dict[str, Any] | None) -> bool:
    """这条步骤证据是否由已经执行本轮保护的执行器写下。"""
    return isinstance(evidence, dict) and evidence.get(GUARD_SEMANTICS_KEY) == GUARD_SEMANTICS


__all__ = [
    "GUARD_SEMANTICS",
    "GUARD_SEMANTICS_KEY",
    "ContextBinding",
    "binding_is_current",
    "build_context_binding",
    "has_guard_semantics",
    "key_id",
    "read_context_binding",
    "stamp_guard_semantics",
    "strip_guard_semantics",
]
