"""The MCP tools. Read-only: nothing here writes to Garmin Connect.

Docstrings are the tool descriptions the model sees, so they say when to reach
for which tool - especially where Garmin's own numbers (get_activity) and the
device's file (analyze_activity_fit) can disagree.
"""

from __future__ import annotations

import functools
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import fitview, project
from .garmin_api.client import GarminError
from .session import NotConnected, current_session, today

MAX_LIMIT = 100


def _guard(fn):
    """Turn our own failures into ToolError, which the SDK passes through to
    the client. Without this the model only ever sees "Error executing tool
    <name>" - and the most common failure here (no tokens yet) is exactly the
    one the user needs to read."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (NotConnected, GarminError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def _day(value: str | None) -> str:
    """Validate a YYYY-MM-DD day, defaulting to today."""
    if not value:
        return today().isoformat()
    return date.fromisoformat(value).isoformat()


async def _fit(session, activity_id: int) -> bytes:
    data = await session.fit_bytes(activity_id)
    if not data:
        raise ValueError(
            f"activity {activity_id} has no FIT original (imported from GPX/TCX?)")
    return data


def register(server: MCPServer, get_session=current_session) -> MCPServer:
    """Tools resolve their session per call: over HTTP that is the account
    behind the bearer token. `S()` is that lookup -
    binding a session at registration time would make the whole server
    single-tenant again."""
    S = get_session

    # ---------------------------------------------------------- activities

    @server.tool()
    @_guard
    async def list_activities(date_from: str | None = None,
                              date_to: str | None = None,
                              sport: str | None = None,
                              limit: int = 20) -> list[dict]:
        """List activities from Garmin Connect, newest first.

        date_from/date_to are local calendar days ("YYYY-MM-DD"). sport is a
        Garmin type key such as "running", "cycling", "lap_swimming",
        "open_water_swimming", "strength_training", "multi_sport". limit is
        capped at 100. Returns one compact row per activity; use get_activity
        for the full record and analyze_activity_fit for what the device
        actually recorded."""
        c = await S().client()
        rows = await c.search_activities(
            limit=max(1, min(int(limit), MAX_LIMIT)),
            start_date=date_from, end_date=date_to, activity_type=sport)
        return [project.activity_summary(a) for a in rows]

    @server.tool()
    @_guard
    async def get_activity(activity_id: int) -> dict:
        """Garmin Connect's own record of one activity: name, description,
        sport, times, distance, heart rate, power, elevation, training effect
        and assigned gear.

        Includes time per heart rate and power zone and the weather at the
        time, where the activity has them.

        These are the values Garmin computed server-side. Where elevation or
        pause handling is in question, compare with analyze_activity_fit,
        which reads the file the watch wrote."""
        c = await S().client()
        activity_id = int(activity_id)
        view = project.activity_detail(await c.get_activity_detail(activity_id))
        # Best effort: an activity without a heart rate belt or power meter
        # simply has no zones, and that must not fail the whole call.
        hr = project.time_in_zones(
            await c.activity_time_in_zones(activity_id, "hr"), "bpm")
        power = project.time_in_zones(
            await c.activity_time_in_zones(activity_id, "power"), "watt")
        weather = project.weather(await c.activity_weather(activity_id))
        return project.compact({**view, "hr_zones": hr, "power_zones": power,
                                "weather": weather})

    # ---------------------------------------------------------- FIT analysis

    @server.tool()
    @_guard
    async def analyze_activity_fit(activity_id: int) -> dict:
        """Download the original FIT of an activity and report the metrics as
        the device recorded them: sport/sub-sport, moving and elapsed time,
        distance, average and max heart rate, power, cadence, elevation,
        device model, and for multisport files the individual legs.

        Use this when Garmin's own numbers look wrong or when the question is
        about what the watch actually measured. The file is cached, so a
        follow-up get_activity_streams for the same activity is cheap."""
        return fitview.metrics(await _fit(S(), int(activity_id)))

    @server.tool()
    @_guard
    async def get_activity_streams(activity_id: int,
                                   channels: list[str] | None = None,
                                   max_points: int = 120,
                                   include_gps: bool = False) -> dict:
        """Time series of an activity from its FIT file: heart rate, power,
        speed, cadence, altitude, temperature and any Connect-IQ channel the
        file carries (Stryd, SmO2, CORE temperature).

        The series is resampled to max_points (default 120, maximum 500) and
        every channel reports min/max/avg, so the result stays readable.
        Call it once without `channels` to see available_channels, then again
        with the ones you need. include_gps adds the route polyline."""
        data = await _fit(S(), int(activity_id))
        return fitview.stream_view(data, channels=channels,
                                   max_points=max_points, include_gps=include_gps)

    @server.tool()
    @_guard
    async def get_swim_detail(activity_id: int) -> dict:
        """Pool swim detail from the FIT: pool length, every single length
        with stroke, time and pace per 100 m, plus the active intervals with
        stroke count. Only meaningful for lap_swimming activities."""
        return fitview.swim_view(await _fit(S(), int(activity_id)))

    @server.tool()
    @_guard
    async def get_activity_sensors(activity_id: int) -> list[dict]:
        """Which Connect-IQ / developer fields the FIT of an activity carries,
        grouped by the app that wrote them, with a sample value and a flag for
        fields that stayed empty or constant - that is how you tell whether an
        external sensor (Stryd, Moxy/SmO2, CORE) was actually connected."""
        return fitview.sensors(await _fit(S(), int(activity_id)))

    # ---------------------------------------------------------- trends

    @server.tool()
    @_guard
    async def get_health_trend(date_from: str, date_to: str) -> list[dict]:
        """Daily health values over a whole period in one call: steps and step
        goal, distance, Body Battery charged and drained, and VO2max for
        running and cycling.

        Use this instead of calling get_daily_health once per day - a month
        costs three requests here and thirty there. For sleep, HRV, stress and
        training readiness of a single day, get_daily_health is still the
        right tool."""
        c = await S().client()
        start, end = _day(date_from), _day(date_to)
        return project.health_trend(
            await c.steps_range(start, end),
            await c.body_battery_range(start, end),
            await c.vo2max_range(start, end))

    # ---------------------------------------------------------- plan

    @server.tool()
    @_guard
    async def get_calendar(date_from: str, date_to: str) -> list[dict]:
        """What was planned in a period: scheduled workouts from a training
        plan and events such as races, with date, sport, planned duration or
        distance and the target.

        This is the plan, not the record - what actually happened is in
        list_activities. Accounts without a training plan will only see
        events, and accounts with neither get an empty list."""
        c = await S().client()
        start, end = date.fromisoformat(_day(date_from)), date.fromisoformat(_day(date_to))
        if end < start:
            raise ValueError("date_to is before date_from")
        if (end - start).days > 370:
            raise ValueError("range too long, ask for at most a year")
        seen: dict = {}
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            for item in await c.calendar_month(year, month):
                if item.get("itemType") not in project.CALENDAR_TYPES:
                    continue
                day = item.get("date") or ""
                if start.isoformat() <= day <= end.isoformat():
                    seen[f"{item.get('itemType')}:{item.get('id')}:{day}"] = item
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return sorted((project.calendar_item(i) for i in seen.values()),
                      key=lambda i: i.get("date") or "")

    @server.tool()
    @_guard
    async def list_planned_workouts(limit: int = 20) -> list[dict]:
        """The structured workouts stored in this Garmin account: name, sport,
        the written description and any estimated duration or distance.

        These are the workout definitions, not their scheduling - use
        get_calendar to see when they are due."""
        c = await S().client()
        rows = await c.planned_workouts(max(1, min(int(limit), MAX_LIMIT)))
        return [project.planned_workout(w) for w in rows]

    # ---------------------------------------------------------- fitness

    @server.tool()
    @_guard
    async def get_fitness_metrics(day: str | None = None) -> dict:
        """Garmin's summary judgements about your fitness: race time
        predictions for 5k, 10k, half and full marathon, fitness age against
        your real age, endurance score, hill score with its strength and
        endurance parts, VO2max, and your lifetime totals.

        day defaults to today; the scores are computed per day."""
        c = await S().client()
        when = _day(day)
        return project.fitness_metrics(
            await c.race_predictions(), await c.fitness_age(when),
            await c.endurance_score(when), await c.hill_score(when),
            await c.lifetime_totals())

    @server.tool()
    @_guard
    async def list_personal_records() -> list[dict]:
        """All-time personal records with the activity that set them: fastest
        1 km, mile, 5 km, 10 km, half marathon and marathon, longest run and
        ride, biggest climb.

        Records Garmin identifies by a type this connector does not know are
        returned with their raw value rather than dropped."""
        c = await S().client()
        return project.personal_records(await c.personal_records())

    # ---------------------------------------------------------- health

    @server.tool()
    @_guard
    async def get_daily_health(day: str | None = None) -> dict:
        """Everything Garmin knows about one day: steps, calories, resting
        heart rate, stress, Body Battery, respiration, intensity minutes, plus
        the night's sleep (phases, score), HRV status and training readiness.

        day is "YYYY-MM-DD" and defaults to today. Sleep is filed under the
        morning you woke up."""
        d = _day(day)
        c = await S().client()
        display = await S().display_id()
        summary = await c.get_daily_summary(display, d)
        sleep_full = await c.get_sleep_full(display, d)
        dto = (sleep_full or {}).get("dailySleepDTO") or {}
        hrv = await c.get_hrv_summary(d)
        readiness = await c.get_training_readiness(d)
        return project.compact({
            "day": d,
            **project.daily_summary(summary),
            "sleep": project.sleep_summary(dto, dto.get("sleepScores"),
                                           sleep_full.get("restlessMomentsCount")),
            "hrv_last_night": hrv.get("lastNightAvg"),
            "hrv_status": hrv.get("status"),
            "hrv_baseline_low": (hrv.get("baseline") or {}).get("lowUpper"),
            "hrv_baseline_high": (hrv.get("baseline") or {}).get("balancedUpper"),
            "training_readiness": project.training_readiness(readiness),
        })

    @server.tool()
    @_guard
    async def get_sleep_detail(day: str | None = None, max_points: int = 60) -> dict:
        """One night in detail: the sequence of sleep phases (deep, light,
        rem, awake) with start, end and minutes, the moments Garmin counted as
        restless (it lists a few fewer than summary.restless_moments counts),
        the night's averages, and how movement, heart rate, HRV, stress, Body
        Battery, SpO2 and respiration went through the night.

        The series share one time grid in `series`: `t` holds the start of
        each stretch (bucket_min minutes long, at most max_points of them,
        default 60, maximum 500) and every other column the average over that
        stretch, null where nothing was measured - row i of every column is
        the same stretch. movement_peak keeps the strongest movement of each
        stretch. Movement is Garmin's activity level from the wrist sensor:
        relative, no unit, higher means more movement. series_stats has min,
        max and avg per series. The grid may begin before sleep_start and run
        past sleep_end; those two mark the sleep itself. All times are the
        local time of that night.

        day is "YYYY-MM-DD", defaults to today, and like all sleep is the
        morning you woke up. For the night's totals and score alone,
        get_daily_health is enough."""
        d = _day(day)
        c = await S().client()
        full = await c.get_sleep_full(await S().display_id(), d)
        if not (full.get("dailySleepDTO") or {}).get("sleepStartTimestampGMT"):
            raise ValueError(f"Garmin has no sleep for {d} - sleep is filed under "
                             "the morning you woke up")
        return {"day": d, **project.sleep_detail(full, max_points)}

    @server.tool()
    @_guard
    async def get_training_status(day: str | None = None) -> dict:
        """Garmin's aggregated training status for a day: the status phrase
        (PRODUCTIVE, MAINTAINING, OVERREACHING …), acute load and load ratio,
        the load focus split (low aerobic / high aerobic / anaerobic) and
        VO2max for running and cycling."""
        c = await S().client()
        return project.training_status(await c.get_training_status(_day(day)))

    @server.tool()
    @_guard
    async def get_body_composition(date_from: str, date_to: str) -> list[dict]:
        """Weight and body composition per day in a date range
        ("YYYY-MM-DD"). Weight and muscle mass come back in kilograms."""
        c = await S().client()
        rows = await c.get_body_composition(_day(date_from), _day(date_to))
        return [project.compact({
            "day": r.get("calendarDate"),
            "weight_kg": round(r["weight"] / 1000, 2) if r.get("weight") else None,
            "body_fat_pct": r.get("bodyFat"),
            "muscle_mass_kg": (round(r["muscleMass"] / 1000, 2)
                               if r.get("muscleMass") else None),
            "bmi": r.get("bmi"),
            "source": r.get("sourceType"),
        }) for r in rows]

    @server.tool()
    @_guard
    async def get_blood_pressure(date_from: str, date_to: str) -> list[dict]:
        """Blood pressure measurements in a date range ("YYYY-MM-DD"):
        systolic, diastolic, pulse and the measurement timestamp."""
        c = await S().client()
        raw = await c.get_blood_pressure(_day(date_from), _day(date_to))
        out = []
        for summary in (raw.get("measurementSummaries") or []):
            for m in (summary.get("measurements") or []):
                out.append(project.compact({
                    "measured_at": m.get("measurementTimestampLocal"),
                    "systolic": m.get("systolic"),
                    "diastolic": m.get("diastolic"),
                    "pulse": m.get("pulse"),
                    "note": m.get("notes"),
                }))
        return out

    # ---------------------------------------------------------- challenges

    @server.tool()
    @_guard
    async def list_challenges(sport: str | None = None, limit: int = 10) -> list[dict]:
        """Social challenges against friends ("Lauf-Challenge" and the like),
        newest first, including the one running right now: name, period, state
        (running/finished) and where you ranked.

        sport filters on "running", "cycling" or "swimming". A running
        challenge reports no player count here - take its challenge_id to
        get_challenge, which returns the current table."""
        c = await S().client()
        rows = [project.challenge_summary(x) for x in await c.list_adhoc_challenges()]
        if sport:
            rows = [r for r in rows if r.get("sport") == sport.strip().lower()]
        return rows[:max(1, min(int(limit), MAX_LIMIT))]

    @server.tool()
    @_guard
    async def get_challenge(challenge_id: str) -> dict:
        """The full leaderboard of one social challenge: every player with
        rank, total distance and when they last synced. Get the challenge_id
        from list_challenges."""
        c = await S().client()
        raw = await c.get_adhoc_challenge(challenge_id.strip())
        return project.challenge_detail(raw, await S().display_id())

    # ---------------------------------------------------------- profile

    @server.tool()
    @_guard
    async def list_gear(include_retired: bool = False) -> list[dict]:
        """Gear (shoes, bikes, …) with how much it has been used: number of
        activities, kilometres, hours, days used and the date of first use.

        Where a replacement limit is set, `limit_km` or `limit_hours` and
        `pct_of_limit` say how close it is. include_retired adds gear that was
        retired, which is off by default because most questions are about what
        is in rotation."""
        c = await S().client()
        rows = await c.list_gear("ACTIVE")
        if include_retired:
            rows = rows + await c.list_gear("RETIRED")
        return project.gear(rows)

    @server.tool()
    @_guard
    async def get_profile() -> dict:
        """The athlete profile behind the numbers: birth date, gender, weight,
        height, VO2max for running and cycling, FTP, lactate threshold heart
        rate and speed, critical swim speed, and the heart rate zones per
        sport. Useful before interpreting any training data."""
        c = await S().client()
        return project.profile(await c.user_settings(),
                               await c.personal_information(),
                               await c.heart_rate_zones())

    @server.tool()
    @_guard
    async def whoami() -> dict:
        """Which Garmin account this server is connected to, and how long the
        current access token is still valid. Use it to check the connection
        before blaming empty results on missing data."""
        from .session import probe
        return await probe(S())

    return server
