"""Trends, plan, fitness metrics and the per-activity context."""

from garmin_mcp import project


class Client:
    """Only the methods the new tools touch."""

    def __init__(self):
        self.calendar_calls = []

    async def steps_range(self, start, end):
        return [{"calendarDate": "2026-08-20", "totalSteps": 11429,
                 "totalDistance": 9099, "stepGoal": 10000},
                {"calendarDate": "2026-08-21", "totalSteps": 16416,
                 "totalDistance": 14831, "stepGoal": 10000}]

    async def body_battery_range(self, start, end):
        return [{"date": "2026-08-20", "charged": 52, "drained": 47,
                 "bodyBatteryValuesArray": [[1, 2, 3]] * 1000},
                {"date": "2026-08-21", "charged": 48, "drained": 50,
                 "bodyBatteryValuesArray": [[1, 2, 3]] * 1000}]

    async def vo2max_range(self, start, end):
        return [{"generic": {"calendarDate": "2026-08-21",
                             "vo2MaxPreciseValue": 50.9},
                 "cycling": None}]

    async def calendar_month(self, year, month):
        self.calendar_calls.append((year, month))
        return {
            (2026, 8): [
                {"itemType": "event", "id": 1, "title": "IRONMAN Vichy",
                 "date": "2026-08-23", "isRace": True,
                 "eventTimeLocal": {"startTimeHhMm": "07:00",
                                    "timeZoneId": "Europe/Paris"},
                 "completionTarget": {"value": 226.0, "unit": "kilometer"}},
                {"itemType": "workout", "workoutId": 77, "title": "4x1000m",
                 "date": "2026-08-05", "sportTypeKey": "running",
                 "duration": 3600, "trainingPlanId": 42},
                {"itemType": "activity", "id": 9, "title": "Morning Run",
                 "date": "2026-08-06"},
                {"itemType": "weight", "id": 10, "date": "2026-08-07"},
            ],
            (2026, 9): [
                {"itemType": "event", "id": 2, "title": "Marathon",
                 "date": "2026-09-27", "isRace": True},
            ],
        }.get((year, month), [])


def test_health_trend_merges_three_endpoints_by_day():
    c = Client()
    import asyncio
    rows = project.health_trend(
        asyncio.run(c.steps_range("a", "b")),
        asyncio.run(c.body_battery_range("a", "b")),
        asyncio.run(c.vo2max_range("a", "b")))
    assert [r["day"] for r in rows] == ["2026-08-20", "2026-08-21"]
    assert rows[0]["steps"] == 11429 and rows[0]["body_battery_charged"] == 52
    assert rows[1]["vo2max_running"] == 50.9


def test_health_trend_drops_the_intraday_curve():
    """A month of Body Battery curves is tens of thousands of numbers."""
    import asyncio
    c = Client()
    rows = project.health_trend([], asyncio.run(c.body_battery_range("a", "b")), [])
    assert all("bodyBatteryValuesArray" not in str(r) for r in rows)
    assert len(str(rows)) < 300


def test_calendar_keeps_plan_and_races_and_drops_duplicates_of_other_tools():
    items = [project.calendar_item(i) for i in [
        {"itemType": "workout", "workoutId": 77, "title": "4x1000m",
         "date": "2026-08-05", "sportTypeKey": "running", "duration": 3600},
        {"itemType": "event", "id": 1, "title": "Race", "date": "2026-08-23",
         "isRace": True, "completionTarget": {"value": 226.0, "unit": "kilometer"}},
    ]]
    assert items[0]["type"] == "workout" and items[0]["planned_duration_min"] == 60.0
    assert items[1]["target"] == "226.0 kilometer" and items[1]["is_race"] is True
    assert project.CALENDAR_TYPES == ("workout", "event")
    assert "activity" not in project.CALENDAR_TYPES     # list_activities covers those


def test_personal_records_label_the_known_types_and_keep_the_rest():
    rows = project.personal_records([
        {"typeId": 5, "value": 5407.0, "activityType": "running",
         "actStartDateTimeInGMTFormatted": "2022-04-03T08:00:00.0",
         "activityId": 1, "activityName": "Berlin"},
        {"typeId": 7, "value": 42999.5, "activityType": "running",
         "actStartDateTimeInGMTFormatted": "2025-06-29T06:00:00.0",
         "activityId": 2, "activityName": "Frankfurt"},
        {"typeId": 99, "value": 814.0, "activityType": "lap_swimming",
         "actStartDateTimeInGMTFormatted": "2023-03-18T10:00:00.0",
         "activityId": 3, "activityName": "Pool"},
    ])
    assert rows[0]["record"] == "half marathon" and rows[0]["time"] == "1:30:07"
    assert rows[1]["distance_km"] == 42.999
    unknown = rows[2]
    assert "record" not in unknown and unknown["value"] == 814.0   # kept, not guessed


def test_fitness_metrics_formats_predictions_and_totals():
    view = project.fitness_metrics(
        {"time5K": 1500, "time10K": 3150, "timeHalfMarathon": 7000,
         "timeMarathon": 15000},
        {"chronologicalAge": 45, "fitnessAge": 40.95, "achievableFitnessAge": 39.69},
        {"overallScore": 7479}, {"overallScore": 38, "strengthScore": 7,
                                 "enduranceScore": 15, "vo2MaxPreciseValue": 50.9},
        {"userMetrics": [{"totalActivities": 1234, "totalDistance": 23456789.6,
                          "totalDuration": 4567890.5,
                          "totalElevationGain": 98765.3}]})
    assert view["race_predictions"] == {"5k": "25:00", "10k": "52:30",
                                        "half_marathon": "1:56:40",
                                        "marathon": "4:10:00"}
    assert view["fitness_age"] == 41.0 and view["chronological_age"] == 45
    assert view["lifetime"]["activities"] == 1234
    assert view["lifetime"]["distance_km"] == 23456.79


def test_zones_only_report_zones_with_time_in_them():
    zones = [{"zoneNumber": 1, "secsInZone": 250.9, "zoneLowBoundary": 80},
             {"zoneNumber": 2, "secsInZone": 0.0, "zoneLowBoundary": 96}]
    assert project.time_in_zones(zones, "bpm") == [
        {"zone": 1, "from_bpm": 80, "minutes": 4.2}]
    assert project.time_in_zones([], "watt") == []


def test_weather_is_converted_from_garmins_imperial_units():
    view = project.weather({"temp": 59, "apparentTemp": 59, "dewPoint": 57,
                            "relativeHumidity": 94, "windSpeed": 10,
                            "windDirectionCompassPoint": "n",
                            "weatherTypeDTO": {"desc": "Showers"}})
    assert view["temperature_c"] == 15.0
    assert view["wind_kmh"] == 16.1
    assert view["conditions"] == "Showers"


def test_planned_workout_hides_garmins_zero_estimates():
    view = project.planned_workout(
        {"workoutId": 7, "workoutName": "LIT", "sportType": {"sportTypeKey": "running"},
         "estimatedDurationInSecs": 0, "estimatedDistanceInMeters": 0})
    assert "estimated_duration_min" not in view
    assert "estimated_distance_km" not in view
