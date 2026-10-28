import datetime as dt

import pytest
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import FileType, Manufacturer, Sport

BASE = dt.datetime(2026, 8, 20, 6, 30, 0, tzinfo=dt.timezone.utc)
BASE_MS = int(BASE.timestamp() * 1000)


def build_fit(n=600, speed_mps=3.2, hr0=120) -> bytes:
    """A plain one-session run: n records at 1 Hz, one lap, one session."""
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.time_created = BASE_MS
    b.add(fid)
    for i in range(n):
        r = RecordMessage()
        r.timestamp = BASE_MS + i * 1000
        r.distance = float(i) * speed_mps
        r.speed = speed_mps
        r.heart_rate = hr0 + (i % 40)
        r.power = 200 + (i % 50)
        r.altitude = 100.0 + (i % 20)
        b.add(r)
    lap = LapMessage()
    lap.start_time = BASE_MS
    lap.timestamp = BASE_MS + (n - 1) * 1000
    lap.total_elapsed_time = float(n - 1)
    lap.total_timer_time = float(n - 1)
    lap.total_distance = (n - 1) * speed_mps
    b.add(lap)
    s = SessionMessage()
    s.sport = Sport.RUNNING.value
    s.start_time = BASE_MS
    s.timestamp = BASE_MS + (n - 1) * 1000
    s.total_elapsed_time = float(n - 1)
    s.total_timer_time = float(n - 1)
    s.total_distance = (n - 1) * speed_mps
    s.avg_heart_rate = 139
    s.max_heart_rate = 159
    s.avg_power = 224
    s.max_power = 249
    s.avg_speed = speed_mps
    s.max_speed = speed_mps
    b.add(s)
    return bytes(b.build().to_bytes())


@pytest.fixture(scope="session")
def fit_bytes() -> bytes:
    return build_fit()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may read or write the real ~/.garmin-mcp or ~/.config/garmin-mcp."""
    monkeypatch.setenv("GARMIN_MCP_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("GARMIN_MCP_TOKENS", str(tmp_path / "config" / "tokens.json"))
    monkeypatch.setenv("MCP_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("MYAUTH_APP_KEY", "test-app-key")
    from garmin_mcp import db
    db.init()
    return tmp_path


class FakeMyAuth:
    """Stands in for MyAuth: which browser cookie belongs to whom, who may use
    the app, and whose Garmin is connected there (uid -> access token). No
    test talks to a real MyAuth."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.cookies: dict[str, int] = {}
        self.allowed: set[int] = set()
        self.garmin: dict[int, dict] = {}
        self.garmin_refused: str | None = None
        self.down = False

    def connect_garmin(self, uid: int, access_token: str, account: str = "") -> None:
        import time
        self.garmin[uid] = {"access_token": access_token, "account": account,
                            "expires_at": int(time.time()) + 3600}

    def add(self, uid: int, username: str, display_name: str = "",
            cookie: str | None = None, allowed: bool = True) -> int:
        self.users[uid] = {"uid": uid, "username": username,
                           "display_name": display_name or username, "is_admin": False}
        if cookie:
            self.cookies[cookie] = uid
        if allowed:
            self.allowed.add(uid)
        return uid

    async def session(self, cookie):
        from garmin_mcp.access import myauth
        if self.down:
            raise myauth.Unavailable("down")
        uid = self.cookies.get(cookie)
        if uid is None:
            return 401, {"detail": "not_authenticated"}
        if uid not in self.allowed:
            return 403, {"detail": "app_not_allowed"}
        return 200, {**self.users[uid], "apps": ["mcp-garmin"], "language": None}

    async def access(self, uid):
        from garmin_mcp.access import myauth
        if self.down:
            raise myauth.Unavailable("down")
        if uid in self.users and uid in self.allowed:
            return 200, dict(self.users[uid])
        return 403, {"detail": "not_allowed"}

    async def garmin_token(self, uid):
        from garmin_mcp.access import myauth
        if self.down:
            raise myauth.Unavailable("down")
        if self.garmin_refused:
            return 502, {"detail": "garmin_refused", "message": self.garmin_refused}
        if uid in self.garmin:
            return 200, dict(self.garmin[uid])
        return 404, {"detail": "not_connected"}


@pytest.fixture(autouse=True)
def fake_myauth(monkeypatch):
    from garmin_mcp.access import myauth
    fake = FakeMyAuth()
    monkeypatch.setenv("MYAUTH_URL", "http://myauth.test")
    monkeypatch.setattr(myauth, "_fetch_session", fake.session)
    monkeypatch.setattr(myauth, "_fetch_access", fake.access)
    monkeypatch.setattr(myauth, "_fetch_garmin", fake.garmin_token)
    myauth.clear_cache()
    yield fake
    myauth.clear_cache()


PUBLIC = "https://garmin.example"


@pytest.fixture
def client(monkeypatch):
    """The HTTP app as the browser sees it behind the reverse proxy."""
    from starlette.testclient import TestClient

    from garmin_mcp.server import build_http_app
    monkeypatch.setenv("PUBLIC_URL", PUBLIC)
    with TestClient(build_http_app(), base_url=PUBLIC) as c:
        yield c


@pytest.fixture
def user_id(fake_myauth):
    """Anja: a MyAuth user who may use the app, known to this server."""
    from garmin_mcp import users
    fake_myauth.add(7, "anja", "Anja", cookie="anja-cookie")
    users.ensure_user(7, "anja", "Anja")
    return 7


@pytest.fixture
def anja(client, user_id):
    """A browser signed in to MyAuth as Anja."""
    client.cookies.set("sso_session", "anja-cookie")
    return client
