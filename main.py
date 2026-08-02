"""Garmin Connect の直近に終了したランアクティビティを OpenTelemetry メトリクスとして OTLP 送信する。"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Final

from garminconnect import Garmin

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

# main() のローカル変数 activity と衝突するため、モジュールごとではなく名前で取り込む
from activity import (
    END_TIME_GMT_KEY,
    METRIC_DEFINITIONS,
    activity_type_key,
    is_running,
    parse_activities,
    recent_running_activities,
    resolve_metric_values,
)

METER_NAME: Final = "garminconnect-otel-metrics"
# 計装スコープ名と service.name は本来別概念だが、単一プロセスの小さなジョブのため同一値に揃える
SERVICE_NAME_VALUE: Final = METER_NAME
OTLP_ENDPOINT_ENV: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
DEFAULT_TOKEN_STORE: Final = "~/.garminconnect"
# 毎時実行と同じ幅。cron 遅延でどの実行にもカバーされない時間帯が生じうるのは承知のうえで、
# まずは窓外スキップをログに出して実際のジッタを観測できる形にする（広げるならこの1行）
SEND_WINDOW_HOURS: Final = 1
SEND_WINDOW: Final = timedelta(hours=SEND_WINDOW_HOURS)
# ラン直後に散歩などを記録すると「最新1件」ではランが見えなくなるため余裕をもって取る
ACTIVITY_FETCH_LIMIT: Final = 10
# --now のヘルプとエラーメッセージで同じ例を示すため、1箇所に持つ
NOW_EXAMPLE: Final = "2026-07-31T12:13:00+00:00"


def _prompt_mfa() -> str:
    """MFA コードを対話取得する。非対話環境では原因がわかる例外にして落とす。

    GitHub Actions では stdin が /dev/null のため input() は EOFError になるが、
    真因（保存済みトークンの失効）が読み取れないため復旧手順つきの例外に置き換える。
    """
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Garmin が MFA コードを要求しましたが、非対話環境のため入力できません。"
            "保存済みトークンが失効しています。ローカルで再ログインし、"
            "GitHub Secret の GARMIN_TOKENS を更新してください。"
        )
    return input("MFA code: ")


def login_garmin() -> Garmin:
    """環境変数の認証情報でログイン済みの Garmin クライアントを返す。

    First run: logs in and saves tokens to ~/.garminconnect
    Subsequent runs: loads saved tokens and auto-refreshes
    """
    client = Garmin(
        os.getenv("EMAIL"),
        os.getenv("PASSWORD"),
        prompt_mfa=_prompt_mfa,
    )
    # 引数を明示すると garminconnect 側の GARMINTOKENS フォールバックが効かなくなるため、
    # ここで環境変数を優先する。CI ではトークンを $HOME 外に置きたい
    client.login(os.getenv("GARMINTOKENS") or DEFAULT_TOKEN_STORE)
    return client


def resolve_otlp_endpoint() -> str:
    """OTLP 送信先を環境変数から解決する。未設定なら例外にして落とす。

    未設定でも SDK は http://localhost:4317 に黙ってフォールバックするため、
    送信先を取り違えたままジョブが成功扱いになる。ここで検知して落とす。
    """
    endpoint = os.getenv(OTLP_ENDPOINT_ENV)
    if not endpoint:
        raise RuntimeError(f"{OTLP_ENDPOINT_ENV} が未設定です")
    return endpoint


def build_meter_provider(endpoint: str) -> MeterProvider:
    """OTLP 送信用の MeterProvider を構築し、グローバルに登録して返す。

    service.name は環境で変わらないアプリの同一性のためコードに持ち、
    環境ごとに変わる送信先だけを呼び手から受け取る。
    """
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
    provider = MeterProvider(
        resource=Resource.create({SERVICE_NAME: SERVICE_NAME_VALUE}),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    return provider


def _parse_now(value: str) -> datetime:
    """--now の値を UTC の aware datetime にする。

    naive を通すと activity 側の時刻比較が TypeError になり、原因が読み取れない場所で
    落ちるため、タイムゾーンの明示をここで要求する。UTC に正規化するのは、既定値の
    datetime.now(UTC) と表現を揃え、ログの読み手が時差を暗算せずに済むようにするため。
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"ISO 8601 形式で指定してください（例: {NOW_EXAMPLE}）"
        ) from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"タイムゾーンを含めて指定してください（例: {NOW_EXAMPLE}）"
        )
    return parsed.astimezone(UTC)


