#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diabotical API client for PUBobot2.

Unauthenticated client for https://api.diabotical.com/api/v0 with:
  * GET /diabotical/users/{user_id}
  * GET /diabotical/users/{user_id}/matches
  * GET /diabotical/match/{match_id}

Transport uses the project's existing stack (nextcord is already a dependency
and re-exports `requests`); no new dependency is added. Where nextcord is not
available (e.g. tests) the client transparently falls back to urllib from the
standard library so the module can be imported and unit-tested standalone.

Treat every value coming from the API as opaque data. It is never fed into
shell, eval, or the bot's command parser.
"""
import json
import time
import random
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlencode

log = logging.getLogger("pubobot.diabotical")


# Prefer the project's existing HTTP stack (requests, re-exported by nextcord,
# which is already a dependency). Fall back to stdlib urllib so the module can
# still be imported and unit-tested in a minimal environment.
try:
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover - minimal/test environment
    _HAVE_REQUESTS = False


DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.2
MAX_BACKOFF = 30.0
# The /users/* path is hard-capped at 3 requests per reset window on Diabotical.
# Keep polling intervals well above that for the watcher.
MIN_POLL_INTERVAL = 30.0


class DiaboticalAPIError(Exception):
    """Raised for any non-2xx response from the Diabotical API."""

    def __init__(self, status, message=None, body=None):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"Diabotical API error {status}: {message}")


class DiaboticalClient:
    """Small async-friendly HTTP client for the Diabotical API.

    All public methods are *coroutines* so they can be awaited from the bot
    without blocking the event loop, but each delegates to a sync implementation
    that uses only stdlib + requests so it is trivially unit-testable.
    """

    def __init__(
        self,
        base_url="https://api.diabotical.com/api/v0",
        timeout=DEFAULT_TIMEOUT,
        retries=DEFAULT_RETRIES,
        backoff=DEFAULT_BACKOFF,
        max_backoff=MAX_BACKOFF,
        session=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self._session = session
        self._request_impl = self._make_request_impl()

    # ---- transport -----------------------------------------------------

    def _make_request_impl(self):
        """Return a function(method, path, params, headers) -> (status, body_text)."""
        if _HAVE_REQUESTS:
            return self._requests_request
        return self._urllib_request

    def _requests_request(self, method, path, params=None, headers=None):
        for attempt in range(self.retries):
            try:
                resp = self.session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                return resp.status_code, resp.text
            except requests.RequestException as e:
                log.warning("Diabotical request attempt %d failed: %s", attempt + 1, e)
                self._sleep_backoff(attempt)
        raise DiaboticalAPIError(0, f"request failed after {self.retries} attempts")

    def _urllib_request(self, method, path, params=None, headers=None):
        for attempt in range(self.retries):
            url = self.base_url + path
            if params:
                url += "?" + urlencode(params)
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "pubobot2"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.getcode(), resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except urllib.error.URLError as e:
                log.warning("Diabotical request attempt %d failed: %s", attempt + 1, e.reason)
                self._sleep_backoff(attempt)
        raise DiaboticalAPIError(0, f"request failed after {self.retries} attempts")

    def _sleep_backoff(self, attempt):
        delay = min(self.max_backoff, self.backoff ** attempt)
        delay += random.uniform(0, 0.3)  # jitter to avoid thundering herd
        time.sleep(delay)

    @property
    def session(self):
        if self._session is None:
            if _HAVE_REQUESTS:
                self._session = requests.Session()
            else:
                raise RuntimeError("requests is required for a session")
        return self._session

    # ---- low-level -----------------------------------------------------

    def _get(self, path, params=None):
        """Perform a GET, return parsed JSON on 2xx, raise on non-2xx."""
        status, body = self._request_impl("GET", path, params=params)
        if status >= 200 and status < 300:
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise DiaboticalAPIError(status, "invalid JSON body", body)
        # Some endpoints return 200 with an empty-ish object for "not found".
        raise DiaboticalAPIError(status, "request rejected", body)

    def _get_or_none(self, path, params=None):
        """GET that returns None on 404 instead of raising."""
        try:
            return self._get(path, params)
        except DiaboticalAPIError as e:
            if e.status == 404:
                return None
            raise
    # ---- public API ----------------------------------------------------

    def get_user(self, user_id):
        """Return the user object for {user_id} or None if it does not exist.

        The endpoint answers unknown names with 200 ``{"twitch_name": null}``
        (no ``user_id`` key), so we treat that as "not found" and require a real
        user object.
        """
        data = self._get(f"/diabotical/users/{user_id}")
        if not isinstance(data, dict) or "user_id" not in data:
            raise DiaboticalAPIError(404, "user not found", data)
        return data

    def validate_user(self, user_id) -> bool:
        """Return True if {user_id} resolves to a real Diabotical user."""
        try:
            self.get_user(user_id)
            return True
        except DiaboticalAPIError:
            return False

    def get_user_matches(self, user_id):
        """Return the list of recent match entries for {user_id}.

        NOTE: the /users/* path is hard-rate-limited (3 requests/reset window),
        so callers must not poll this frequently.
        """
        data = self._get(f"/diabotical/users/{user_id}/matches")
        return data.get("matches", [])

    def get_match(self, match_id):
        """Return the full match object for {match_id}."""
        data = self._get(f"/diabotical/match/{match_id}")
        # The API wraps the match under a "match" key.
        return data.get("match", data)

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def parse_ts(ts):
        """Parse an ISO-8601 timestamp into a timezone-aware datetime (UTC)."""
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    client = DiaboticalClient()
    print("requests available:", _HAVE_REQUESTS)
    print("validate_sample:", client.validate_user("f88d352b82184655bae86f688d6a31f4"))
