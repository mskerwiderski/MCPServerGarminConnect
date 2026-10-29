"""AI runs through MyAuth, for the apps that let MyAuth hold every AI access.

MyAuth keeps the Claude subscription, the API keys and the AI call
statistics; an app never holds a token. Two endpoints, both with the app's
key (`MYAUTH_APP_KEY`, see `myauth.app_secret`) where needed:

- `GET /api/ai/models` (no key) answers `{"providers": [{"key", "name",
  "billing", "live", "models": [{"id", "display_name", "capabilities":
  {"pdf", "image", "schema", ...}, ...}]}]}`, newest models first. Cached
  CACHE_TTL_S; when MyAuth does not answer, the last list keeps working.
- `POST /api/internal/ai/run` (bearer = app key, Docker network only),
  multipart: `app`, `uid` (the MyAuth user the run is for), `provider`,
  `model` (the exact listed id), `task`, `prompt`, `reason`, optional
  `json_schema` and `timeout_s` (MyAuth's default is 300 s), files as
  `files`. 200 answers the provider's result (`result`,
  `structured_output`, `usage`, `is_error`, `provider`, `model`, ...); else
  `{"detail": <code>, "message": ...}` with 400 (`model_not_available`,
  `model_not_capable`, `file_*`), 401 (unknown app key), 402 (no credit),
  403 (user without the app), 413 (`file_too_large`), 429, 502, 503 (no
  connection), 504.

`run` falls back once: when a paid provider answers 402 or 503, the app's
`fallback(provider, model)` may name a subscription model (`ABO`); a paid one
is never taken. Errors carry MyAuth's status and detail code, no wording -
the texts for the user stay in the apps. `_fetch_models`,
`_fetch_models_sync` and `_post_run` are the mock points of the tests.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time

import httpx

from . import myauth

log = logging.getLogger("access.ai")

ABO = "claude-abo"            # the Claude subscription, MyAuth's provider key
CACHE_TTL_S = 600
DEFAULT_TIMEOUT_S = 300       # MyAuth stops a run after this unless asked for more
RUN_GRACE_S = 30              # ... and answers a little later
MODEL_ERRORS = ("model_not_available", "model_not_capable")
FILE_ERRORS = ("file_too_large", "file_type_not_supported", "file_unreadable")

_cache: tuple[float, dict] | None = None   # (fetched at, /api/ai/models answer)


class AiError(RuntimeError):
    """A run or the model list failed; `status` is MyAuth's HTTP status (0 =
    MyAuth did not answer), `detail` its code, `message` its text."""

    def __init__(self, status: int, detail: str = "", message: str = ""):
        super().__init__(f"MyAuth AI: HTTP {status} {detail} {message}".strip())
        self.status, self.detail, self.message = status, detail, message


class AiUnavailable(AiError):
    """No usable AI right now: MyAuth unreachable, 401/402/403/503."""


class AiRejected(AiError):
    """MyAuth refused the request: the model (400) or a file (400/413)."""


class AiFailed(AiError):
    """The run itself failed: 502, 504, 429, an error answer, anything else."""


def enabled() -> bool:
    """AI needs MyAuth's address and the app key."""
    return bool(myauth.auth_url() and myauth.app_secret())


# ---------- models ----------

async def _fetch_models() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{myauth.auth_url()}/api/ai/models")
    except httpx.HTTPError as e:
        raise AiUnavailable(0, "unreachable") from e
    if r.status_code != 200:
        raise AiUnavailable(r.status_code, "http")
    return r.json()


def _fetch_models_sync() -> dict:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{myauth.auth_url()}/api/ai/models")
    except httpx.HTTPError as e:
        raise AiUnavailable(0, "unreachable") from e
    if r.status_code != 200:
        raise AiUnavailable(r.status_code, "http")
    return r.json()


def _fresh() -> dict | None:
    if _cache and time.monotonic() - _cache[0] < CACHE_TTL_S:
        return _cache[1]
    return None


def _remember(answer: dict) -> dict:
    global _cache
    _cache = (time.monotonic(), answer)
    return answer


def _stale(err: AiUnavailable) -> dict:
    if _cache:
        log.warning("MyAuth unavailable, using cached model list")
        return _cache[1]
    raise err


async def models() -> dict:
    """MyAuth's model answer. Raises AiUnavailable when neither MyAuth nor
    the cache helps."""
    hit = _fresh()
    if hit is not None:
        return hit
    try:
        return _remember(await _fetch_models())
    except AiUnavailable as e:
        return _stale(e)


def models_sync() -> dict:
    """The same for synchronous callers; shares the cache."""
    hit = _fresh()
    if hit is not None:
        return hit
    try:
        return _remember(_fetch_models_sync())
    except AiUnavailable as e:
        return _stale(e)


def clear_models() -> None:
    global _cache
    _cache = None


def cached() -> dict | None:
    """The last model answer, however old (None before the first)."""
    return _cache[1] if _cache else None


