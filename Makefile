.PHONY: uv.sync
uv.sync: ## uv sync --locked（uv.lock が pyproject.toml と一致するか検証して同期する）
	@uv sync --locked

.PHONY: uv.run
uv.run: ## uv run python main.py（NOW=<ISO8601> で送信判定の基準時刻を指定できる）
	@uv run python main.py $(if $(NOW),--now '$(NOW)')

.PHONY: uv.test
uv.test: ## uv run pytest（単体テストを実行する）
	@uv run pytest -q

.PHONY: docker.compose.up
docker.compose.up: ## docker compose up
	@docker compose up

.PHONY: docker.compose.up.d
docker.compose.up.d: ## docker compose up -d（バックグラウンド起動）
	@docker compose up -d

# dry-run の config は Mackerel / New Relic exporter を持たないため API キーは使われない。
# ただし compose.yaml の `:?` 検証はファイル読み込み時に評価されるためダミー値で満たす。
# 停止・ログ・撤収は docker.compose.stop / logs / down をそのまま使える
.PHONY: docker.compose.up.d.dryrun
docker.compose.up.d.dryrun: ## dry-run（本番 APM に送らず debug exporter に出すだけ）で Collector を起動する
	@COMPOSE_FILE=compose.yaml:compose.dryrun.yaml \
	MACKEREL_API_KEY=dry-run NEW_RELIC_API_KEY=dry-run \
	docker compose up -d

.PHONY: docker.compose.wait
docker.compose.wait: ## OTel Collector が OTLP を受信できるようになるまで待つ
	@./scripts/wait-for-collector.sh

.PHONY: docker.compose.stop
docker.compose.stop: ## batch processor を drain してから Collector を停止する
	@sleep 5
	@docker compose stop --timeout 30

.PHONY: docker.compose.logs
docker.compose.logs: ## docker compose logs
	@docker compose logs --no-color --timestamps

.PHONY: docker.compose.down
docker.compose.down: ## docker compose down
	@docker compose down

################################################################################
# Utility-Command help
################################################################################
.DEFAULT_GOAL := help

################################################################################
# マクロ
################################################################################
# Makefileの中身を抽出してhelpとして1行で出す
# $(1): Makefile名
# 使い方例: $(call help,{included-makefile})
define help
grep -E '^[\.a-zA-Z0-9_-]+:.*?## .*$$' $(1) \
| grep --invert-match "## non-help" \
| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
endef

################################################################################
# タスク
################################################################################
.PHONY: help
help: ## Make タスク一覧
	@echo '######################################################################'
	@echo '# Makeタスク一覧'
	@echo '# $$ make XXX'
	@echo '# or'
	@echo '# $$ make XXX --dry-run'
	@echo '######################################################################'
	@echo $(MAKEFILE_LIST) \
	| tr ' ' '\n' \
	| xargs -I {included-makefile} $(call help,{included-makefile})
