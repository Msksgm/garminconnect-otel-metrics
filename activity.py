"""Garmin Connect のアクティビティ JSON を解釈する純粋なロジック。

このモジュールは外部 I/O を持たない。ネットワーク・環境変数・現在時刻に触れないことで、
送信するかどうかを分ける判定（半開区間の境界・終了時刻基準・短絡順序・スキーマ破損時の挙動）を
Garmin にログインせず単体テストで固定できるようにしている。

時刻に依存する判定は `now` を引数で受け取る。ここに `datetime.now()` を持ち込むと、
テストが実行時刻に左右されて境界条件を固定できなくなる。

「何時間ぶんを送るか」といった運用ポリシーの定数は置かない（main.py の責務）。
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NamedTuple

type Activity = Mapping[str, Any]

RUNNING_TYPE_KEY: Final = "running"
END_TIME_GMT_KEY: Final = "endTimeGMT"
ACTIVITY_TYPE_KEY: Final = "activityType"
TYPE_KEY: Final = "typeKey"
# endTimeGMT は tz サフィックスを持たない UTC 表記（"2026-07-31 11:28:21"）
GMT_DATETIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"


class MetricDefinition(NamedTuple):
    """送信するメトリクス1件の定義。activity_key は Garmin アクティビティ JSON 上のキー。"""

    name: str
    activity_key: str
    unit: str
    description: str


class MetricValue(NamedTuple):
    """定義と、アクティビティから解決した値。値が取得できなかった場合 value は None。"""

    definition: MetricDefinition
    value: float | None


METRIC_DEFINITIONS: Final[tuple[MetricDefinition, ...]] = (
    MetricDefinition("garmin.activity.running.distance", "distance", "m", "走行距離"),
    MetricDefinition("garmin.activity.running.duration", "duration", "s", "経過時間"),
    MetricDefinition(
        "garmin.activity.running.average_speed", "averageSpeed", "m/s", "平均速度"
    ),
    MetricDefinition(
        "garmin.activity.running.average_hr", "averageHR", "bpm", "平均心拍数"
    ),
    MetricDefinition("garmin.activity.running.max_hr", "maxHR", "bpm", "最大心拍数"),
)


def parse_activities(response: object) -> tuple[Activity, ...]:
    """get_activities() のレスポンスを検証してアクティビティ列にする。新しい順のまま返す。

    このAPIはエラー時に list 以外（dict など）も返しうる外部データのため、型から検証する。
    """
    if not isinstance(response, list):
        return ()
    return tuple(response)


def activity_end_time(activity: Activity) -> datetime:
    """アクティビティの終了時刻を UTC の aware datetime で返す。

    endTimeGMT は tz サフィックスを持たない UTC 文字列のため、ここで UTC を明示し、
    以降の比較で naive/aware を取り違えられない形に正規化する。

    欠損・形式不一致を握りつぶさないのは、この変更で「送らない」が正常な結果に
    なったため。スキーマ破損まで「送らない + 終了コード0」にすると、緑のログのまま
    送信が静かに止まり続ける。resolve_otlp_endpoint() と同じ理由で例外にする。
    """
    raw = activity.get(END_TIME_GMT_KEY)
    if not isinstance(raw, str):
        raise RuntimeError(f"{END_TIME_GMT_KEY} が文字列で取得できません (value={raw!r})")
    try:
        naive = datetime.strptime(raw, GMT_DATETIME_FORMAT)
    except ValueError as error:
        raise RuntimeError(
            f"{END_TIME_GMT_KEY} の形式が想定と異なります (value={raw!r})"
        ) from error
    return naive.replace(tzinfo=UTC)


def is_within_window(end_time: datetime, now: datetime, window: timedelta) -> bool:
    """終了時刻が now を終端とする window 内かを判定する。

    now を引数で受けるのは、判定を実行時刻に依存しない純粋関数に保つため。
    開始時刻ではなく終了時刻で見るのは、アクティビティが Garmin に現れるのが終了・同期後で、
    1時間を超えるランでは取得できた時点で既に開始時刻が窓外になっているため。
    未来側も弾くのは、デバイス時計のずれや将来日付の手動登録を「直近」と見なさないため。
    上端を閉じないのは、境界ちょうどのアクティビティが実行ごとに揺れないようにするため。
    """
    elapsed = now - end_time
    return timedelta() <= elapsed < window


def activity_type_key(activity: Activity) -> str | None:
    """アクティビティ種別（typeKey）を取り出す。取れなければ None。

    activityType は入れ子の外部データのため、KeyError ではなく型から検証する。
    ここで落とすと送信対象外のデータでジョブが赤くなるため、例外にはしない。
    """
    activity_type = activity.get(ACTIVITY_TYPE_KEY)
    if not isinstance(activity_type, Mapping):
        return None
    type_key = activity_type.get(TYPE_KEY)
    return type_key if isinstance(type_key, str) else None


def is_running(activity: Activity) -> bool:
    """アクティビティ種別がランかを判定する。"""
    return activity_type_key(activity) == RUNNING_TYPE_KEY


def recent_running_activities(
    activities: Sequence[Activity], now: datetime, window: timedelta
) -> tuple[Activity, ...]:
    """ラン かつ 直近 window 内に終了したアクティビティを、新しい順のまま返す。

    種別を先に見て and で短絡させる順序が重要。activity_end_time() は例外を投げるため、
    順序を入れ替えると手動登録などランでないアクティビティのスキーマ差異でジョブが落ちる。
    """
    return tuple(
        activity
        for activity in activities
        if is_running(activity)
        and is_within_window(activity_end_time(activity), now, window)
    )


def resolve_metric_values(
    activity: Activity, definitions: Sequence[MetricDefinition]
) -> tuple[MetricValue, ...]:
    """各定義に対応する値をアクティビティから解決する。

    欠損値をここで捨てず None のまま返すのは、スキップをログに出すか否かを呼び手に委ねるため。
    """
    return tuple(
        MetricValue(definition, activity.get(definition.activity_key))
        for definition in definitions
    )
