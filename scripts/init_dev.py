"""初始化仅本机使用的开发密码，不输出秘密、不覆盖已有配置。"""
from pathlib import Path
import hashlib
import os
import secrets
import socket

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    print("保留已有 .env")
else:
    template = (root / ".env.example").read_text()
    content = template.replace("POSTGRES_PASSWORD=\n", "POSTGRES_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    project_id = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    content = content.replace("COMPOSE_PROJECT_NAME=interface-testing-auto-dev", "COMPOSE_PROJECT_NAME=interface-testing-auto-" + project_id)
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
if not values.get("POSTGRES_PASSWORD", "").strip():
    raise SystemExit("已有 .env 未设置 POSTGRES_PASSWORD；请填写后重试，现有配置不会被覆盖")
print("前端预览：http://127.0.0.1:" + values.get("WEB_PORT", "15173"))
print("后端检查：http://127.0.0.1:" + values.get("API_PORT", "18080") + "/health")