# ---------- model rules ----------

def base_id(model_id: str | None) -> str:
    """'claude-haiku-4-5-20251001' -> 'claude-haiku-4-5' (API ids carry a date)."""
    return re.sub(r"-\d{8}$", "", model_id or "")


def recommended(ids: list[str]) -> str:
    """Newest Sonnet (MyAuth lists newest first), else the newest model."""
    return next((i for i in ids if "sonnet" in i), ids[0])


def usable(provider: dict, needs: set[str] | frozenset[str]) -> list[dict]:
    """Models of a provider that have every capability in `needs`."""
    return [m for m in provider.get("models") or []
            if set(needs) <= {k for k, ok in (m.get("capabilities") or {}).items() if ok}]


def exact_id(provider_key: str, model: str) -> str:
    """The id under which MyAuth lists `model` for that provider ('claude-haiku-4-5'
    is stored, 'claude-haiku-4-5-20251001' is listed); `model` itself when the
    list does not know it."""
    answer = cached() or {}
    entry = next((p for p in answer.get("providers") or [] if p.get("key") == provider_key), {})
    ids = [m["id"] for m in entry.get("models") or []]
    return next((i for i in ids if base_id(i) == base_id(model)), model)


# ---------- runs ----------

async def _post_run(data: dict, files: list[tuple[str, bytes]], timeout: float) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{myauth.auth_url()}/api/internal/ai/run", data=data,
                                  files=[("files", (name, content)) for name, content in files],
                                  headers={"authorization": f"Bearer {myauth.app_secret()}"})
    except httpx.HTTPError as e:
        raise AiUnavailable(0, "unreachable") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


async def run(prompt: str, *, uid: int, provider: str, model: str, task: str,
              reason: str = "", json_schema: str | dict | None = None,
              files: list[tuple[str, bytes]] | tuple = (), timeout_s: int | None = None,
              fallback=None) -> dict:
    """One run in MyAuth for user `uid`; returns the provider's answer (see
    the module docstring). `reason` and `task` show up in MyAuth's AI call
    statistics; `timeout_s` asks for another limit than MyAuth's 300 s.
    `fallback(provider, model)` (plain or async) is asked once when a paid
    provider answers 402/503 and may return (ABO, model) or None."""
    data = {"app": myauth.app_key(), "uid": str(uid), "provider": provider,
            "model": exact_id(provider, model), "task": task, "prompt": prompt,
            "reason": reason}
    if json_schema is not None:
        data["json_schema"] = json_schema if isinstance(json_schema, str) else json.dumps(json_schema)
    if timeout_s is not None:
        data["timeout_s"] = str(timeout_s)
    timeout = (timeout_s or DEFAULT_TIMEOUT_S) + RUN_GRACE_S
    files = list(files)
    status, body = await _post_run(data, files, timeout)
    if status in (402, 503) and provider != ABO and fallback is not None:
        clear_models()                      # reload: the provider shows up as not live
        choice = fallback(provider, model)
        if inspect.isawaitable(choice):
            choice = await choice
        if choice and choice[0] == ABO:     # never into a paid API
            log.warning("%s failed (%s), retrying with the Claude subscription", provider, status)
            fb_model = exact_id(ABO, choice[1])
            status, body = await _post_run({**data, "provider": ABO, "model": fb_model},
                                           files, timeout)
    if status == 200:
        return body
    detail, message = str(body.get("detail") or ""), str(body.get("message") or "")
    if status == 400 and detail in MODEL_ERRORS:
        clear_models()                      # the next run reloads the list
        raise AiRejected(status, detail, message)
    if status in (400, 413) and detail in FILE_ERRORS:
        raise AiRejected(status, detail, message)
    if status in (0, 401, 402, 403, 503):
        raise AiUnavailable(status, detail, message)
    raise AiFailed(status, detail, message)


def parse(answer: dict) -> tuple[dict, dict]:
    """A run's answer -> (payload, usage). `structured_output` (json_schema)
    preferred, else the result text as JSON (code fences tolerated). Usage:
    input tokens including the cache tokens, output tokens. An error answer
    raises AiFailed(200, "is_error", <result>); text that is no JSON raises
    ValueError."""
    if answer.get("is_error"):
        raise AiFailed(200, "is_error", str(answer.get("result"))[:300])
    usage = answer.get("usage") or {}
    tokens_in = usage.get("input_tokens")
    if tokens_in is not None:               # cache tokens count as input
        tokens_in += (usage.get("cache_creation_input_tokens") or 0)
        tokens_in += (usage.get("cache_read_input_tokens") or 0)
    meta = {"input_tokens": tokens_in, "output_tokens": usage.get("output_tokens")}
    structured = answer.get("structured_output")
    if isinstance(structured, dict):
        return structured, meta
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (answer.get("result") or "").strip())
    return json.loads(text), meta
