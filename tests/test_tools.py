"""Tools against a stubbed Garmin API: wiring plus the projections."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from garmin_mcp.server import build_server

ACTIVITY_ROW = {
    "activityId": 4242, "activityName": "Morning Run",
    "startTimeLocal": "2026-08-20 08:30:00", "activityType": {"typeKey": "running"},
    "distance": 12345.6, "duration": 3600.0, "movingDuration": 3500.0,
    "averageHR": 142.4, "maxHR": 171.0, "calories": 812.0, "elevationGain": 118.0,
    "averageSpeed": 3.43, "ownerId": 7, "privacy": {"typeKey": "private"},
}

ACTIVITY_DETAIL = {
    "activityId": 4242, "activityName": "Morning Run", "description": "easy",
    "activityTypeDTO": {"typeKey": "running"},
    "eventTypeDTO": {"typeKey": "training"},
    "summaryDTO": {"startTimeLocal": "2026-08-20 08:30:00", "distance": 12345.6,
                   "duration": 3600.0, "movingDuration": 3500.0, "averageHR": 142.4,
                   "maxHR": 171, "averagePower": 244, "normPower": 251,
                   "elevationGain": 118, "calories": 812, "trainingEffect": 3.4},
    "metadataDTO": {"elevationCorrected": False},
    "gear": [{"displayName": "Nimbus 26"}],
    "summarizedDiveInfo": {"summarizedDiveGases": []},
}


def _ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _gmt(minute):
    """Garmin's GMT string for a minute counted from 20:00 GMT on 2026-08-19."""
    t = datetime(2026, 8, 19, 20, 0) + timedelta(minutes=minute)
    return t.isoformat() + ".0"


def _gmt_ms(minute):
    """The same minute as epoch milliseconds, Garmin's other time format."""
    return _ms(_gmt(minute)[:-2])


# One night in CEST (UTC+2): asleep 21:00-04:30 GMT, i.e. 23:00-06:30 local.
# Movement runs a minute at a time from an hour before to an hour after;
# heart rate every two minutes, stress every three, SpO2 only for half the night.
SLEEP_FULL = {
    "dailySleepDTO": {
        "sleepTimeSeconds": 27000, "deepSleepSeconds": 4800,
        "lightSleepSeconds": 15000, "remSleepSeconds": 6000, "awakeSleepSeconds": 1200,
        "sleepStartTimestampGMT": _ms("2026-08-19T21:00:00"),
        "sleepStartTimestampLocal": _ms("2026-08-19T23:00:00"),
        "sleepEndTimestampGMT": _ms("2026-08-20T04:30:00"),
        "avgHeartRate": 55.0,
        "sleepScores": {"overall": {"value": 78, "qualifierKey": "GOOD"},
                        "restlessness": {"qualifierKey": "FAIR"}}},
    "sleepMovement": [{"startGMT": _gmt(m), "endGMT": _gmt(m + 1),
                       "activityLevel": 6.5 if m == 215 else 0.5}
                      for m in range(570)],
    "sleepLevels": [
        {"startGMT": _gmt(60), "endGMT": _gmt(120), "activityLevel": 1.0},
        {"startGMT": _gmt(120), "endGMT": _gmt(150), "activityLevel": 1.0},
        {"startGMT": _gmt(150), "endGMT": _gmt(210), "activityLevel": 0.0},
        {"startGMT": _gmt(210), "endGMT": _gmt(220), "activityLevel": 3.0},
        {"startGMT": _gmt(220), "endGMT": _gmt(510), "activityLevel": 2.0}],
    "sleepRestlessMoments": [{"value": 1, "startGMT": _ms("2026-08-19T23:35:00")}],
    "restlessMomentsCount": 1,
    "sleepHeartRate": [{"value": 50 if m < 300 else 60, "startGMT": _gmt_ms(m)}
                       for m in range(60, 510, 2)],
    "sleepStress": [{"value": -1 if m == 63 else 20, "startGMT": _gmt_ms(m)}
                    for m in range(60, 510, 3)],
    "wellnessEpochSPO2DataDTOList": [{"epochTimestamp": _gmt(m), "spo2Reading": 95,
                                      "readingConfidence": 2} for m in range(60, 300)],
    "restingHeartRate": 49, "avgOvernightHrv": 25.0, "hrvStatus": "BALANCED",
    "skinTempDataExists": True, "avgSkinTempDeviationC": 0.0,
}