def parse_now_argument() -> datetime | None:
    """コマンドライン引数から送信判定の基準時刻を取り出す。未指定なら None。

    ローカルで過去の時刻を指定し、送信まで通る経路を実データで確認できるようにするための入口。
    定期実行は引数を渡さないため、None を返して main() 側の既定（実行時刻）に委ねる。

    Namespace ではなく datetime | None を返すのは、argparse の型を呼び出し側に漏らさないため。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--now",
        type=_parse_now,
        default=None,
        help=f"送信判定の基準時刻。未指定なら実行時刻（例: {NOW_EXAMPLE}）",
    )
    return parser.parse_args().now


def main(now: datetime | None = None) -> int:
    """直近に終了したランのメトリクスだけを送信する。戻り値は終了コード。

    送るものが無い場合も 0 を返す。毎時実行のため、スキップでジョブを赤くしない。

    now は送信判定の基準時刻で、UTC の aware datetime を渡す。省略時は datetime.now(UTC)。
    引数で受け取るのは、判定の基準時刻を実行時刻から切り離し、任意の時刻で挙動を再現
    できるようにするため（activity.py が now を引数で受け取っているのと同じ方針）。

    step:
    1: 送信先を解決する（設定不備ならログイン前に落とす）
    2: Garmin にログインして直近アクティビティを取得する
    3: 基準時刻を確定させ、ラン かつ 直近 SEND_WINDOW 内に終了したものへ絞る
    4: 対象が無ければ理由を出してスキップする
    5: 最新1件のメトリクスを記録し、明示的に flush して送信を確定する
    """
    # 1. 送信先を解決する（設定不備ならログイン前に落とす）
    endpoint = resolve_otlp_endpoint()

    # 2. Garmin にログインして直近アクティビティを取得する
    client = login_garmin()
    activities = parse_activities(client.get_activities(0, ACTIVITY_FETCH_LIMIT))
    if not activities:
        print("アクティビティが見つかりませんでした")
        return 0

    # 3. ラン かつ 直近 SEND_WINDOW 内に終了したものへ絞る
    #    未指定なら現在時刻をここで1度だけ確定させ、以降の判定が同一時刻を見るようにする
    now = now if now is not None else datetime.now(UTC)
    runs = tuple(activity for activity in activities if is_running(activity))
    targets = recent_running_activities(activities, now, SEND_WINDOW)

    # 4. 対象が無ければ理由を出してスキップする
    #    「ランが無い」と「ランはあるが窓外」を区別する。同じ文言だと、cron 遅延で
    #    窓から漏れた欠測が正常時と見分けられなくなる
    if not runs:
        print(
            f"直近 {len(activities)} 件にランがありません"
            f"（最新: type={activity_type_key(activities[0])}）"
        )
        return 0
    if not targets:
        print(
            f"直近 {SEND_WINDOW_HOURS} 時間に終了したランはありません"
            f"（最新のラン: end={runs[0].get(END_TIME_GMT_KEY)}Z）"
        )
        return 0

    activity = targets[0]
    if len(targets) > 1:
        # 同一プロセスの gauge.set() は最後の値しか export されないため、全件送ると
        # 古い方が黙って消える。送信を最新1件に限定し、捨てたことをログに残す
        print(f"note: 対象が {len(targets)} 件ありますが、最新1件のみ送信します")
    print(
        f"target: activityId={activity.get('activityId')} "
        f"end={activity.get(END_TIME_GMT_KEY)}Z"
    )

    # 5. メトリクスを記録し、明示的に flush して送信を確定する
    provider = build_meter_provider(endpoint)
    meter = metrics.get_meter(METER_NAME)
    for definition, value in resolve_metric_values(activity, METRIC_DEFINITIONS):
        if value is None:
            print(f"skip: {definition.name}（値なし key={definition.activity_key}）")
            continue
        gauge = meter.create_gauge(
            definition.name, unit=definition.unit, description=definition.description
        )
        gauge.set(value)
        print(f"recorded: {definition.name}={value}{definition.unit}")

    # PeriodicExportingMetricReader の次回周期を待たずに送信を確定させる
    provider.force_flush()
    provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(parse_now_argument()))
