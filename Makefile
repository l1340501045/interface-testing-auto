.PHONY: init dev up stop check check-template check-api check-web check-integration check-db admin pools worker worker-restart worker-down worker-logs
init:
	python3 scripts/init_dev.py
dev: init
	docker compose up --build -d --wait --wait-timeout 90
	docker compose watch --no-up --prune=false
up: init
	docker compose up --build -d --wait --wait-timeout 90
stop:
	docker compose down

# 创建首位管理员：密码只从本机终端读取，不经过命令行参数、不写入日志。
# 已存在管理员时该命令直接跳过，不会覆盖已有账号。
admin:
	docker compose run --rm api python -m app.cli bootstrap

# 列出各项目的执行池 id，供填写 .env 的 WORKER_POOL_IDS。
pools:
	docker compose run --rm api python -m app.cli list-pools

# 独立执行进程：不随 make up 启动。未配置 WORKER_POOL_IDS 时它明确报错退出，
# 不会静默空转或领取任意池，因此授权之后再用这两条命令显式启动。
worker:
	docker compose --profile worker up -d --wait --wait-timeout 90 worker

# worker 是常驻进程，不像管理 API 那样带 --reload：改了 apps/api/app 下的代码后
# 它仍在跑改动前的模块，页面上的执行结果会与源码对不上（新加的校验看上去没生效）。
# 验证执行链路的改动之前先跑这一条。
worker-restart:
	docker compose --profile worker restart worker

worker-down:
	docker compose --profile worker stop worker

worker-logs:
	docker compose --profile worker logs -f worker

# 集成测试使用独立数据库，不触碰开发库数据；建库与迁移都可重复执行。
IT_DATABASE ?= interface_testing_it

check: check-template check-web check-api check-db check-integration
	python3 scripts/smoke.py

# 部署模板契约：凡是从 apps/api 构建的服务（都会加载目标守卫）都必须提供容器到宿主机的
# 入口。Docker Desktop 会自己注入同名解析，所以删掉 extra_hosts 在开发机上照样能解析，
# 只有这一层检查能在任何平台上发现模板缺了它。
check-template:
	python3 scripts/check_deploy_template.py

# 前端：类型检查、组件行为测试与生产构建。
check-web:
	docker compose exec -T web npm run check
	docker compose exec -T web npm run test
	docker compose exec -T web npm run build

# 后端：静态检查与不需要数据库的纯行为测试。
# migrations 与 tests 显式挂载，保证检查的是工作区当前内容而不是镜像里的旧副本。
API_CHECK_MOUNTS = -v ./apps/api/migrations:/app/migrations:ro -v ./apps/api/tests:/app/tests:ro

check-api:
	docker compose run --rm --no-deps $(API_CHECK_MOUNTS) --entrypoint python api -m ruff check --no-cache app migrations tests
	docker compose run --rm --no-deps $(API_CHECK_MOUNTS) --entrypoint python api -m pytest tests -q -m "not integration"

check-db:
	docker compose exec -T db psql -U interface_testing -d interface_testing -v ON_ERROR_STOP=1 < scripts/check_db_comments.sql

# 后端集成：真实 PostgreSQL 上的 RLS、角色隔离、工项领取与结构契约。
check-integration:
	docker compose exec -T db psql -U interface_testing -d postgres -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = '$(IT_DATABASE)'" | grep -q 1 \
		|| docker compose exec -T db psql -U interface_testing -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $(IT_DATABASE)"
	docker compose run --rm --no-deps --entrypoint sh api -c 'PGDATABASE=$(IT_DATABASE) python -m app.cli init-roles && PGDATABASE=$(IT_DATABASE) python -m app.cli migrate'
	docker compose run --rm --no-deps $(API_CHECK_MOUNTS) --entrypoint sh api -c 'PGDATABASE=$(IT_DATABASE) python -m pytest tests/integration -q'
