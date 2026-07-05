import os
from datetime import date
from garminconnect import Garmin

# First run: logs in and saves tokens to ~/.garminconnect
# Subsequent runs: loads saved tokens and auto-refreshes
client = Garmin(
    os.getenv("EMAIL"),
    os.getenv("PASSWORD"),
    prompt_mfa=lambda: input("MFA code: "),
)
client.login("~/.garminconnect")

# Get today's stats
today = date.today().isoformat()
stats = client.get_stats(today)
activities = client.get_activities(0, 1)
if isinstance(activities, list) and activities:
    activity = activities[0]
    distance = activity["distance"]
    duration = activity["duration"]
    startTimeLocal = activity["startTimeLocal"]
    print(f"startTimeLocal:{startTimeLocal},distance:{distance},duration:{duration}")
