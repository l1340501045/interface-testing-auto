"""检查本机开发入口，不输出或传输环境密码。"""
import json
from pathlib import Path
from urllib.request import urlopen

root = Path(__file__).resolve().parent.parent
values = dict(
    line.split("=", 1)
    for line in (root / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
web_port = int(values.get("WEB_PORT", "15173"))
api_port = int(values.get("API_PORT", "18080"))
if not all(1 <= port <= 65535 for port in (web_port, api_port)):
    raise SystemExit("开发端口无效")
for name, url in (
    ("后端", f"http://127.0.0.1:{api_port}/health"),
    ("前端代理", f"http://127.0.0.1:{web_port}/api/health"),
):
    with urlopen(url, timeout=5) as response:
        result = json.load(response)
    if result.get("status") != "ok" or result.get("database") != "connected":
        raise SystemExit(name + "检查失败")
    print(name + "与数据库连接正常")
with urlopen(f"http://127.0.0.1:{web_port}/", timeout=5) as response:
    if '<div id="root">' not in response.read().decode():
        raise SystemExit("前端入口无效")
print("开发环境冒烟检查通过")
