"""activity.py の単体テスト。

送るか送らないかを分ける境界条件を固定するのが目的のため、
「窓に入る/入らない」「例外を投げる/投げない」の分岐を網羅的に置く。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from activity import (
    METRIC_DEFINITIONS,
    MetricDefinition,
    activity_end_time,
    activity_type_key,
    is_running,
    is_within_window,
    parse_activities,
    recent_running_activities,
    resolve_metric_values,
)

ONE_HOUR = timedelta(hours=1)

# ユーザー提供の実データ。67分ランであることがこのファイルの回帰テストの根拠になっている
REAL_RUN_START_GMT = "2026-07-31 10:20:49"
REAL_RUN_END_GMT = "2026-07-31 11:28:21"
# 毎時13分の cron が、上記ランの終了後に初めて起動する時刻
CRON_RUN_AT = datetime(2026, 7, 31, 12, 13, tzinfo=UTC)


def build_activity(type_key: object, end_time: object) -> dict[str, Any]:
    """テスト用のアクティビティを最小構成で組み立てる。

    引数にデフォルト値を持たせないのは、各テストがどの値に依存しているかを
    呼び出し側で明示させるため。
    """
    return {"activityType": {"typeKey": type_key}, "endTimeGMT": end_time}


class TestParseActivities:
    def test_list_を順序を保った_tuple_にする(self) -> None:
        # Arrange
        response = [{"activityId": 1}, {"activityId": 2}]

        # Act
        activities = parse_activities(response)

        # Assert
        assert activities == ({"activityId": 1}, {"activityId": 2})

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param([], id="空list"),
            pytest.param({"error": "unauthorized"}, id="dict"),
            pytest.param(None, id="None"),
            pytest.param("running", id="str"),
        ],
    )
    def test_list_でない値や空の応答は空タプルになる(self, response: object) -> None:
        # str が1文字ずつ分解されないことも、このケースで担保している
        assert parse_activities(response) == ()


class TestActivityEndTime:
    def test_endTimeGMT_を_UTC_の_aware_datetime_に変換する(self) -> None:
        # Arrange
        activity = build_activity(type_key="running", end_time=REAL_RUN_END_GMT)

        # Act
        end_time = activity_end_time(activity)

        # Assert
        assert end_time == datetime(2026, 7, 31, 11, 28, 21, tzinfo=UTC)

    def test_返り値は_naive_ではなく_UTC_の_aware_datetime(self) -> None:
        # naive のまま返すと now との比較で TypeError になるため、tz を明示的に検証する
        end_time = activity_end_time(build_activity("running", REAL_RUN_END_GMT))

        assert end_time.tzinfo is not None
        assert end_time.utcoffset() == timedelta()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(None, id="None"),
            pytest.param(1785493249000, id="数値"),
            pytest.param("2026-07-31T11:28:21", id="ISO8601形式"),
            pytest.param("", id="空文字"),
            pytest.param("2026-07-31 11:28:21.000", id="末尾に余分な文字"),
        ],
    )
    def test_endTimeGMT_が想定形式でなければ例外になる(self, raw: object) -> None:
        # 握りつぶすと「送らない + 終了コード0」になり、送信停止に気づけなくなる
        with pytest.raises(RuntimeError):
            activity_end_time(build_activity(type_key="running", end_time=raw))

    def test_endTimeGMT_キーそのものが無ければ例外になる(self) -> None:
        with pytest.raises(RuntimeError):
            activity_end_time({"activityType": {"typeKey": "running"}})


class TestIsWithinWindow:
    @pytest.mark.parametrize(
        ("elapsed", "expected"),
        [
            pytest.param(timedelta(minutes=30), True, id="窓内"),
            pytest.param(timedelta(), True, id="下端の境界は含む"),
            pytest.param(
                ONE_HOUR - timedelta(seconds=1), True, id="上端の1秒手前は含む"
            ),
            pytest.param(ONE_HOUR, False, id="上端の境界は半開区間なので含まない"),
            pytest.param(ONE_HOUR + timedelta(seconds=1), False, id="窓より古い"),
            pytest.param(-timedelta(seconds=1), False, id="1秒でも未来なら含まない"),
            pytest.param(-timedelta(days=1), False, id="将来日付の手動登録"),
        ],
    )
    def test_半開区間で判定する(self, elapsed: timedelta, expected: bool) -> None:
        # Arrange
        now = CRON_RUN_AT
        end_time = now - elapsed

        # Act & Assert
        assert is_within_window(end_time, now, ONE_HOUR) is expected


class TestActivityTypeKey:
    def test_typeKey_を取り出す(self) -> None:
        assert activity_type_key(build_activity("running", REAL_RUN_END_GMT)) == "running"

    @pytest.mark.parametrize(
        "activity",
        [
            pytest.param({}, id="activityType欠損"),
            pytest.param({"activityType": None}, id="activityTypeがNone"),
            pytest.param({"activityType": "running"}, id="activityTypeがstr"),
            pytest.param({"activityType": ["running"]}, id="activityTypeがlist"),
            pytest.param({"activityType": {}}, id="typeKey欠損"),
            pytest.param({"activityType": {"typeKey": None}}, id="typeKeyがNone"),
            pytest.param({"activityType": {"typeKey": 17}}, id="typeKeyが数値"),
        ],
    )
    def test_取り出せない形なら例外ではなく_None_を返す(
        self, activity: dict[str, Any]
    ) -> None:
        # 送信対象外のデータでジョブを赤くしないため、ここは例外にしない
        assert activity_type_key(activity) is None


class TestIsRunning:
    @pytest.mark.parametrize(
        ("type_key", "expected"),
        [
            pytest.param("running", True, id="running"),
            pytest.param("walking", False, id="walking"),
            pytest.param("cycling", False, id="cycling"),
            # 現仕様の明示。将来サブタイプもランに含める判断をしたらここが落ちる
            pytest.param("trail_running", False, id="trail_runningは対象外"),
        ],
    )
    def test_種別がランかを判定する(self, type_key: str, expected: bool) -> None:
        activity = build_activity(type_key=type_key, end_time=REAL_RUN_END_GMT)

        assert is_running(activity) is expected

    def test_activityType_が壊れていても例外にならず_False_になる(self) -> None:
        assert is_running({"activityType": "broken"}) is False


class TestRecentRunningActivities:
    def test_ランかつ窓内のものだけを新しい順で返す(self) -> None:
        # Arrange
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        newer_run = build_activity("running", "2026-07-31 11:50:00")
        older_run = build_activity("running", "2026-07-31 11:10:00")
        out_of_window_run = build_activity("running", "2026-07-31 09:00:00")
        walk_in_window = build_activity("walking", "2026-07-31 11:30:00")

        # Act
        targets = recent_running_activities(
            [newer_run, walk_in_window, older_run, out_of_window_run], now, ONE_HOUR
        )

        # Assert
        assert targets == (newer_run, older_run)

    def test_アクティビティが空なら空タプルを返す(self) -> None:
        assert recent_running_activities([], CRON_RUN_AT, ONE_HOUR) == ()

    def test_回帰_67分ランは終了時刻基準なら窓内に入る(self) -> None:
        """開始時刻基準で実装すると、この実データのランが永久に送信されなくなる。

        アクティビティが Garmin に現れるのは終了・同期後であり、次の毎時実行の時点で
        開始からは既に1時間52分が経過している。終了基準なら44分39秒で窓内に収まる。
        """
        # Arrange
        run = build_activity(type_key="running", end_time=REAL_RUN_END_GMT)

        # Act
        targets = recent_running_activities([run], CRON_RUN_AT, ONE_HOUR)

        # Assert
        assert targets == (run,)
        # 開始時刻基準だと窓外に落ちることを対比して固定しておく
        start_time = datetime.strptime(REAL_RUN_START_GMT, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        assert is_within_window(start_time, CRON_RUN_AT, ONE_HOUR) is False

    def test_回帰_endTimeGMT_が壊れたランでない種別が混ざっても例外にならない(self) -> None:
        """is_running を先に見る短絡順序を固定する。順序を入れ替えるとここが落ちる。

        手動登録などランでないアクティビティのスキーマ差異で、送るつもりのないデータのために
        毎時ジョブが赤くなるのを防いでいる。
        """
        # Arrange
        broken_walk = build_activity(type_key="walking", end_time=None)
        run = build_activity(type_key="running", end_time=REAL_RUN_END_GMT)

        # Act
        targets = recent_running_activities([broken_walk, run], CRON_RUN_AT, ONE_HOUR)

        # Assert
        assert targets == (run,)

    def test_ランの_endTimeGMT_が壊れていれば例外にする(self) -> None:
        # 送信対象のスキーマ破損は握りつぶさない（静かな送信停止を防ぐ）
        broken_run = build_activity(type_key="running", end_time="2026/07/31 11:28:21")

        with pytest.raises(RuntimeError):
            recent_running_activities([broken_run], CRON_RUN_AT, ONE_HOUR)


class TestResolveMetricValues:
    def test_定義順を保ち対応する値を返す(self) -> None:
        # Arrange
        definitions = (
            MetricDefinition("test.distance", "distance", "m", "走行距離"),
            MetricDefinition("test.duration", "duration", "s", "経過時間"),
        )
        activity = {"distance": 10009.849609375, "duration": 4052.1220703125}

        # Act
        values = resolve_metric_values(activity, definitions)

        # Assert
        assert [value.definition.name for value in values] == [
            "test.distance",
            "test.duration",
        ]
        assert [value.value for value in values] == [10009.849609375, 4052.1220703125]

    def test_欠損している値は捨てずに_None_のまま返す(self) -> None:
        # スキップをログに出すか否かは呼び手の判断に委ねるため、ここでは落とさない
        definitions = (MetricDefinition("test.max_hr", "maxHR", "bpm", "最大心拍数"),)

        values = resolve_metric_values({}, definitions)

        assert len(values) == 1
        assert values[0].value is None

    def test_実データから_METRIC_DEFINITIONS_の全件が解決できる(self) -> None:
        # Arrange: ユーザー提供の実ペイロードから該当キーだけ抜き出したもの
        activity = {
            "distance": 10009.849609375,
            "duration": 4052.1220703125,
            "averageSpeed": 2.4700000286102295,
            "averageHR": 161.0,
            "maxHR": 178.0,
        }

        # Act
        values = resolve_metric_values(activity, METRIC_DEFINITIONS)

        # Assert
        assert len(values) == 5
        assert all(value.value is not None for value in values)
