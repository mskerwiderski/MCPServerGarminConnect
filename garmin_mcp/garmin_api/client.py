"""Garmin Connect client - the MyCommons building block `garmin_api`.

Shared by MyFITContainer (app/garmin_api/), Garmin MCP (garmin_mcp/garmin_api/)
and MyGPXContainer (app/garmin_api/); never edit a copy, change it in MyCommons
and sync.
Merged on 2026-10-28 from MyFITContainer's app/garmin_client.py (originally
ported from MyActivitySync app/sync/garmin.py) and Garmin MCP's copy of it.

The client never signs in: the caller hands it the OAuth2 access token, or a
`token_source` it asks for one (see GarminClient). Signing in to Garmin is the
job of MyAuth and of the building block `garmin_login`.
Upload: POST /upload-service/upload (multipart, Bearer).
Rename: PUT /activity-service/activity/{id}.
Get: GET /activitylist-service/.../activities (list) and
GET /download-service/files/activity/{id} (ZIP with the original FIT).
Courses and sending them to devices: course-service, device-service (since
1.1.0, MyGPXContainer).
`fit_tool` is imported lazily, only for uploading weight (_build_weight_fit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger(__name__)

UA_DEFAULT = "GCM-iOS-5.7.2.1"
UA_OAUTH = "com.garmin.android.apps.connectmobile"   # upload expects the app identifier

CONNECT_API = "https://connectapi.garmin.com"

# Garmin activity visibility (accessControlRuleDTO.typeKey). The values are
# Garmin's own keys; the PUT needs ONLY the typeKey (no typeId).
# private = Only me · subscribers = My connections · groups = connections + groups
# · public = Everyone.
GARMIN_PRIVACY_KEYS = ("private", "subscribers", "groups", "public")

# Garmin elevation source per activity (metadataDTO.elevationCorrected): "device" =
# elevation recorded by the device (correction OFF), "dem" = digital elevation model
# data (correction ON). device is the default.
GARMIN_ELEVATION_KEYS = ("device", "dem")


def _local_and_gmt(day: str, measured_at: Optional[datetime],
                   home_tz: Optional[str]) -> tuple[datetime, datetime]:
    """(local wall clock, true UTC instant) for Garmin timestamps.
    measured_at = naive local measurement time (or None -> day 12:00 local). home_tz =
    IANA name (Europe/Berlin); missing/invalid -> local is treated like UTC
    (no offset). Garmin needs BOTH separately: Local shows the wall-clock time,
    GMT the instant — setting them identical was the bug (+offset too late)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    local = measured_at or datetime.fromisoformat(f"{day}T12:00:00")
    local = local.replace(microsecond=0, tzinfo=None)
    tz = None
    if home_tz:
        try:
            tz = ZoneInfo(home_tz)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    if tz is None:
        return local, local
    gmt = local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return local, gmt


def _build_weight_fit(day: str, weight_kg: float, percent_fat=None,
                      muscle_mass=None, percent_hydration=None, bone_mass=None,
                      measured_at: Optional[datetime] = None,
                      home_tz: Optional[str] = None) -> bytes:
    """Minimal weight scale FIT (FileType.WEIGHT) for the Garmin upload —
    weight (kg) + optional body composition. Timestamp = true UTC instant
    of the local measurement time (measured_at), or day 12:00 local."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.weight_scale_message import WeightScaleMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    _, gmt = _local_and_gmt(day, measured_at, home_tz)
    ts = int(gmt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.WEIGHT
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.time_created = ts
    b.add(fid)
    ws = WeightScaleMessage()
    ws.timestamp = ts
    ws.weight = float(weight_kg)
    if percent_fat is not None:
        ws.percent_fat = float(percent_fat)
    if muscle_mass is not None:
        ws.muscle_mass = float(muscle_mass)
    if percent_hydration is not None:
        ws.percent_hydration = float(percent_hydration)
    if bone_mass is not None:
        ws.bone_mass = float(bone_mass)
    b.add(ws)
    return bytes(b.build().to_bytes())


class GarminError(Exception):
    pass


class GarminAuthError(GarminError):
    pass


class GarminHttpError(GarminError):
    """Error with HTTP status (for callers that react differently per status)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class GarminUploadError(GarminError):
    pass


class GarminDuplicateError(GarminUploadError):
    pass


@dataclass
class OAuth2Token:
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: float  # unix seconds

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "OAuth2Token":
        d = json.loads(s)
        return cls(**d)


# Async callable returning {"access_token", "expires_at"}; see GarminClient.
TokenSource = Callable[[], Awaitable[dict]]