CHALLENGE = {
    "uuid": "RUN1", "adHocChallengeName": "Lauf-Challenge",
    "socialChallengeActivityTypeId": 1, "startDate": "2026-07-01T00:00:00.0",
    "endDate": "2026-07-31T23:59:59.0", "userRanking": 3, "playerCount": 3,
    "players": [
        {"ranking": 2, "fullName": "Second Place", "totalNumber": 175048.3,
         "displayName": "other-guid", "lastSyncTime": "2026-07-31T15:16:26.0"},
        {"ranking": 1, "fullName": "First Place", "totalNumber": 210389.5,
         "displayName": "another-guid", "lastSyncTime": "2026-08-01T11:56:08.0"},
        {"ranking": 3, "fullName": "Test Athlete", "totalNumber": 173547.0,
         "displayName": "display-guid", "lastSyncTime": "2026-08-01T09:00:00.0"},
    ],
}


class FakeClient:
    def __init__(self, fit: bytes) -> None:
        self._fit = fit
        self.calls = []

    async def search_activities(self, **kw):
        self.calls.append(("search", kw))
        return [ACTIVITY_ROW]

    async def get_activity_detail(self, activity_id):
        return ACTIVITY_DETAIL

    async def activity_time_in_zones(self, activity_id, kind="hr"):
        if kind != "hr":
            return []                      # no power meter on this activity
        return [{"zoneNumber": 1, "secsInZone": 600.0, "zoneLowBoundary": 80},
                {"zoneNumber": 2, "secsInZone": 0.0, "zoneLowBoundary": 96}]

    async def activity_weather(self, activity_id):
        return {"temp": 59, "relativeHumidity": 94, "windSpeed": 10,
                "weatherTypeDTO": {"desc": "Showers"}}

    async def download_original_fit(self, activity_id):
        return self._fit if activity_id == 4242 else None

    async def get_daily_summary(self, display, day):
        return {"totalSteps": 9123, "restingHeartRate": 44, "averageStressLevel": 28,
                "bodyBatteryHighestValue": 92, "bodyBatteryLowestValue": 21,
                "totalKilocalories": 2890, "userProfileId": 7}

    async def get_sleep_full(self, display, day):
        # Garmin answers a night without sleep with an empty DTO.
        return SLEEP_FULL if day == "2026-08-20" else {"dailySleepDTO": {}}

    async def get_hrv_summary(self, day):
        return {"lastNightAvg": 61, "status": "BALANCED",
                "baseline": {"lowUpper": 48, "balancedUpper": 72}}

    async def get_training_readiness(self, day):
        return [{"score": 71, "level": "READY", "inputContext": "AFTER_WAKEUP_RESET",
                 "feedbackShort": "GOOD", "sleepScore": 78, "recoveryTime": 240}]

    async def get_training_status(self, day):
        return {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 54.2}}}

    async def get_body_composition(self, start, end):
        return [{"calendarDate": "2026-08-20", "weight": 74300, "bodyFat": 12.4,
                 "muscleMass": 33100}]

    async def get_blood_pressure(self, start, end):
        return {"measurementSummaries": [{"measurements": [
            {"measurementTimestampLocal": "2026-08-20T07:00:00.0",
             "systolic": 118, "diastolic": 74, "pulse": 52}]}]}

    async def list_gear(self, statuses="ACTIVE"):
        if statuses == "RETIRED":
            return [{"uuid": "u0", "name": "Old Kayano", "gearType": "SHOES",
                     "status": "RETIRED", "distanceUsedMeters": 900000,
                     "numActivitiesLinked": 120}]
        return [{"uuid": "u1", "name": "Nimbus 26", "gearType": "SHOES",
                 "brand": "Asics", "status": "ACTIVE", "distanceUsedMeters": 412000,
                 "maxUsageDistanceMeters": 800000, "numActivitiesLinked": 47,
                 "durationUsedSeconds": 133200, "daysUsed": 41,
                 "firstUseDate": "2026-01-14"}]

    async def user_settings(self):
        return {"birthDate": "1975-01-01", "gender": "MALE", "weight": 74300,
                "height": 183}

    async def personal_information(self):
        return {"vo2Max": 54.2, "functionalThresholdPower": 265,
                "criticalSwimSpeed": 1180}

    async def heart_rate_zones(self):
        return [{"sport": "RUNNING", "maxHeartRateUsed": 186, "zone1Floor": 110,
                 "zone2Floor": 130, "zone3Floor": 145, "zone4Floor": 160,
                 "zone5Floor": 172}]

    async def fetch_display_name(self):
        return "Test Athlete"

    async def list_adhoc_challenges(self):
        return [CHALLENGE, {**CHALLENGE, "uuid": "BIKE1",
                            "adHocChallengeName": "Radfahr-Challenge",
                            "socialChallengeActivityTypeId": 2}]

    async def get_adhoc_challenge(self, uuid):
        return CHALLENGE


class FakeSession:
    def __init__(self, fit: bytes) -> None:
        self._client = FakeClient(fit)
        self.client_stub = self._client
        self.account = "Test Athlete"

    async def client(self):
        return self._client

    async def display_id(self):
        return "display-guid"

    async def fit_bytes(self, activity_id):
        return await self._client.download_original_fit(activity_id)


