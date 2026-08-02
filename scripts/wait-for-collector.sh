#!/usr/bin/env bash
# OTel Collector が OTLP を受信できる状態になるまで待つ。
#
# compose の ports 公開はコンテナ起動と同時に有効になるため、ポート疎通確認では
# 「まだ設定を読み終えていない collector」を ready と誤判定する（false ready）。
# そのため collector 自身がサービスグラフ構築完了時に出すログを待つ。
set -euo pipefail

readonly SERVICE=otel-collector
readonly READY_LOG="Everything is ready"
readonly TIMEOUT_SECONDS=60

for _ in $(seq 1 "${TIMEOUT_SECONDS}"); do
  # docker compose logs を直接 grep -q に繋がない。grep -q は最初のマッチで終了するため
  # 書き込み途中の docker が SIGPIPE で死に、pipefail によりパイプライン全体が非0になる。
  # ログ量が少ないうちはレースに勝って通るが、溜まると ready を検知できずタイムアウトする
  logs="$(docker compose logs --no-color "${SERVICE}" 2>/dev/null || true)"
  if printf '%s' "${logs}" | grep -q "${READY_LOG}"; then
    echo "collector ready"
    exit 0
  fi

  # 設定不正や API キー未設定は起動直後の異常終了として現れる。
  # タイムアウトまで待たずに即座に落として原因ログを出す
  cid="$(docker compose ps -q "${SERVICE}")"
  if [ -z "${cid}" ] || [ "$(docker inspect -f '{{.State.Running}}' "${cid}")" != "true" ]; then
    echo "::error::collector が起動直後に終了しました" >&2
    docker compose logs --no-color "${SERVICE}" >&2
    exit 1
  fi

  sleep 1
done

echo "::error::collector が ${TIMEOUT_SECONDS} 秒以内に ready になりませんでした" >&2
docker compose logs --no-color "${SERVICE}" >&2
exit 1