class GarminClient:
    """Garmin Connect client. It never signs in and never renews by itself:
    the access token comes from the caller, either set directly
    (`client.oauth2_token = OAuth2Token(...)`) or from a `token_source` the
    client asks whenever it has no token or the token expired.

        async with GarminClient(token_source=my_tokens) as client:
            await client.upload(fit_bytes, "activity.fit")

    `token_source` is an async callable returning {"access_token",
    "expires_at"} (MyAuth's /api/internal/garmin/token, or a token file); it
    raises GarminAuthError when there is no Garmin connection.
    """

    def __init__(self, token_source: Optional[TokenSource] = None, *,
                 timeout: float = 30.0) -> None:
        self.oauth2_token: Optional[OAuth2Token] = None
        self._token_source = token_source
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GarminClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---------- access token ----------

    def restore(self, oauth1_json: Optional[str] = None, oauth2_json: Optional[str] = None) -> None:
        """For callers of the old API: forgets the current token, so the next
        call asks the token source again. The arguments are ignored."""
        self.oauth2_token = None

    async def _refresh_if_needed(self) -> None:
        if self.oauth2_token and not self.oauth2_token.is_expired:
            return
        if self._token_source is None:
            raise GarminAuthError("no valid Garmin access token")
        a = await self._token_source()
        self.oauth2_token = OAuth2Token(
            access_token=a["access_token"], refresh_token="", expires_in=0,
            expires_at=float(a.get("expires_at") or time.time() + 600))

    # ---------- API: profile (test) ----------

    async def fetch_display_name(self) -> str:
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "nk": "NT",
                "User-Agent": UA_DEFAULT,
            },
        )
        if r.status_code != 200:
            return ""
        j = r.json()
        return j.get("fullName") or j.get("displayName") or j.get("userName") or ""

    async def profile_display_id(self) -> str:
        """socialProfile.displayName (the GUID-like identifier, NOT the full
        name) — required in the path by usersummary/wellness endpoints. Cached."""
        if getattr(self, "_display_id", None):
            return self._display_id
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        j = r.json() if r.status_code == 200 else {}
        self._display_id = j.get("displayName") or ""
        return self._display_id

    # ---------- API: athlete profile (user settings + HR zones) ----------

    async def full_name(self) -> tuple[str, str]:
        """(first name, last name) from socialProfile.fullName (best-effort split)."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        full = ((r.json() or {}).get("fullName") if r.status_code == 200 else "") or ""
        parts = full.split()
        return (parts[0], " ".join(parts[1:])) if parts else ("", "")

    async def user_settings(self) -> dict:
        """userData from /userprofile-service/userprofile/user-settings: bio,
        VO₂max (run/bike), lactate threshold, weight/height/birth date/gender."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/userprofile/user-settings",
            headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"user-settings HTTP {r.status_code}")
        return ((r.json() or {}).get("userData") or {})

    async def personal_information(self) -> dict:
        """biometricProfile from /userprofile-service/userprofile/personal-information:
        e.g. criticalSwimSpeed (swim threshold, mm/s), functionalThresholdPower,
        lactateThresholdHeartRate, vo2Max/vo2MaxCycling."""
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/userprofile/personal-information",
            headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"personal-information HTTP {r.status_code}")
        return ((r.json() or {}).get("biometricProfile") or {})

    async def heart_rate_zones(self) -> list[dict]:
        """HR zones per sport (/biometric-service/heartRateZones): each entry has
        zone1Floor..zone5Floor, maxHeartRateUsed, restingHeartRateUsed, sport."""
        r = await self._http.get(
            f"{CONNECT_API}/biometric-service/heartRateZones", headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"heartRateZones HTTP {r.status_code}")
        j = r.json()
        return j if isinstance(j, list) else []

    # ---------- API: gear (v2; endpoints captured live 2026-06-18) ----------

    async def _bearer(self, content_type: bool = False) -> dict:
        await self._refresh_if_needed()
        assert self.oauth2_token
        h = {"Authorization": f"Bearer {self.oauth2_token.access_token}",
             "nk": "NT", "User-Agent": UA_DEFAULT}
        if content_type:
            h["Content-Type"] = "application/json"
        return h

    async def gear_profile_pk(self) -> int | None:
        """userProfilePk (for gear create). From socialProfile, cached."""
        if getattr(self, "_gear_pk", None):
            return self._gear_pk
        r = await self._http.get(
            f"{CONNECT_API}/userprofile-service/socialProfile", headers=await self._bearer())
        j = r.json() if r.status_code == 200 else {}
        self._gear_pk = j.get("profileId") or j.get("id")
        return self._gear_pk

    async def list_gear(self, statuses: str = "ACTIVE") -> list[dict]:
        """Gear list (v2): items with e.g. uuid, gearType (SHOES/BIKE), name, brand,
        status, distanceUsedMeters."""
        r = await self._http.get(
            f"{CONNECT_API}/gear-service/gear/v2/list",
            params={"start": 0, "limit": 100, "gearStatuses": statuses,
                    "sortOrder": "firstUseDate_desc"}, headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"list_gear HTTP {r.status_code}: {r.text[:160]}")
        data = r.json()
        return data if isinstance(data, list) else []

    async def create_gear(self, *, name: str, gear_type: str, brand: str = "",
                          first_use: str | None = None) -> str:
        """Creates gear (gear_type 'SHOES'|'BIKE'). Returns the uuid."""
        body = {
            "userProfilePk": await self.gear_profile_pk(),
            "gearType": gear_type, "status": "ACTIVE", "name": name,
            "brand": brand or None, "usageType": "DISTANCE",
            "maxUsageDistanceMeters": 0, "maxUsageDurationSeconds": 0,
            "firstUseDate": first_use or datetime.utcnow().strftime("%Y-%m-%d"),
        }
        r = await self._http.post(
            f"{CONNECT_API}/gear-service/gear/v2",
            headers=await self._bearer(content_type=True), content=json.dumps(body))
        if r.status_code not in (200, 201):
            raise GarminError(f"create_gear HTTP {r.status_code}: {r.text[:160]}")
        return (r.json() or {}).get("uuid")

    async def rename_gear(self, gear_uuid: str, *, name: str, brand: str = "",
                          first_use: str | None = None) -> None:
        """Updates name/brand (array-free: PUT of the object) and optionally the
        firstUseDate (purchase date). Best-effort — the edit endpoint is not
        verified separately."""
        items = await self.list_gear()
        obj = next((g for g in items if str(g.get("uuid")) == str(gear_uuid)), None)
        if obj is None:
            return
        obj = {**obj, "name": name, "brand": brand or None}
        if first_use:
            obj["firstUseDate"] = first_use
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}",
            headers=await self._bearer(content_type=True), content=json.dumps(obj))
        if r.status_code not in (200, 204):
            raise GarminError(f"rename_gear HTTP {r.status_code}: {r.text[:160]}")

    async def retire_gear(self, gear_uuid: str) -> None:
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}/status/RETIRED",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"retire_gear HTTP {r.status_code}: {r.text[:160]}")

    async def delete_gear(self, gear_uuid: str) -> None:
        r = await self._http.delete(
            f"{CONNECT_API}/gear-service/gear/v2/{gear_uuid}", headers=await self._bearer())
        if r.status_code not in (200, 204, 404):
            raise GarminError(f"delete_gear HTTP {r.status_code}: {r.text[:160]}")

    async def link_gear(self, gear_uuid: str, activity_id: int) -> None:
        """Assigns the gear to an activity (PUT link/{uuid}/activity/{id})."""
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/link/{gear_uuid}/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"link_gear HTTP {r.status_code}: {r.text[:160]}")

    async def unlink_gear(self, gear_uuid: str, activity_id: int) -> None:
        """Removes the gear assignment from an activity (PUT unlink/{uuid}/activity/{id})."""
        r = await self._http.put(
            f"{CONNECT_API}/gear-service/gear/unlink/{gear_uuid}/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"unlink_gear HTTP {r.status_code}: {r.text[:160]}")

    # ---------- workouts (training plan -> Garmin calendar) ----------

    async def create_workout(self, body: dict) -> int:
        """Creates a workout in the Garmin library (POST workout-service/
        workout). Returns the workoutId. Scheduling is done separately via
        schedule_workout."""
        r = await self._http.post(
            f"{CONNECT_API}/workout-service/workout",
            headers=await self._bearer(content_type=True), content=json.dumps(body))
        if r.status_code not in (200, 201):
            raise GarminError(f"create_workout HTTP {r.status_code}: {r.text[:160]}")
        wid = (r.json() or {}).get("workoutId")
        if not isinstance(wid, int):
            raise GarminError(f"create_workout: no workoutId in response {r.text[:160]}")
        return wid

    async def delete_workout(self, workout_id: int) -> None:
        """Removes a workout from the library (404 = already gone)."""
        r = await self._http.delete(
            f"{CONNECT_API}/workout-service/workout/{workout_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204, 404):
            raise GarminError(f"delete_workout HTTP {r.status_code}: {r.text[:160]}")

    async def schedule_workout(self, workout_id: int, day: str) -> int:
        """Schedules a workout on a day (YYYY-MM-DD) in the Garmin calendar.
        Returns the workoutScheduleId - unschedule_workout needs it."""
        r = await self._http.post(
            f"{CONNECT_API}/workout-service/schedule/{workout_id}",
            headers=await self._bearer(content_type=True),
            content=json.dumps({"date": day}))
        if r.status_code not in (200, 201):
            raise GarminError(f"schedule_workout HTTP {r.status_code}: {r.text[:160]}")
        sid = (r.json() or {}).get("workoutScheduleId")
        if not isinstance(sid, int):
            raise GarminError(f"schedule_workout: no workoutScheduleId in {r.text[:160]}")
        return sid

    async def unschedule_workout(self, schedule_id: int) -> None:
        """Removes a scheduled entry from the calendar, keeps the workout in the
        library (404 = already gone)."""
        r = await self._http.delete(
            f"{CONNECT_API}/workout-service/schedule/{schedule_id}",
            headers=await self._bearer())
        if r.status_code not in (200, 204, 404):
            raise GarminError(f"unschedule_workout HTTP {r.status_code}: {r.text[:160]}")

    # ---------- calendar events (competitions) ----------
    # Endpoints and behaviour per garminconnect-js (MIT, verified live
    # 2026-10-06): self-created events are always PRIVATE, an unknown
    # eventType is a 500, isPrimaryEvent also sets isTrainingEvent, the
    # date must lie within the next 365 days (Garmin manual).

    async def list_calendar_events(self, start: str, end: str) -> list[dict]:
        r = await self._http.get(
            f"{CONNECT_API}/calendar-service/events",
            params={"startDate": start, "endDate": end}, headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"list_calendar_events HTTP {r.status_code}: {r.text[:160]}")
        j = r.json()
        return j if isinstance(j, list) else []

    async def get_calendar_event(self, event_id: int) -> dict | None:
        r = await self._http.get(f"{CONNECT_API}/calendar-service/event/{event_id}",
                                 headers=await self._bearer())
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GarminError(f"get_calendar_event HTTP {r.status_code}: {r.text[:160]}")
        return r.json()

    async def create_calendar_event(self, body: dict) -> dict:
        r = await self._http.post(
            f"{CONNECT_API}/calendar-service/event",
            headers=await self._bearer(content_type=True), content=json.dumps(body))
        if r.status_code not in (200, 201):
            raise GarminHttpError(
                f"create_calendar_event HTTP {r.status_code}: {r.text[:160]}", r.status_code)
        j = r.json() or {}
        if not isinstance(j.get("id"), int):
            raise GarminError(f"create_calendar_event: no id in response {r.text[:160]}")
        return j

    async def update_calendar_event(self, event_id: int, body: dict) -> None:
        """PUT of the WHOLE event (Garmin responds without a body)."""
        r = await self._http.put(
            f"{CONNECT_API}/calendar-service/event/{event_id}",
            headers=await self._bearer(content_type=True), content=json.dumps(body))
        if r.status_code not in (200, 204):
            raise GarminHttpError(
                f"update_calendar_event HTTP {r.status_code}: {r.text[:160]}", r.status_code)

    async def delete_calendar_event(self, event_id: int) -> None:
        r = await self._http.delete(f"{CONNECT_API}/calendar-service/event/{event_id}",
                                    headers=await self._bearer())
        if r.status_code not in (200, 204, 404):
            raise GarminError(f"delete_calendar_event HTTP {r.status_code}: {r.text[:160]}")

    async def multisport_children(self, activity_id: int) -> list[dict]:
        """Child activities of a multisport parent activity: [{id, type}] with
        typeKey (e.g. 'cycling', 'running', 'transition_v2'). Empty for
        non-multisport or on errors (best-effort)."""
        h = await self._bearer()
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}", headers=h)
        if r.status_code != 200:
            return []
        md = (r.json() or {}).get("metadataDTO") or {}
        out = []
        for cid in (md.get("childIds") or []):
            rc = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{cid}", headers=h)
            if rc.status_code != 200:
                continue
            tk = ((rc.json() or {}).get("activityTypeDTO") or {}).get("typeKey")
            out.append({"id": int(cid), "type": tk or ""})
        return out

    # ---------- API: health (read; pull source, endpoints from python-garminconnect) ----------
    # Note: taken over from the library, still to be verified live. All best-effort:
    # empty list/None on non-200, so an endpoint problem does not break the pull.

    async def get_body_composition(self, start: str, end: str) -> list[dict]:
        """Weight/body composition per day (YYYY-MM-DD..YYYY-MM-DD).
        Items: calendarDate, weight (grams!), bodyFat (%), muscleMass (grams)."""
        r = await self._http.get(
            f"{CONNECT_API}/weight-service/weight/dateRange",
            params={"startDate": start, "endDate": end}, headers=await self._bearer())
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("dateWeightList") or []

    async def get_blood_pressure(self, start: str, end: str) -> dict:
        """Blood pressure measurements in the range (raw JSON with measurementSummaries)."""
        r = await self._http.get(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure/range/{start}/{end}",
            params={"includeAll": "true"}, headers=await self._bearer())
        return r.json() if r.status_code == 200 else {}

    async def get_resting_hr(self, display_name: str, day: str) -> float | None:
        """Resting heart rate of a day (usersummary)."""
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/usersummary/daily/{display_name}",
            params={"calendarDate": day}, headers=await self._bearer())
        if r.status_code != 200:
            return None
        v = (r.json() or {}).get("restingHeartRate")
        return float(v) if v is not None else None

    async def get_hrv(self, day: str) -> float | None:
        """HRV of a day (overnight RMSSD, lastNightAvg) — Garmin's HRV status
        is RMSSD, NOT SDNN."""
        r = await self._http.get(
            f"{CONNECT_API}/hrv-service/hrv/{day}", headers=await self._bearer())
        if r.status_code != 200:
            return None
        s = (r.json() or {}).get("hrvSummary") or {}
        v = s.get("lastNightAvg") or s.get("weeklyAvg")
        return float(v) if v is not None else None

    async def get_sleep(self, display_name: str, day: str) -> dict:
        """Sleep DTO of a day (wake-up date). Returns the raw dailySleepDTO
        (seconds per stage: sleepTimeSeconds/deep/light/rem/awakeSleepSeconds,
        averageRespirationValue, start/end). {} on non-200 — an endpoint
        problem does not break the pull."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailySleepData/{display_name}",
            params={"date": day, "nonSleepBufferMinutes": 60},
            headers=await self._bearer())
        if r.status_code != 200:
            return {}
        return (r.json() or {}).get("dailySleepDTO") or {}

    async def get_sleep_full(self, display_name: str, day: str) -> dict:
        """Like get_sleep, but the COMPLETE dailySleepData response (incl.
        dailySleepDTO.sleepScores = Garmin Sleep Score). {} on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailySleepData/{display_name}",
            params={"date": day, "nonSleepBufferMinutes": 60},
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def get_hrv_summary(self, day: str) -> dict:
        """hrvSummary of a day: lastNightAvg (RMSSD), status (BALANCED/
        UNBALANCED/LOW/…), baseline. {} on non-200/missing."""
        r = await self._http.get(
            f"{CONNECT_API}/hrv-service/hrv/{day}", headers=await self._bearer())
        if r.status_code != 200:
            return {}
        return (r.json() or {}).get("hrvSummary") or {}

    async def get_daily_summary(self, display_name: str, day: str) -> dict:
        """The full usersummary daily response of a day: restingHeartRate,
        stress (averageStressLevel), Body Battery (bodyBatteryMostRecentValue,
        …Charged/…Drained), steps, calories, respiration rate. {} on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/usersummary/daily/{display_name}",
            params={"calendarDate": day}, headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def get_body_battery_series(self, day: str) -> list[list[int]]:
        """Body Battery curve of a day (wellness-service dailyStress,
        3-minute grid) as [[epoch_s, level], …] without gaps (level None).
        Also covers activities (still) missing from the usersummary's
        `bodyBatteryActivityEventList`. [] on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailyStress/{day}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        return [[int(row[0] // 1000), int(row[2])]
                for row in (r.json() or {}).get("bodyBatteryValuesArray") or []
                if len(row) >= 3 and row[2] is not None]

    async def get_step_intervals(self, display_name: str, day: str) -> list[list[int]]:
        """Steps per quarter hour of a day (wellness-service
        dailySummaryChart) as [[start_s, end_s, steps], …]. [] on non-200.
        Checked live 2026-10-16: sum = the usersummary's totalSteps."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/dailySummaryChart/{display_name}",
            params={"date": day}, headers=await self._bearer())
        if r.status_code != 200 or not isinstance(r.json(), list):
            return []
        return [[_gmt_s(x["startGMT"]), _gmt_s(x["endGMT"]), int(x.get("steps") or 0)]
                for x in r.json() if x.get("startGMT") and x.get("endGMT")]

    async def get_floor_intervals(self, day: str) -> list[list[float]]:
        """Floors climbed per quarter hour ([[start_s, end_s, floors], …]).
        [] on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/floorsChartData/daily/{day}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        return [[_gmt_s(row[0]), _gmt_s(row[1]), float(row[2] or 0)]
                for row in (r.json() or {}).get("floorValuesArray") or [] if len(row) >= 3]

    async def get_training_readiness(self, day: str) -> list[dict]:
        """Training readiness entries of a day (metrics-service). The
        MORNING entry (Garmin's Morning Report) has inputContext =
        'AFTER_WAKEUP_RESET' — if it is missing, the watch has not synced
        since waking up. [] on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/trainingreadiness/{day}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    async def get_training_status(self, day: str) -> dict:
        """Garmin's aggregated Training Status (PRODUCTIVE/MAINTAINING/…,
        Load Focus, Acute Load, VO2max). {} on non-200."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/trainingstatus/aggregated/{day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def delete_weight(self, day: str, sample_pk) -> None:
        """Deletes a weight/body composition entry (samplePk + date).
        DELETE /weight-service/weight/{day}/byversion/{samplePk}."""
        r = await self._http.delete(
            f"{CONNECT_API}/weight-service/weight/{day}/byversion/{sample_pk}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"delete_weight HTTP {r.status_code}: {r.text[:160]}")

    async def delete_blood_pressure(self, day: str, version) -> None:
        """Deletes a blood pressure measurement (date + `version` from get_blood_pressure).
        DELETE /bloodpressure-service/bloodpressure/{day}/{version}."""
        r = await self._http.delete(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure/{day}/{version}",
            headers=await self._bearer())
        if r.status_code not in (200, 204):
            raise GarminError(f"delete_blood_pressure HTTP {r.status_code}: {r.text[:160]}")

    # ---------- API: upload ----------

    async def upload(
        self,
        fit_bytes: bytes,
        filename: str,
        start_utc: Optional[datetime] = None,
    ) -> Optional[int]:
        """Upload a FIT and resolve the resulting activity id.

        start_utc is the activity's start time in UTC (from intervals.icu). It is
        used to correlate the asynchronously-processed upload back to its
        Garmin activity id by matching startTimeGMT. Required for reliable
        post-upload resolution; without it we fall back to "newest activity",
        which is unreliable under back-to-back uploads.
        """
        await self._refresh_if_needed()
        assert self.oauth2_token
        url = f"{CONNECT_API}/upload-service/upload"
        files = {"file": (filename, fit_bytes, "application/octet-stream")}
        r = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_OAUTH,
            },
            files=files,
        )
        if r.status_code == 409 or "duplicate" in r.text.lower():
            raise GarminDuplicateError(f"duplicate (HTTP {r.status_code})")
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(f"upload HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except Exception:
            data = {}
        log.info("garmin upload response: %s", json.dumps(data)[:500])
        result = (data or {}).get("detailedImportResult") or {}
        successes = result.get("successes") or []
        failures = result.get("failures") or []
        if not successes and failures:
            messages = json.dumps(failures)[:300]
            if "duplicate" in messages.lower():
                raise GarminDuplicateError(messages)
            raise GarminUploadError(messages)
        if successes:
            internal_id = successes[0].get("internalId")
            if internal_id is not None:
                return int(internal_id)
        if start_utc is None:
            log.warning("garmin upload: no start_utc provided; cannot resolve activity id")
            return None
        return await self._resolve_activity_by_start_time(start_utc)

    async def add_blood_pressure(
        self, day: str, systolic: int, diastolic: int,
        pulse: int = 0, notes: str = "",
        measured_at: Optional[datetime] = None, home_tz: Optional[str] = None,
    ) -> None:
        """Health hub: record a day's blood pressure measurement. Timestamp =
        local measurement time (measured_at), or day 12:00 local; Local = wall-clock
        time, GMT = the same instant in UTC (separately!). Garmin creates a NEW
        entry (no update) — the caller ensures there is only one per day."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        local, gmt = _local_and_gmt(day, measured_at, home_tz)
        fmt = "%Y-%m-%dT%H:%M:%S.00"
        body = {
            "measurementTimestampLocal": local.strftime(fmt),
            "measurementTimestampGMT": gmt.strftime(fmt),
            "systolic": int(systolic), "diastolic": int(diastolic),
            "sourceType": "MANUAL", "notes": notes or "",
        }
        # Only send the pulse if plausible (1..300) — Garmin rejects pulse=0 with
        # HTTP 400; if the pulse is missing (no RHR that day), it accepts null.
        if pulse and 1 <= int(pulse) <= 300:
            body["pulse"] = int(pulse)
        r = await self._http.post(
            f"{CONNECT_API}/bloodpressure-service/bloodpressure",
            headers={"Authorization": f"Bearer {self.oauth2_token.access_token}",
                     "User-Agent": UA_DEFAULT, "Content-Type": "application/json"},
            json=body)
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(
                f"blood pressure HTTP {r.status_code}: {r.text[:200]}")

    async def add_body_composition(
        self, day: str, weight_kg: float, percent_fat: Optional[float] = None,
        muscle_mass: Optional[float] = None, percent_hydration: Optional[float] = None,
        bone_mass: Optional[float] = None,
        measured_at: Optional[datetime] = None, home_tz: Optional[str] = None,
    ) -> None:
        """Health hub: record a day's weight + body composition.
        Garmin only accepts this as a weight scale FIT (no JSON field for body fat)
        via the upload service. Weight in kg; timestamp = local measurement
        time (measured_at), or day 12:00 local, as a true UTC instant.
        The caller ensures there is only one per day (upload = new entry)."""
        fit_bytes = _build_weight_fit(day, weight_kg, percent_fat, muscle_mass,
                                      percent_hydration, bone_mass,
                                      measured_at=measured_at, home_tz=home_tz)
        await self._refresh_if_needed()
        assert self.oauth2_token
        files = {"file": (f"weight_{day}.fit", fit_bytes, "application/octet-stream")}
        r = await self._http.post(
            f"{CONNECT_API}/upload-service/upload",
            headers={"Authorization": f"Bearer {self.oauth2_token.access_token}",
                     "User-Agent": UA_OAUTH},
            files=files)
        # 409/"duplicate" = identical measurement already exists -> treat as ok.
        if r.status_code == 409 or "duplicate" in r.text.lower():
            return
        if not (200 <= r.status_code < 300):
            raise GarminUploadError(
                f"body composition HTTP {r.status_code}: {r.text[:200]}")

    async def find_activity_by_start_time(
        self,
        start_utc: datetime,
        tolerance_s: int = 60,
    ) -> Optional[int]:
        """Find an existing Garmin activity whose startTimeGMT matches the
        given UTC datetime within tolerance_s seconds. Returns the activityId
        with the highest value (most recent upload) when multiple match, so
        post-upload resolution prefers our just-created copy over older ones
        with the same start time.
        """
        await self._refresh_if_needed()
        assert self.oauth2_token
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        else:
            start_utc = start_utc.astimezone(timezone.utc)
        day = start_utc.strftime("%Y-%m-%d")
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params={"limit": 50, "start": 0, "startDate": day, "endDate": day},
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            return None
        arr = r.json() or []
        best_id: Optional[int] = None
        for a in arr:
            ts = a.get("startTimeGMT")
            if not ts:
                continue
            try:
                # Garmin returns "YYYY-MM-DD HH:MM:SS" in GMT (no tz suffix).
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if abs((dt - start_utc).total_seconds()) <= tolerance_s:
                aid = a.get("activityId")
                if aid is None:
                    continue
                if best_id is None or int(aid) > best_id:
                    best_id = int(aid)
        return best_id

    async def _resolve_activity_by_start_time(
        self,
        start_utc: datetime,
        attempts: int = 12,
        interval_s: float = 5.0,
    ) -> Optional[int]:
        for i in range(attempts):
            await asyncio.sleep(interval_s)
            aid = await self.find_activity_by_start_time(start_utc)
            if aid is not None:
                log.info("garmin resolved activity %s for start=%s (attempt %d)", aid, start_utc.isoformat(), i + 1)
                return aid
        log.warning("garmin: could not resolve activity id for start=%s after %d attempts", start_utc.isoformat(), attempts)
        return None

    async def set_activity_metadata(
        self,
        activity_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        retries: int = 3,
    ) -> bool:
        """Update activity name and/or description on Garmin. Idempotent:
        one GET to compare, then a single PUT that carries only the
        diverging fields. Pass `None` to leave a field untouched, `""`
        to clear it. Returns True iff a write was made.
        """
        wrote = False
        if name is not None:
            wrote = await self.rename_activity(activity_id, name, retries=retries)
        if description is not None:
            wrote = await self.set_activity_description(
                activity_id, description, retries=retries) or wrote
        return wrote

    async def rename_activity(
        self, activity_id: int, name: str, retries: int = 3
    ) -> bool:
        """Thin wrapper around _update_activity_field for name-only updates,
        kept so the orchestrator's metadata-resync path can address name and
        description independently with clearer log lines.
        """
        return await self._update_activity_field(
            activity_id, "activityName", (name or "").strip(), retries=retries
        )

    async def set_activity_description(
        self, activity_id: int, description: str, retries: int = 3
    ) -> bool:
        """Set a Garmin activity's description. Idempotent: GETs current
        description first and skips the PUT if it already matches. An
        empty string clears the description. Returns True iff a write was
        made.
        """
        description = description or ""
        return await self._update_activity_field(
            activity_id, "description", description, retries=retries
        )

    async def get_elevation_correction_state(
        self, activity_id: int
    ) -> Optional[bool]:
        """Return the current value of metadataDTO.elevationCorrected for
        an activity, or None if the GET fails / the field is absent.

        Used as a pre-check before calling disable_elevation_corrections,
        because the toggle endpoint is NOT idempotent — sending it when
        corrections are already off would re-enable them.
        """
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return None
        assert self.oauth2_token
        try:
            r = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "garmin elevation pre-GET %s failed: %s", activity_id, e
            )
            return None
        if r.status_code != 200:
            log.warning(
                "garmin elevation pre-GET %s HTTP %s", activity_id, r.status_code
            )
            return None
        try:
            md = (r.json() or {}).get("metadataDTO") or {}
        except Exception:  # noqa: BLE001
            return None
        v = md.get("elevationCorrected")
        return v if isinstance(v, bool) else None

    async def set_elevation_correction(
        self, activity_id: int, corrected: bool, retries: int = 3
    ) -> bool:
        """Set Garmin Connect's "Elevation Corrections" for one activity to a
        desired state: corrected=True -> use the digital elevation model (DEM),
        corrected=False -> use the recorded device elevation.

        Safe to call repeatedly — does a GET first and only sends the
        (non-idempotent) toggle when the current state differs from the target.

        Returns True if the activity is in the desired state after the call
        (either it already was, or we just toggled it). Returns False on:
          - GET failed AND we can't safely guess the current state (we refuse
            to send a blind toggle that could flip it the wrong way),
          - or the toggle POST returned a non-2xx after retries.

        Garmin's web UI uses POST /activity-service/activity/
        toggleElevationCorrection/{id} with a form-encoded body of
        `elevationCorrected=<current_state>` — the server inverts internally.
        Sending the CURRENT state therefore lands on the opposite (= target,
        since we only get here when current != target).
        """
        state = await self.get_elevation_correction_state(activity_id)
        if state == corrected:
            # Already in the desired state. No-op — success for the caller.
            return True
        if state is None:
            # Pre-check failed (network, auth, missing field — or Garmin hasn't
            # computed elevationCorrected yet right after upload). Refuse a blind
            # toggle; the caller retries on the next sync.
            log.warning(
                "garmin set_elevation_correction %s -> %s: pre-check inconclusive, "
                "skipping toggle", activity_id, corrected,
            )
            return False
        # state != corrected → send the toggle (body = current state, server flips).
        assert self.oauth2_token
        url = (
            f"{CONNECT_API}/activity-service/activity/"
            f"toggleElevationCorrection/{activity_id}"
        )
        body = f"elevationCorrected={'true' if state else 'false'}"
        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            assert self.oauth2_token
            r = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
                content=body,
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning(
            "garmin toggleElevationCorrection %s failed: HTTP %s body=%s",
            activity_id,
            r.status_code,
            r.text[:200],
        )
        return False

    async def disable_elevation_corrections(
        self, activity_id: int, retries: int = 3
    ) -> bool:
        """Ensure "Elevation Corrections" is OFF (use device elevation)."""
        return await self.set_elevation_correction(
            activity_id, False, retries=retries)

    async def _update_activity_field(
        self, activity_id: int, field: str, value: str, retries: int = 3
    ) -> bool:
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token

        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                current = (g.json() or {}).get(field) or ""
                if current == value:
                    return False
        except Exception as e:  # noqa: BLE001
            log.warning(
                "garmin update %s: pre-GET failed for %s: %s", field, activity_id, e
            )

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            assert self.oauth2_token
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id, field: value}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    # ---------- API: Get (MFC sync round trip) ----------

    async def list_recent_activities(self, limit: int = 20) -> list[dict]:
        """The most recent activities (newest first), each entry with e.g.
        activityId, activityName, startTimeGMT ("YYYY-MM-DD HH:MM:SS")."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params={"limit": limit, "start": 0},
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            raise GarminError(f"list activities HTTP {r.status_code}: {r.text[:200]}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def download_original_fit(self, activity_id: int) -> Optional[bytes]:
        """Downloads an activity's original file (download-service returns
        a ZIP) and extracts the FIT. None if the original is not a FIT
        (e.g. GPX import) — the caller then skips it."""
        import io
        import zipfile

        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.get(
            f"{CONNECT_API}/download-service/files/activity/{activity_id}",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code != 200:
            raise GarminError(
                f"download {activity_id} HTTP {r.status_code}: {r.text[:200]}")
        data = r.content
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not names:
                    return None
                return zf.read(names[0])
        except zipfile.BadZipFile:
            # Some responses are the file itself (no ZIP).
            if data[:12].find(b".FIT") != -1:
                return data
            return None

    async def set_activity_type(self, activity_id: int, type_key: str,
                                retries: int = 3) -> bool:
        """Sets the Garmin activity type (activityTypeDTO.typeKey, e.g.
        'lap_swimming'). Idempotent: GET comparison, then one PUT. True if
        a write was made."""
        if not type_key:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("activityTypeDTO") or {}).get("typeKey")
                if cur == type_key:
                    return False
        except Exception as e:  # noqa: BLE001
            log.warning("garmin type pre-GET %s failed: %s", activity_id, e)

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "activityTypeDTO": {"typeKey": type_key}}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    async def activity_exists(self, activity_id: int) -> Optional[bool]:
        """True/False whether the activity still exists; None on auth/network/
        other errors (then do NOT treat it as deleted). Used before a
        metadata update to detect a dead link (activity deleted on Garmin)
        and upload again instead."""
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return None
        assert self.oauth2_token
        try:
            r = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
        except Exception:  # noqa: BLE001
            return None
        if r.status_code == 404:
            return False
        if r.status_code == 200:
            return True
        return None

    async def delete_activity(self, activity_id: int) -> bool:
        """Deletes an activity (DELETE /activity-service/activity/{id}). True
        on success or if it is already gone (404). For the replace path:
        changed correction FIT -> delete the old activity + upload again."""
        await self._refresh_if_needed()
        assert self.oauth2_token
        r = await self._http.request(
            "DELETE",
            f"{CONNECT_API}/activity-service/activity/{activity_id}",
            headers={
                "Authorization": f"Bearer {self.oauth2_token.access_token}",
                "User-Agent": UA_DEFAULT,
                "nk": "NT",
            },
        )
        if r.status_code in (200, 202, 204, 404):
            return True
        raise GarminError(
            f"delete activity {activity_id} HTTP {r.status_code}: {r.text[:160]}")

    async def set_activity_summary(self, activity_id: int,
                                   distance_m: Optional[float] = None,
                                   duration_s: Optional[float] = None,
                                   retries: int = 3) -> bool:
        """Overwrites an activity's distance (m) and/or duration (s)
        (summaryDTO — like the edit in Garmin Connect Web). Idempotent via
        GET comparison; best-effort: not every activity type allows the
        override, then Garmin responds non-2xx -> False."""
        if distance_m is None and duration_s is None:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        body: dict = {}
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            cur = (g.json() or {}).get("summaryDTO") or {} if g.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            cur = {}
        if distance_m is not None and round(cur.get("distance") or -1, 1) != round(distance_m, 1):
            body["distance"] = float(distance_m)
        if duration_s is not None and round(cur.get("duration") or -1, 1) != round(duration_s, 1):
            body["duration"] = float(duration_s)
        if not body:
            return False

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "summaryDTO": body}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning("garmin summary override %s rejected", activity_id)
        return False

    # Sidecar-feel (1 = Strong … 5 = Weak) -> Garmin directWorkoutFeel
    # (0–100 in steps of 25, 100 = best/Strong).
    _FEEL_TO_GARMIN = {1: 100.0, 2: 75.0, 3: 50.0, 4: 25.0, 5: 0.0}

    async def set_self_evaluation(self, activity_id: int,
                                  feel: Optional[int] = None,
                                  rpe: Optional[int] = None,
                                  retries: int = 3) -> bool:
        """Self-evaluation as in the Connect web edit: summaryDTO.directWorkoutFeel
        (0/25/50/75/100) + directWorkoutRpe (RPE × 10, 10–100). Field names from
        community knowledge — verify live against the real account. Idempotent
        via GET comparison; best-effort like set_activity_summary (non-2xx ->
        False, never fatal)."""
        if feel is None and rpe is None:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            cur = (g.json() or {}).get("summaryDTO") or {} if g.status_code == 200 else {}
        except Exception:  # noqa: BLE001
            cur = {}
        body: dict = {}
        if feel is not None and feel in self._FEEL_TO_GARMIN:
            want = self._FEEL_TO_GARMIN[feel]
            if cur.get("directWorkoutFeel") != want:
                body["directWorkoutFeel"] = want
        if rpe is not None and 1 <= rpe <= 10:
            want = float(rpe * 10)
            if cur.get("directWorkoutRpe") != want:
                body["directWorkoutRpe"] = want
        if not body:
            return False

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({"activityId": activity_id,
                                    "summaryDTO": body}),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        log.warning("garmin self-evaluation %s rejected", activity_id)
        return False

    async def set_event_type_race(self, activity_id: int,
                                  retries: int = 3) -> bool:
        """Marks an activity as a race (eventTypeDTO typeKey 'race').
        Idempotent via GET comparison; only sets it, never reverts it."""
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("eventTypeDTO") or {}).get("typeKey")
                if cur == "race":
                    return False
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({
                    "activityId": activity_id,
                    "eventTypeDTO": {"typeId": 1, "typeKey": "race"},
                }),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    async def set_activity_privacy(self, activity_id: int, type_key: str,
                                   retries: int = 3) -> bool:
        """Sets an activity's visibility (accessControlRuleDTO.typeKey:
        private | subscribers | groups | public). Changes ONLY the setting —
        no re-upload. Idempotent via GET comparison. True on success/already set."""
        if type_key not in GARMIN_PRIVACY_KEYS:
            return False
        try:
            await self._refresh_if_needed()
        except GarminAuthError:
            return False
        assert self.oauth2_token
        try:
            g = await self._http.get(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "User-Agent": UA_DEFAULT,
                    "nk": "NT",
                },
            )
            if g.status_code == 200:
                cur = ((g.json() or {}).get("accessControlRuleDTO") or {}).get("typeKey")
                if cur == type_key:
                    return True
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(1, retries + 1):
            try:
                await self._refresh_if_needed()
            except GarminAuthError:
                return False
            r = await self._http.put(
                f"{CONNECT_API}/activity-service/activity/{activity_id}",
                headers={
                    "Authorization": f"Bearer {self.oauth2_token.access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": UA_DEFAULT,
                },
                content=json.dumps({
                    "activityId": activity_id,
                    "accessControlRuleDTO": {"typeKey": type_key},
                }),
            )
            if 200 <= r.status_code < 300:
                return True
            if attempt < retries:
                await asyncio.sleep(5)
        return False

    # ---------- reading (from Garmin MCP) ----------

    async def search_activities(
        self,
        limit: int = 20,
        start: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        activity_type: Optional[str] = None,
    ) -> list[dict]:
        """Activity list with the filters the activitylist-service accepts.
        start_date/end_date are local calendar dates ("YYYY-MM-DD"),
        activity_type a Garmin typeKey ("running", "cycling", "lap_swimming").
        """
        params: dict[str, Any] = {"limit": limit, "start": start}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if activity_type:
            params["activityType"] = activity_type
        r = await self._http.get(
            f"{CONNECT_API}/activitylist-service/activities/search/activities",
            params=params, headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"list activities HTTP {r.status_code}: {r.text[:200]}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def get_activity_detail(self, activity_id: int) -> dict:
        """Garmin's own activity record (summaryDTO, activityTypeDTO, …).
        Raises GarminError on 404 so a wrong id is not silently an empty dict."""
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}",
            headers=await self._bearer())
        if r.status_code == 404:
            raise GarminError(f"activity {activity_id} not found")
        if r.status_code != 200:
            raise GarminError(f"activity {activity_id} HTTP {r.status_code}")
        return r.json() or {}

    async def list_adhoc_challenges(self) -> list[dict]:
        """Social challenges against friends, newest first.

        Two endpoints are needed. `historical` returns only FINISHED
        challenges; the one running this month exists exclusively under
        `active`. Asking only historical is why the current month looks like it
        does not exist - which is how this was found.

        `active` entries carry playerCount 0 and players []; the real numbers
        come from get_adhoc_challenge.
        """
        found: dict[str, dict] = {}
        for path in ("active", "historical"):
            r = await self._http.get(
                f"{CONNECT_API}/adhocchallenge-service/adHocChallenge/{path}",
                headers=await self._bearer())
            if r.status_code != 200:
                if path == "historical":       # the finished ones are the bulk
                    raise GarminError(f"adhoc challenges HTTP {r.status_code}")
                continue
            for item in (r.json() or []):
                uuid = item.get("uuid")
                if uuid and uuid not in found:
                    found[uuid] = item
        return sorted(found.values(), key=lambda i: i.get("startDate") or "",
                      reverse=True)

    async def get_adhoc_challenge(self, uuid: str) -> dict:
        """One social challenge including its leaderboard (`players`). The
        list endpoint returns players=[]; only this one fills it."""
        r = await self._http.get(
            f"{CONNECT_API}/adhocchallenge-service/adHocChallenge/{uuid}",
            headers=await self._bearer())
        if r.status_code == 404:
            raise GarminError(f"challenge {uuid} not found")
        if r.status_code != 200:
            raise GarminError(f"challenge {uuid} HTTP {r.status_code}")
        return r.json() or {}

    async def personal_records(self) -> list[dict]:
        """All-time personal records (typeId per record kind, value in seconds
        or metres depending on the kind)."""
        r = await self._http.get(
            f"{CONNECT_API}/personalrecord-service/personalrecord/prs/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"personal records HTTP {r.status_code}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def race_predictions(self) -> dict:
        """Garmin's current 5k/10k/half/marathon predictions, in seconds."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/racepredictions/latest/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def fitness_age(self, day: str) -> dict:
        r = await self._http.get(f"{CONNECT_API}/fitnessage-service/fitnessage/{day}",
                                 headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def endurance_score(self, day: str) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/endurancescore?calendarDate={day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def hill_score(self, day: str) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/hillscore?calendarDate={day}",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def lifetime_totals(self) -> dict:
        r = await self._http.get(
            f"{CONNECT_API}/userstats-service/statistics/"
            f"{await self.profile_display_id()}", headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def steps_range(self, start: str, end: str) -> list[dict]:
        r = await self._http.get(
            f"{CONNECT_API}/usersummary-service/stats/steps/daily/{start}/{end}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def body_battery_range(self, start: str, end: str) -> list[dict]:
        """Per day charged/drained. The response also carries the full intraday
        curve; callers are expected to drop it."""
        r = await self._http.get(
            f"{CONNECT_API}/wellness-service/wellness/bodyBattery/reports/daily",
            params={"startDate": start, "endDate": end}, headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def vo2max_range(self, start: str, end: str) -> list[dict]:
        """maxmet history: one entry per day, `generic` = running, `cycling`
        separate, either may be null."""
        r = await self._http.get(
            f"{CONNECT_API}/metrics-service/metrics/maxmet/daily/{start}/{end}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def planned_workouts(self, limit: int = 20) -> list[dict]:
        """The structured workouts stored in the account."""
        r = await self._http.get(f"{CONNECT_API}/workout-service/workouts",
                                 params={"start": 1, "limit": limit},
                                 headers=await self._bearer())
        if r.status_code != 200:
            raise GarminError(f"workouts HTTP {r.status_code}")
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    async def calendar_month(self, year: int, month: int) -> list[dict]:
        """calendarItems of one month. month is 1-12 here; Garmin's own path
        counts months from 0, which this method hides."""
        r = await self._http.get(
            f"{CONNECT_API}/calendar-service/year/{year}/month/{month - 1}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("calendarItems") or []

    async def activity_weather(self, activity_id: int) -> dict:
        """Weather at the time of the activity. Garmin answers in Fahrenheit
        and mph here regardless of account settings."""
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}/weather",
            headers=await self._bearer())
        return (r.json() or {}) if r.status_code == 200 else {}

    async def activity_time_in_zones(self, activity_id: int, kind: str = "hr") -> list[dict]:
        """Time per heart rate or power zone. Empty for activities recorded
        without the matching sensor, which is not an error."""
        path = "hrTimeInZones" if kind == "hr" else "powerTimeInZones"
        r = await self._http.get(
            f"{CONNECT_API}/activity-service/activity/{activity_id}/{path}",
            headers=await self._bearer())
        if r.status_code != 200:
            return []
        arr = r.json() or []
        return arr if isinstance(arr, list) else []

    # ---------- courses and devices (endpoints tried live 2026-10-27) ----------
    # Errors with a status are GarminHttpError: callers tell a foreign private
    # course (401/403) or "not yet ready" (429, right after saving) apart.

    @staticmethod
    def _check(r: httpx.Response, what: str, ok=(200,)) -> httpx.Response:
        if r.status_code not in ok:
            raise GarminHttpError(f"{what} HTTP {r.status_code}: {r.text[:120]}".strip(),
                                  r.status_code)
        return r

    async def list_courses(self) -> list[dict]:
        """The user's own courses - WITHOUT their description (Garmin answers
        courseDescription null here); that comes only with get_course."""
        name = await self.profile_display_id()
        if not name:
            raise GarminError("no displayName in the profile")
        r = await self._http.get(f"{CONNECT_API}/course-service/course/owner/{name}",
                                 headers=await self._bearer())
        arr = self._check(r, "list_courses").json() or []
        return arr if isinstance(arr, list) else []

    async def get_course(self, course_id: int | str) -> dict | None:
        """A course (own or public) with name, description and activityTypePk;
        None if it is gone (404)."""
        r = await self._http.get(f"{CONNECT_API}/course-service/course/{course_id}",
                                 headers=await self._bearer())
        if r.status_code == 404:
            return None
        self._check(r, "get_course")
        return (r.json() if r.content else None) or {}

    async def course_gpx(self, course_id: int | str) -> bytes:
        """The course as a GPX file."""
        r = await self._http.get(f"{CONNECT_API}/course-service/course/gpx/{course_id}",
                                 headers=await self._bearer())
        return self._check(r, "course_gpx").content

    async def import_course(self, data: bytes, filename: str = "route.gpx") -> dict:
        """Garmin parses a GPX into a course and saves NOTHING (save_course
        does). Its geoPoints come back without elevation."""
        r = await self._http.post(f"{CONNECT_API}/course-service/course/import",
                                  headers=await self._bearer(),
                                  files={"file": (filename, data, "application/gpx+xml")})
        return self._check(r, "import_course").json() or {}

    async def save_course(self, course: dict) -> int:
        """Saves a course (e.g. import_course's answer, completed). Returns the
        courseId."""
        r = await self._http.post(f"{CONNECT_API}/course-service/course",
                                  headers=await self._bearer(content_type=True),
                                  content=json.dumps(course))
        course_id = (self._check(r, "save_course", ok=(200, 201)).json() or {}).get("courseId")
        if not isinstance(course_id, int):
            raise GarminError("save_course: no courseId in the answer")
        return course_id

    async def delete_course(self, course_id: int) -> None:
        """Deletes a course (404 = already gone). Right after it was saved
        Garmin answers 429 "not yet ready to delete or update" for a moment -
        GarminHttpError with status 429, the caller decides when to try again."""
        r = await self._http.delete(f"{CONNECT_API}/course-service/course/{course_id}",
                                    headers=await self._bearer())
        self._check(r, "delete_course", ok=(200, 204, 404))

    async def list_devices(self) -> list[dict]:
        """The user's registered devices (deviceId, productDisplayName,
        courseCapable, deviceCategories - a bike computer says
        DEDICATED_BIKE_COMPUTER)."""
        r = await self._http.get(f"{CONNECT_API}/device-service/deviceregistration/devices",
                                 headers=await self._bearer())
        arr = self._check(r, "list_devices").json() or []
        return arr if isinstance(arr, list) else []

    async def send_course(self, course_id: int, device_id: int, name: str) -> None:
        """Queues the course for the device, like "Send to device" in Garmin
        Connect: the device fetches the FIT on its next sync. There is no
        message that removes a course from a device."""
        r = await self._http.post(
            f"{CONNECT_API}/device-service/devicemessage/messages",
            headers=await self._bearer(content_type=True), content=json.dumps([{
                "deviceId": device_id,
                "messageUrl": f"course-service/course/fit/{course_id}/{device_id}?elevation=true",
                "messageType": "courses", "messageName": name[:255],
                "groupName": None, "priority": 1, "fileType": "FIT",
                "metaDataId": course_id}]))
        self._check(r, "send_course", ok=(200, 201, 204))


def _gmt_s(value: str) -> int:
    """Garmin's GMT timestamp ("2026-10-16T13:45:00.0") as Unix seconds."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    return int(_dt.fromisoformat(value[:19]).replace(tzinfo=_tz.utc).timestamp())
