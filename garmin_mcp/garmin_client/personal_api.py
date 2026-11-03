"""Client for Garmin's official Connect Personal API
(https://developer.garmin.com/gc-developer-program/personal/).

It serves the same internal Connect endpoints `garminconnect` scrapes, so
responses have the same shapes and the existing row mappers apply
unchanged. Method names mirror `garminconnect.Garmin` for the same reason.

Auth: the long-lived personal API key (max one year) is exchanged for a
24-hour bearer token. There are no refresh tokens; a new token is just
requested from the key again.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

# Renew an hour early so a long backfill never sends an expired token.
_TOKEN_TTL_SECONDS = 23 * 3600


class PersonalApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        time_fn: Callable[[], float] = time.time,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._http = http or httpx.Client(timeout=60)
        self._time_fn = time_fn
        self._token: str | None = None
        self._token_fetched_at = 0.0

    def _fetch_token(self) -> str:
        response = self._http.post(
            self._base_url + TOKEN_PATH, headers={"Authorization": f"ApiKey {self._api_key}"}
        )
        response.raise_for_status()
        # The endpoint returns the bare JWT as text, not JSON.
        self._token = response.text.strip().strip('"')
        self._token_fetched_at = self._time_fn()
        return self._token

    def _bearer(self) -> str:
        if self._token is None or self._time_fn() - self._token_fetched_at > _TOKEN_TTL_SECONDS:
            return self._fetch_token()
        return self._token

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        response = self._http.get(
            self._base_url + path, params=params, headers={"Authorization": f"Bearer {self._bearer()}"}
        )
        if response.status_code == 401:
            # Token revoked or expired early (e.g. the key was rotated).
            response = self._http.get(
                self._base_url + path, params=params, headers={"Authorization": f"Bearer {self._fetch_token()}"}
            )
        return response

    def _get_json(self, path: str, params: dict | None = None) -> Any:
        response = self._get(path, params)
        # httpx.HTTPStatusError carries .response.status_code, which the
        # rate limiter inspects for 429/403.
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get_user_summary(self, cdate: str) -> dict | None:
        return self._get_json("/usersummary-service/usersummary/daily", {"calendarDate": cdate})

    def get_sleep_data(self, cdate: str) -> dict | None:
        return self._get_json("/sleep-service/sleep/dailySleep", {"date": cdate})

    def get_hrv_data(self, cdate: str) -> dict | None:
        return self._get_json(f"/hrv-service/hrv/{cdate}")

    def get_body_composition(self, cdate: str) -> dict:
        entries = self._get_json(f"/weight-service/weight/all/{cdate}/{cdate}") or []
        # Same envelope as garminconnect's weight/dateRange, oldest first, so
        # the mapper's "last entry is the latest weigh-in" holds.
        return {"dateWeightList": sorted(entries, key=lambda e: e.get("date") or 0)}

    # Range variants return {calendarDate: payload}, with each payload shaped
    # like the single-day method's so the same mapper applies. Days with no
    # data are simply absent.

    def get_sleep_range(self, start: str, end: str) -> dict[str, dict]:
        days = self._get_json("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": start, "endDate": end})
        return {d["calendarDate"]: d for d in days or [] if d.get("calendarDate")}

    def get_hrv_range(self, start: str, end: str) -> dict[str, dict]:
        body = self._get_json(f"/hrv-service/hrv/daily/{start}/{end}") or {}
        return {s["calendarDate"]: s for s in body.get("hrvSummaries") or [] if s.get("calendarDate")}

    def get_body_composition_range(self, start: str, end: str) -> dict[str, dict]:
        entries = self._get_json(f"/weight-service/weight/all/{start}/{end}") or []
        by_day: dict[str, list] = {}
        for entry in sorted(entries, key=lambda e: e.get("date") or 0):
            by_day.setdefault(entry.get("calendarDate"), []).append(entry)
        return {day: {"dateWeightList": items} for day, items in by_day.items() if day}

    def get_activities(self, start: int, limit: int) -> list[dict]:
        return self._get_json(
            "/activitylist-service/activities/search/activities", {"start": start, "limit": limit}
        ) or []

    def download_activity_fit(self, activity_id: int) -> bytes | None:
        """The original upload, zipped. None when Garmin has no file for the
        activity: third-party uploads such as Peloton return 404 here even
        though Connect still has their laps."""
        response = self._get(f"/download-service/files/activity/{activity_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content