@pytest.fixture
def session(fit_bytes):
    return FakeSession(fit_bytes)


@pytest.fixture
def server(session):
    return build_server(lambda: session)


def _blocks(server, name, args):
    res = asyncio.run(server.call_tool(name, args))
    return [json.loads(c.text) for c in res.content]


def call(server, name, **args):
    """A tool returning one object."""
    blocks = _blocks(server, name, args)
    assert len(blocks) == 1
    return blocks[0]


def call_list(server, name, **args):
    """A tool returning a list - one content block per item."""
    return _blocks(server, name, args)


def test_all_tools_are_registered(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "list_activities", "get_activity", "analyze_activity_fit",
        "get_activity_streams", "get_swim_detail", "get_activity_sensors",
        "get_daily_health", "get_sleep_detail", "get_training_status", "get_body_composition",
        "get_blood_pressure", "list_challenges", "get_challenge", "list_gear",
        "get_profile", "whoami", "get_health_trend", "get_calendar",
        "list_planned_workouts", "get_fitness_metrics", "list_personal_records"}


def test_every_tool_has_a_description(server):
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and len(tool.description) > 60, tool.name


def test_list_activities_projects_the_row(server):
    rows = call_list(server, "list_activities", sport="running")
    assert rows == [{"activity_id": 4242, "name": "Morning Run",
                     "start_local": "2026-08-20 08:30:00", "sport": "running",
                     "distance_km": 12.346, "duration_min": 60.0,
                     "moving_min": 58.3, "avg_hr": 142, "max_hr": 171,
                     "avg_speed_ms": 3.43, "elevation_gain_m": 118,
                     "calories": 812}]


def test_list_activities_caps_the_limit(server, session):
    call_list(server, "list_activities", limit=5000)
    assert session.client_stub.calls[-1][1]["limit"] == 100


def test_list_activities_passes_filters_through(server, session):
    call_list(server, "list_activities", date_from="2026-08-01",
              date_to="2026-08-20", sport="cycling", limit=7)
    _, kw = session.client_stub.calls[-1]
    assert kw == {"limit": 7, "start_date": "2026-08-01", "end_date": "2026-08-20",
                  "activity_type": "cycling"}


def test_get_activity_drops_garmin_noise(server):
    a = call(server, "get_activity", activity_id=4242)
    assert a["name"] == "Morning Run" and a["gear"] == ["Nimbus 26"]
    assert a["normalized_power"] == 251
    assert "summarizedDiveInfo" not in a and "metadataDTO" not in a


def test_get_activity_adds_zones_and_weather(server):
    a = call(server, "get_activity", activity_id=4242)
    assert a["hr_zones"] == [{"zone": 1, "from_bpm": 80, "minutes": 10.0}]
    assert a["weather"]["temperature_c"] == 15.0
    # An activity without a power meter must not carry an empty key.
    assert "power_zones" not in a


def test_analyze_activity_fit_reads_the_file(server):
    m = call(server, "analyze_activity_fit", activity_id=4242)
    assert m["sport"] == "running" and m["avg_hr"] == 139


def test_missing_fit_is_a_clear_error(server):
    with pytest.raises(Exception) as exc:
        call(server, "analyze_activity_fit", activity_id=999)
    assert "no FIT original" in str(exc.value.__cause__)


def test_streams_stay_small(server):
    view = call(server, "get_activity_streams", activity_id=4242, max_points=30)
    assert view["n_points"] == 30
    assert len(json.dumps(view)) < 20_000


def test_daily_health_merges_four_endpoints(server):
    d = call(server, "get_daily_health", day="2026-08-20")
    assert d["steps"] == 9123 and d["resting_hr"] == 44
    assert d["sleep"]["score"] == 78 and d["sleep"]["deep_min"] == 80.0
    assert d["hrv_last_night"] == 61 and d["hrv_status"] == "BALANCED"
    assert d["training_readiness"]["score"] == 71
    assert d["training_readiness"]["is_morning_value"] is True
    assert d["sleep"]["restless_moments"] == 1 and d["sleep"]["restlessness"] == "FAIR"
    assert "phases" not in d["sleep"]           # the detail stays in get_sleep_detail


def test_sleep_detail_is_in_the_local_time_of_that_night(server):
    d = call(server, "get_sleep_detail", day="2026-08-20")
    assert d["sleep_start"] == "2026-08-19T23:00" and d["sleep_end"] == "2026-08-20T06:30"
    assert d["restless_at"] == ["2026-08-20T01:35"]
    assert d["summary"]["score"] == 78


