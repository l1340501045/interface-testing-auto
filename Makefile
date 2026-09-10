.PHONY: init dev up stop check
init:
	python3 scripts/init_dev.py
dev: init
	docker compose up --build -d --wait --wait-timeout 90
	docker compose watch --no-up --prune=false
up: init
	docker compose up --build -d --wait --wait-timeout 90
stop:
	docker compose down
check:
	docker compose exec -T web npm run check
	docker compose exec -T web npm run build
	docker compose exec -T api python -c 'import ast,pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path("app").rglob("*.py")]; print("后端语法检查通过")'
	docker compose exec -T db psql -U interface_testing -d interface_testing -v ON_ERROR_STOP=1 < scripts/check_db_comments.sql
	python3 scripts/smoke.py
