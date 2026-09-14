"""检查部署模板是否提供了容器到宿主机的入口，不输出任何 .env 内容。

**为什么需要这一步**：目标守卫把 `host.docker.internal` 当成*必需来源*——它解析到的地址
就是宿主自己的地址，既跑着平台本体、又常常是公司内网的入口。模板把这条入口交给
`extra_hosts: host.docker.internal:host-gateway`（`host-gateway` 在 Linux 与 Docker Desktop
上都解析到宿主自己）。

而**行为上只在 Linux／CI 看得出来**：Docker Desktop 会自己把同名解析注入容器，把
`extra_hosts` 删掉也照样能解析。也就是说“模板到底有没有提供入口”这件事，在开发机上用
真实解析是验证不出来的，只能直接检查模板本身。这里就是那个检查点。

用 `docker compose config` 而不是自己解析 YAML：它才是真正解释模板的那个工具，展开
profile、变量与继承后的结果才是容器真正拿到的配置。

规则与具体服务名无关：**凡是从 `apps/api` 构建的服务都必须声明这条入口**——从那个镜像
起的进程都会加载目标守卫，将来再加一个同类服务会自动被这条规则覆盖，不需要在这里补名字。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REQUIRED_HOST = "host.docker.internal"
REQUIRED_TARGET = "host-gateway"

root = Path(__file__).resolve().parent.parent
guard_image_context = (root / "apps" / "api").resolve()


def _describe(build: object) -> Path | None:
    """服务的构建上下文；不是从本地目录构建的返回 None。"""
    if isinstance(build, dict):
        context = build.get("context")
    else:
        context = build
    if not isinstance(context, str):
        return None
    return Path(context).resolve()


def _declared_hosts(extra_hosts: object) -> dict[str, str]:
    """把 extra_hosts 的两种写法（列表 ``host=ip`` 与映射）统一成 ``host -> ip``。"""
    if isinstance(extra_hosts, dict):
        return {str(host): str(value) for host, value in extra_hosts.items()}
    if isinstance(extra_hosts, list):
        declared = {}
        for entry in extra_hosts:
            if isinstance(entry, str) and "=" in entry:
                host, _, value = entry.partition("=")
                declared[host.strip()] = value.strip()
        return declared
    return {}


def main() -> int:
    if not (root / ".env").exists():
        print("缺少 .env：请先运行 make init（模板检查需要展开变量后的配置）")
        return 1
    # worker 在 profile 后面：不加 profile 的话它不出现在展开结果里，也就漏检。
    result = subprocess.run(
        ["docker", "compose", "--profile", "worker", "config", "--format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        first_line = (result.stderr or "").strip().splitlines()[:1]
        print("无法展开部署模板：" + (first_line[0] if first_line else "docker compose config 失败"))
        return 1
    services = json.loads(result.stdout)["services"]

    guarded = {name: service for name, service in services.items()
               if _describe(service.get("build")) == guard_image_context}
    if not guarded:
        print("部署模板里没有从 apps/api 构建的服务：守卫的宿主入口检查无对象，请核对模板")
        return 1

    missing = []
    for name, service in sorted(guarded.items()):
        if _declared_hosts(service.get("extra_hosts")).get(REQUIRED_HOST) != REQUIRED_TARGET:
            missing.append(name)
    if missing:
        print(
            "部署模板没有提供容器到宿主机的入口："
            + "、".join(missing)
            + f" 缺少 extra_hosts: {REQUIRED_HOST}:{REQUIRED_TARGET}。\n"
            "这些服务会加载目标守卫，宿主入口是必需来源：解析不出来时所有发送都会被拒绝，"
            "指向宿主的用例则可能被真实发出去。请把这条 extra_hosts 补回模板。"
        )
        return 1

    print(f"部署模板已为 {len(guarded)} 个执行守卫的服务提供宿主入口：" + "、".join(sorted(guarded)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
