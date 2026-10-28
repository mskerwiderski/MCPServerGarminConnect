"""Challenges come from two endpoints, and that is the whole point of the file.

`historical` returns only finished challenges. The challenge running this month
is exclusively under `active`, which is why asking one endpoint made the current
month look like it did not exist.
"""
import time

import pytest

from garmin_mcp import project
from garmin_mcp.garmin_api.client import GarminClient, OAuth2Token

ACTIVE = [{"uuid": "AUG", "adHocChallengeName": "Lauf-Challenge",
           "socialChallengeActivityTypeId": 1, "socialChallengeStatusId": 1,
           "startDate": "2026-08-01T00:00:00.0", "endDate": "2026-08-31T23:59:59.0",
           "userRanking": 5, "playerCount": 0, "players": []}]

HISTORICAL = [{"uuid": "JUL", "adHocChallengeName": "Lauf-Challenge",
               "socialChallengeActivityTypeId": 1, "socialChallengeStatusId": 4,
               "startDate": "2026-07-01T00:00:00.0", "endDate": "2026-07-31T23:59:59.0",
               "userRanking": 3, "playerCount": 11, "players": []},
              # Garmin repeats the running one here on some accounts.
              dict(ACTIVE[0])]


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    c = GarminClient()
    c.oauth2_token = OAuth2Token("access", "refresh", 3600, time.time() + 3600)
    calls = []

    async def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/active"):
            return FakeResponse(ACTIVE)
        if url.endswith("/historical"):
            return FakeResponse(HISTORICAL)
        return FakeResponse({}, 404)

    monkeypatch.setattr(c._http, "get", fake_get)
    c.calls = calls
    return c


async def _list(c):
    return await c.list_adhoc_challenges()


def test_both_endpoints_are_asked(client):
    import asyncio
    rows = asyncio.run(_list(client))
    assert [u.rsplit("/", 1)[-1] for u in client.calls] == ["active", "historical"]
    assert [r["uuid"] for r in rows] == ["AUG", "JUL"]      # newest first


def test_the_running_challenge_is_not_duplicated(client):
    import asyncio
    rows = asyncio.run(_list(client))
    assert len(rows) == 2


def test_a_broken_active_endpoint_does_not_lose_the_history(monkeypatch):
    import asyncio
    c = GarminClient()
    c.oauth2_token = OAuth2Token("access", "refresh", 3600, time.time() + 3600)

    async def fake_get(url, **kw):
        return FakeResponse([], 500) if url.endswith("/active") else FakeResponse(HISTORICAL)

    monkeypatch.setattr(c._http, "get", fake_get)
    assert {r["uuid"] for r in asyncio.run(_list(c))} == {"AUG", "JUL"}


def test_state_comes_from_the_dates(monkeypatch):
    import datetime

    from garmin_mcp import session
    monkeypatch.setattr(session, "today", lambda: datetime.date(2026, 8, 26))
    assert project.challenge_summary(ACTIVE[0])["state"] == "running"
    assert project.challenge_summary(HISTORICAL[0])["state"] == "finished"


def test_a_running_challenge_hides_its_bogus_player_count():
    """Garmin reports playerCount 0 while a challenge runs - showing "0 players"
    would be worse than showing nothing."""
    assert "players" not in project.challenge_summary(ACTIVE[0])
    assert project.challenge_summary(HISTORICAL[0])["players"] == 11


def test_the_detail_call_supplies_the_real_count():
    detail = dict(ACTIVE[0], players=[
        {"ranking": 1, "fullName": "Lisa", "totalNumber": 192700.0,
         "displayName": "x", "lastSyncTime": "2026-08-26T06:00:00.0"},
        {"ranking": 2, "fullName": "Max", "totalNumber": 107210.0,
         "displayName": "me", "lastSyncTime": "2026-08-26T07:00:00.0"}])
    view = project.challenge_detail(detail, "me")
    assert view["players"] == 2
    assert view["leaderboard"][0]["total_km"] == 192.7
    assert view["leaderboard"][1]["is_you"] is True
