import os
from datetime import date
from garminconnect import Garmin

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

# First run: logs in and saves tokens to ~/.garminconnect
# Subsequent runs: loads saved tokens and auto-refreshes
client = Garmin(
    os.getenv("EMAIL"),
    os.getenv("PASSWORD"),
    prompt_mfa=lambda: input("MFA code: "),
)
client.login("~/.garminconnect")

activities = client.get_activities(0, 1)
if not (isinstance(activities, list) and activities):
    print("アクティビティが見つかりませんでした")
    raise SystemExit(0)

activity = activities[0]
activity_type = activity["activityType"]["typeKey"]
if activity_type != "running":
    print(
        f"最新アクティビティはランではありません(type={activity_type})。送信をスキップします。"
    )
    raise SystemExit(0)

METRIC_DEFINITIONS = [
    ("garmin.activity.running.distance", "distance", "m", "走行距離"),
    ("garmin.activity.running.duration", "duration", "s", "経過時間"),
    ("garmin.activity.running.average_speed", "averageSpeed", "m/s", "平均速度"),
    ("garmin.activity.running.average_hr", "averageHR", "bpm", "平均心拍数"),
    ("garmin.activity.running.max_hr", "maxHR", "bpm", "最大心拍数"),
]


reader = PeriodicExportingMetricReader(OTLPMetricExporter())
provider = MeterProvider(
    resource=Resource.create({}),
    metric_readers=[reader],
)
metrics.set_meter_provider(provider)

meter = metrics.get_meter("garminconnect-lambda-mackerel")


for name, key, unit, description in METRIC_DEFINITIONS:
    value = activity.get(key)
    if value is None:
        print(f"skip: {name}（値なし key={key}）")
        continue
    gauge = meter.create_gauge(name, unit=unit, description=description)
    gauge.set(value)
    print(f"recorded: {name}={value}{unit}")

provider.force_flush()
provider.shutdown()
