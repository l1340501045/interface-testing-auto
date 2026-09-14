"""初始化仅本机使用的开发密码，不输出秘密、不覆盖已有配置。"""
from pathlib import Path
import base64
import hashlib
import os
import secrets
import socket

root = Path(__file__).resolve().parent.parent
target = root / ".env"
# 数据库口令只要求足够随机，任意可打印字符串都可用。
password_keys = ("POSTGRES_PASSWORD", "APP_RUNTIME_PASSWORD")
# SECRET_KEY 是 Fernet 主密钥，必须正好是 32 字节的 urlsafe base64，否则
# 加密第一份凭证时才在请求里抛出 ValueError。口令和加密密钥是两种不同的
# 材料，不能用同一个生成器。
encryption_keys = ("SECRET_KEY",)
secret_keys = password_keys + encryption_keys


def _new_encryption_key() -> str:
    """与 cryptography 的 Fernet.generate_key() 同一格式，避免宿主脚本引入依赖。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


if target.exists():
    print("保留已有 .env")
else:
    template = (root / ".env.example").read_text()
    content = template
    for key in password_keys:
        content = content.replace(f"{key}=\n", f"{key}=" + secrets.token_urlsafe(32) + "\n")
    for key in encryption_keys:
        content = content.replace(f"{key}=\n", f"{key}=" + _new_encryption_key() + "\n")
    project_id = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    content = content.replace(
        "COMPOSE_PROJECT_NAME=interface-testing-auto-dev",
        "COMPOSE_PROJECT_NAME=interface-testing-auto-" + project_id,
    )
    for key, preferred in [("WEB_PORT", 15173), ("API_PORT", 18080)]:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
            except OSError:
                probe.bind(("127.0.0.1", 0))
            selected = probe.getsockname()[1]
        content = content.replace(f"{key}={preferred}", f"{key}={selected}")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
    print("已生成仅本机使用的开发配置")

values = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line and not line.startswith("#"))
for key in secret_keys:
    if not values.get(key, "").strip():
        raise SystemExit(f"已有 .env 未设置 {key}；请填写后重试，现有配置不会被覆盖")
print("前端预览：http://127.0.0.1:" + values.get("WEB_PORT", "15173"))
print("后端检查：http://127.0.0.1:" + values.get("API_PORT", "18080") + "/health")