def test_sleep_phases_are_merged_where_garmin_splits_them(server):
    phases = call(server, "get_sleep_detail", day="2026-08-20")["phases"]
    assert [(p["phase"], p["start"], p["min"]) for p in phases] == [
        ("light", "2026-08-19T23:00", 90), ("deep", "2026-08-20T00:30", 60),
        ("awake", "2026-08-20T01:30", 10), ("rem", "2026-08-20T01:40", 290)]


def test_sleep_series_share_one_time_grid(server):
    d = call(server, "get_sleep_detail", day="2026-08-20", max_points=57)
    g = d["series"]
    assert g["bucket_min"] == 10 and len(g["t"]) == 57
    assert g["t"][0] == "2026-08-19T22:00" and g["t"][6] == "2026-08-19T23:00"
    assert all(len(g[k]) == 57 for k in ("movement", "movement_peak", "heart_rate",
                                         "stress", "spo2"))
    # the hour before sleep has movement only
    assert g["movement"][0] == 0.5 and g["heart_rate"][0] is None
    assert g["heart_rate"][6] == 50 and g["heart_rate"][30] == 60
    assert g["spo2"][29] == 95 and g["spo2"][30] is None


def test_sleep_movement_keeps_the_peak_of_each_stretch(server):
    d = call(server, "get_sleep_detail", day="2026-08-20", max_points=57)
    assert d["series"]["movement"][21] == 1.1 and d["series"]["movement_peak"][21] == 6.5
    assert d["series_stats"]["movement"]["max"] == 6.5


def test_unmeasured_markers_do_not_count(server):
    stats = call(server, "get_sleep_detail", day="2026-08-20")["series_stats"]
    assert stats["stress"]["min"] == 20                 # the -1 is not a value


def test_sleep_detail_carries_the_night_averages(server):
    d = call(server, "get_sleep_detail", day="2026-08-20")
    assert d["avg_hr"] == 55 and d["resting_hr"] == 49 and d["avg_hrv"] == 25.0
    assert d["skin_temp_deviation_c"] == 0.0            # a real zero is kept
    assert "series" in d and len(json.dumps(d)) < 12_000


def test_sleep_detail_explains_a_night_without_sleep(server):
    with pytest.raises(Exception, match="morning you woke up"):
        call(server, "get_sleep_detail", day="2026-08-01")


def test_body_composition_is_in_kilograms(server):
    rows = call_list(server, "get_body_composition", date_from="2026-08-01",
                     date_to="2026-08-20")
    assert rows[0]["weight_kg"] == 74.3 and rows[0]["muscle_mass_kg"] == 33.1


def test_blood_pressure_is_flattened(server):
    rows = call_list(server, "get_blood_pressure", date_from="2026-08-01",
                     date_to="2026-08-20")
    assert rows == [{"measured_at": "2026-08-20T07:00:00.0", "systolic": 118,
                     "diastolic": 74, "pulse": 52}]


def test_profile_converts_units(server):
    p = call(server, "get_profile")
    assert p["weight_kg"] == 74.3
    assert p["critical_swim_speed_ms"] == 1.18
    assert p["hr_zones"][0]["floors"] == [110, 130, 145, 160, 172]


def test_gear_reports_how_much_it_has_been_used(server):
    g = call_list(server, "list_gear")
    assert g[0] == {"uuid": "u1", "name": "Nimbus 26", "type": "SHOES",
                    "brand": "Asics", "status": "ACTIVE", "activities": 47,
                    "used_km": 412.0, "used_hours": 37.0, "days_used": 41,
                    "first_use": "2026-01-14", "limit_km": 800.0,
                    "pct_of_limit": 51.5}


def test_retired_gear_is_off_by_default(server):
    assert [g["name"] for g in call_list(server, "list_gear")] == ["Nimbus 26"]
    both = call_list(server, "list_gear", include_retired=True)
    assert [g["name"] for g in both] == ["Nimbus 26", "Old Kayano"]
    assert both[1]["status"] == "RETIRED" and "pct_of_limit" not in both[1]


def test_bad_day_is_rejected(server):
    with pytest.raises(Exception):
        call(server, "get_daily_health", day="20th of August")


def test_list_challenges_filters_by_sport(server):
    rows = call_list(server, "list_challenges", sport="cycling")
    assert [r["name"] for r in rows] == ["Radfahr-Challenge"]


def test_challenge_leaderboard_is_sorted_and_marks_you(server):
    d = call(server, "get_challenge", challenge_id="RUN1")
    assert d["sport"] == "running" and d["start"] == "2026-07-01"
    assert [p["rank"] for p in d["leaderboard"]] == [1, 2, 3]
    assert d["leaderboard"][0] == {"rank": 1, "name": "First Place",
                                   "total_km": 210.389, "last_sync": "2026-08-01"}
    you = [p for p in d["leaderboard"] if p.get("is_you")]
    assert len(you) == 1 and you[0]["name"] == "Test Athlete"
