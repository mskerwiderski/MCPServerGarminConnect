"""Keeping Garmin sign-ins and tokens for many users - the part around
`signin` that MyAuth and MyGPXContainer's own accounts both need.

Still nothing about storage: where the tokens live (a database row, a file)
is the app's. This module holds what was written twice before (v1.1.0):

- `SignIns`: a sign-in waiting for its MFA code, per key (a user id),
  across two web requests, at most `ttl_s`. Browsers that fill in one-time
  codes by themselves often submit the form twice (autofill plus the click):
  the second submission waits for the first and, if that one signed in
  within `grace_s`, counts as done (`submit` returns None) instead of
  sending the spent code again and reporting a rejection.
- `Sealer`: tokens as encrypted text (Fernet). `Sealer.from_secret(text)`
  derives the key from any secret like MyAuth always did
  (sha256(b"garmin-tokens:" + secret)), `Sealer(fernet_key)` takes a
  ready Fernet key. `cryptography` is imported only here.
- `Renewer`: the OAuth2 token, renewed first when it is due - one renewal at
  a time per key, reading the stored tokens again inside the lock so a
  second caller gets the first one's result instead of renewing again.

Usage:

    signins = SignIns()
    tokens = signins.start(uid, email, password)   # None: Garmin wants the code
    tokens = signins.submit(uid, code)              # None: a twin already did it
    # store tokens["oauth1"], tokens["oauth2"] (sealed), tokens["account"]

    oauth2 = Renewer().fresh(uid, load=lambda: (oauth1, oauth2), save=store_oauth2)

All of it is synchronous and thread-safe; async callers use asyncio.to_thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Callable

from . import signin
from .signin import GarminError

PENDING_TTL_S = 10 * 60          # how long a sign-in waits for the MFA code
MFA_GRACE_S = 5 * 60             # a twin submission after success counts as done


class SignIns:
    """Sign-ins waiting for their MFA code, per key. `factory` makes a
    `signin.SignIn` (the tests hand in a fake)."""

    def __init__(self, ttl_s: float = PENDING_TTL_S, grace_s: float = MFA_GRACE_S,
                 factory: Callable[[], signin.SignIn] | None = None) -> None:
        self.ttl_s, self.grace_s = ttl_s, grace_s
        self._factory = factory or signin.SignIn
        self._pending: dict = {}             # key -> (started, SignIn)
        self._done: dict = {}                # key -> when a sign-in last succeeded
        self._lock = threading.Lock()
        self._key_locks: dict = {}

    def _drop(self, key) -> None:
        with self._lock:
            hit = self._pending.pop(key, None)
        if hit is not None:
            hit[1].close()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.ttl_s
        for key, (started, _) in list(self._pending.items()):
            if started < cutoff:
                self._drop(key)

    def waiting(self, key) -> bool:
        """A sign-in of `key` waits for its MFA code."""
        self._prune()
        return key in self._pending

    def start(self, key, email: str, password: str) -> dict | None:
        """Tokens when signed in, None when Garmin wants the MFA code first. A
        new start drops a sign-in still waiting for its code, even if the new
        one fails. GarminError: a text for the user."""
        if not email.strip() or not password:
            raise GarminError("Enter your Garmin e-mail and password.")
        self._prune()
        self._drop(key)
        attempt = self._factory()
        try:
            tokens = attempt.start(email, password)
        except Exception:
            attempt.close()
            raise
        if tokens is None:
            with self._lock:
                self._pending[key] = (time.monotonic(), attempt)
            return None
        attempt.close()
        self._done[key] = time.monotonic()
        return tokens

    def submit(self, key, code: str) -> dict | None:
        """Tokens once Garmin accepts the code; None when a twin submission
        signed in moments ago (nothing to store). A wrong or stale code raises
        GarminError and keeps the sign-in open for the next code; any other
        failure ends it."""
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            self._prune()
            with self._lock:
                hit = self._pending.pop(key, None)
            if hit is None:
                if time.monotonic() - self._done.get(key, -1e12) < self.grace_s:
                    return None
                raise GarminError("The sign-in timed out. Please start again.")
            started, attempt = hit
            try:
                tokens = attempt.submit_mfa(code)
            except Exception:
                if attempt.waiting_for_mfa:            # wrong or stale code: stays open
                    with self._lock:
                        self._pending[key] = (started, attempt)
                else:
                    attempt.close()
                raise
            attempt.close()
            self._done[key] = time.monotonic()
            return tokens

    def cancel(self, key) -> None:
        """Drop a sign-in that waits for its code."""
        self._drop(key)

    def clear(self) -> None:
        """Drop everything (tests)."""
        for key in list(self._pending):
            self._drop(key)
        self._done.clear()


class Sealer:
    """Tokens as encrypted text. `open` gives None for text this key cannot
    read (another key, damaged)."""

    def __init__(self, fernet_key: bytes) -> None:
        from cryptography.fernet import Fernet
        self._fernet = Fernet(fernet_key)

    @classmethod
    def from_secret(cls, secret: str) -> "Sealer":
        digest = hashlib.sha256(b"garmin-tokens:" + secret.encode()).digest()
        return cls(base64.urlsafe_b64encode(digest))

    def seal(self, data: dict) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def open(self, text: str) -> dict | None:
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(self._fernet.decrypt(text.encode()))
        except (InvalidToken, ValueError):
            return None


class Renewer:
    """The OAuth2 token of a key, renewed when it is due. `renew` is
    `signin.renew` (the tests hand in a fake)."""

    def __init__(self, renew: Callable[[dict], dict] | None = None,
                 margin_s: float = signin.REFRESH_MARGIN_S) -> None:
        self._renew = renew or signin.renew
        self.margin_s = margin_s
        self._lock = threading.Lock()
        self._key_locks: dict = {}

    def fresh(self, key, load: Callable[[], tuple[dict, dict | None]],
              save: Callable[[dict], None]) -> dict:
        """`load()` -> (oauth1, oauth2) as stored now; `save(oauth2)` keeps a
        renewed one. Returns the OAuth2 token to use. GarminError when Garmin
        refuses (its `__cause__` is an httpx error when the network failed)."""
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            oauth1, oauth2 = load()
            if not signin.needs_renewal(oauth2, self.margin_s):
                return oauth2
            oauth2 = self._renew(oauth1)
            save(oauth2)
            return oauth2
