"""Official Personal API client, FIT lap parsing, and the hybrid routing
between the official API and `garminconnect`. All values are synthetic."""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import httpx
import pytest
from garmin_fit_sdk import Encoder, Profile

from garmin_mcp.db.connection import init_db, upsert
from garmin_mcp.garmin_client.factory import GarminClients
from garmin_mcp.garmin_client.personal_api import TOKEN_PATH, PersonalApiClient
from garmin_mcp.garmin_client.rate_limiter import _is_rate_limited_or_forbidden
from garmin_mcp.sync.activities_sync import ActivitiesSyncResult, fill_missing_laps
from garmin_mcp.sync.daily_categories import build_specs
from garmin_mcp.sync.engine import sync_daily_category
from garmin_mcp.sync.fit_laps import parse_fit_laps

LAP_START = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone.utc)
LAP_START_MS = 1767261600000


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


class PassThroughLimiter:
    def call(self, category, func, *args, **kwargs):
        return func(*args, **kwargs)


def _fit_file(sport: str, laps: list[dict]) -> bytes:
    mesg = Profile["mesg_num"]
    enc = Encoder()
    enc.write_mesg({"mesg_num": mesg["FILE_ID"], "type": "activity", "manufacturer": "development", "product": 0, "time_created": LAP_START, "serial_number": 1})
    for i, lap in enumerate(laps):
        start = LAP_START + dt.timedelta(minutes=5 * i)
        enc.write_mesg({"mesg_num": mesg["LAP"], "message_index": i, "start_time": start, "timestamp": start, "sport": sport, **lap})
    enc.write_mesg({"mesg_num": mesg["SESSION"], "start_time": LAP_START, "timestamp": LAP_START, "sport": sport})
    return enc.close()


def _zipped(fit: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("123_ACTIVITY.fit", fit)
    return buf.getvalue()


RUN_LAP = {
    "total_elapsed_time": 300.0,
    "total_timer_time": 290.0,
    "total_distance": 400.0,
    "avg_heart_rate": 120,
    "max_heart_rate": 130,
    "avg_cadence": 80,
    "avg_fractional_cadence": 0.25,
    "max_cadence": 85,
    "max_fractional_cadence": 0.5,
    "enhanced_avg_speed": 1.5,
    "intensity": "warmup",
    "wkt_step_index": 0,
}


def test_fit_laps_match_connect_split_semantics():
    rows = parse_fit_laps(9, _zipped(_fit_file("running", [RUN_LAP, {**RUN_LAP, "intensity": "active", "wkt_step_index": 1}])))

    assert [r["lap_index"] for r in rows] == [1, 2]
    first = rows[0]
    assert first["start_time_utc_ms"] == LAP_START_MS
    assert first["end_time_utc_ms"] == LAP_START_MS + 300_000
    assert first["duration_s"] == 290.0
    assert (first["avg_cadence"], first["max_cadence"]) == (160.5, 171.0)
    assert (first["intensity_type"], rows[1]["intensity_type"]) == ("WARMUP", "ACTIVE")
    assert first["workout_step_index"] == 0
    assert json.loads(first["measurements_json"])["total_distance"] == 400.0


def test_fit_laps_keep_cycling_cadence_as_rpm():
    rows = parse_fit_laps(9, _fit_file("cycling", [{"avg_cadence": 88, "max_cadence": 110}]))
    assert (rows[0]["avg_cadence"], rows[0]["max_cadence"]) == (88.0, 110.0)


def _api(handler) -> tuple[PersonalApiClient, dict]:
    calls = {"token": 0}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            assert request.headers["Authorization"] == "ApiKey k"
            calls["token"] += 1
            return httpx.Response(200, text=f"tok{calls['token']}")
        return handler(request)

    clock = {"now": 0.0}
    client = PersonalApiClient("k", http=httpx.Client(transport=httpx.MockTransport(route)), time_fn=lambda: clock["now"])
    calls["clock"] = clock
    return client, calls


def test_token_is_reused_then_renewed_after_ttl():
    seen = []
    client, calls = _api(lambda r: (seen.append(r.headers["Authorization"]), httpx.Response(200, json={"totalSteps": 1}))[1])

    client.get_user_summary("2026-01-01")
    client.get_user_summary("2026-01-02")
    calls["clock"]["now"] = 24 * 3600
    client.get_user_summary("2026-01-03")

    assert calls["token"] == 2
    assert seen == ["Bearer tok1", "Bearer tok1", "Bearer tok2"]


def test_401_gets_one_retry_with_a_fresh_token():
    def handler(request):
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401)
        return httpx.Response(200, json={"hrvSummary": {}})

    client, calls = _api(handler)
    assert client.get_hrv_data("2026-01-01") == {"hrvSummary": {}}
    assert calls["token"] == 2


def test_204_is_no_data_not_an_error():
    client, _ = _api(lambda r: httpx.Response(204))
    assert client.get_hrv_data("2026-01-01") is None


def test_429_is_seen_by_the_rate_limiter():
    client, _ = _api(lambda r: httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        client.get_sleep_data("2026-01-01")
    assert _is_rate_limited_or_forbidden(excinfo.value)


def test_body_composition_uses_garminconnect_envelope_oldest_first():
    entries = [{"date": 2000, "weight": 70000.0}, {"date": 1000, "weight": 71000.0}]
    client, _ = _api(lambda r: httpx.Response(200, json=entries))
    result = client.get_body_composition("2026-01-01")
    assert [e["date"] for e in result["dateWeightList"]] == [1000, 2000]


def test_fit_download_404_means_no_file():
    client, _ = _api(lambda r: httpx.Response(404, json={"message": "Activity not found"}))
    assert client.download_activity_fit(1) is None


class FakePersonal:
    def __init__(self, files=None):
        self.files = files or {}
        self.calls: list[str] = []

    def get_user_summary(self, cdate):
        self.calls.append(f"summary {cdate}")
        return {"totalSteps": 1234}

    def download_activity_fit(self, activity_id):
        self.calls.append(f"fit {activity_id}")
        return self.files.get(activity_id)


class FakeGarmin:
    def __init__(self):
        self.calls: list[str] = []

    def get_user_summary(self, cdate):
        self.calls.append(f"summary {cdate}")
        return {"totalSteps": 99}

    def get_training_readiness(self, cdate):
        self.calls.append(f"readiness {cdate}")
        return [{"score": 70}]

    def get_activity_splits(self, activity_id):
        self.calls.append(f"splits {activity_id}")
        return {"lapDTOs": [{"lapIndex": 1, "startTimeGMT": "2026-01-01T10:00:00.0", "elapsedDuration": 60.0, "intensityType": "ACTIVE"}]}


def _spec(category):
    return next(s for s in build_specs() if s.category == category)


def test_shared_categories_prefer_the_official_api():
    personal, garmin = FakePersonal(), FakeGarmin()
    clients = GarminClients(personal, lambda: garmin)
    assert _spec("daily_health_metrics").fetch(clients, "2026-01-01") == {"totalSteps": 1234}
    assert garmin.calls == []


def test_shared_categories_fall_back_without_an_api_key():
    garmin = FakeGarmin()
    assert _spec("daily_health_metrics").fetch(GarminClients(None, lambda: garmin), "2026-01-01") == {"totalSteps": 99}


def test_unofficial_only_categories_use_garminconnect_even_with_a_key():
    personal, garmin = FakePersonal(), FakeGarmin()
    _spec("training_readiness").fetch(GarminClients(personal, lambda: garmin), "2026-01-01")
    assert garmin.calls == ["readiness 2026-01-01"] and personal.calls == []


def test_failed_unofficial_login_is_attempted_once_and_official_data_still_syncs(conn):
    attempts = []

    def broken_login():
        attempts.append(1)
        raise RuntimeError("SSO login failed")

    clients = GarminClients(FakePersonal(), broken_login)
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]

    official = sync_daily_category(_spec("daily_health_metrics"), PassThroughLimiter(), clients, conn, dates, "incremental_sync")
    unofficial = sync_daily_category(_spec("training_readiness"), PassThroughLimiter(), clients, conn, dates, "incremental_sync")

    assert official.fetched == 3 and not official.failed
    assert conn.execute("SELECT COUNT(*) FROM daily_health_metrics WHERE total_steps = 1234").fetchone()[0] == 3
    assert len(unofficial.failed) == 3
    assert len(attempts) == 1
    assert isinstance(clients.login_error, RuntimeError)


def test_laps_come_from_fit_and_fall_back_to_splits_when_no_file(conn):
    for aid in (1, 2):
        upsert(conn, "activities", {"activity_id": aid, "begin_timestamp_utc_ms": aid, "source": "api"}, ["activity_id"])
    personal = FakePersonal(files={1: _zipped(_fit_file("running", [RUN_LAP, RUN_LAP, RUN_LAP]))})
    garmin = FakeGarmin()

    result = ActivitiesSyncResult()
    fill_missing_laps(PassThroughLimiter(), GarminClients(personal, lambda: garmin), conn, result)

    assert sorted(personal.calls) == ["fit 1", "fit 2"]
    assert garmin.calls == ["splits 2"]
    counts = dict(conn.execute("SELECT activity_id, lap_count FROM activities").fetchall())
    assert counts == {1: 3, 2: 1}
    assert result.laps_written == 4


def test_range_methods_key_payloads_by_day():
    def handler(request):
        path = request.url.path
        if path.startswith("/weight-service"):
            return httpx.Response(200, json=[
                {"calendarDate": "2026-01-02", "date": 20, "weight": 70000.0},
                {"calendarDate": "2026-01-02", "date": 10, "weight": 71000.0},
                {"calendarDate": "2026-01-05", "date": 50, "weight": 69000.0},
            ])
        if path.startswith("/hrv-service"):
            return httpx.Response(200, json={"hrvSummaries": [{"calendarDate": "2026-01-02", "lastNightAvg": 40}]})
        return httpx.Response(200, json=[{"calendarDate": "2026-01-03", "deepSleepSeconds": 3600}])

    client, _ = _api(handler)
    weights = client.get_body_composition_range("2026-01-01", "2026-01-31")
    assert [e["date"] for e in weights["2026-01-02"]["dateWeightList"]] == [10, 20]
    assert set(weights) == {"2026-01-02", "2026-01-05"}
    assert client.get_hrv_range("2026-01-01", "2026-01-31") == {"2026-01-02": {"calendarDate": "2026-01-02", "lastNightAvg": 40}}
    assert set(client.get_sleep_range("2026-01-01", "2026-01-31")) == {"2026-01-03"}


class FakeRangePersonal:
    def __init__(self, fail_from: str | None = None):
        self.ranges: list[tuple[str, str]] = []
        self.fail_from = fail_from

    def get_hrv_range(self, start, end):
        self.ranges.append((start, end))
        if self.fail_from and start >= self.fail_from:
            raise RuntimeError("boom")
        return {"2026-01-02": {"lastNightAvg": 40, "status": "BALANCED"}}

    def get_hrv_data(self, cdate):
        raise AssertionError("per-day fetch used despite a range endpoint")


def _days(start: dt.date, n: int) -> list[str]:
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


def test_range_categories_fetch_31_day_chunks_not_days(conn):
    personal = FakeRangePersonal()
    dates = _days(dt.date(2026, 1, 1), 40)

    result = sync_daily_category(_spec("hrv_daily"), PassThroughLimiter(), GarminClients(personal, FakeGarmin), conn, dates, "backfill")

    assert personal.ranges == [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-09")]
    assert result.fetched == 40 and not result.failed
    assert [tuple(r) for r in conn.execute("SELECT calendar_date, last_night_avg FROM hrv_daily")] == [("2026-01-02", 40)]


def test_range_chunks_follow_backfill_order_and_a_failed_chunk_fails_its_days(conn):
    personal = FakeRangePersonal(fail_from="2025-01-01")
    dates = list(reversed(_days(dt.date(2025, 12, 20), 20)))

    result = sync_daily_category(_spec("hrv_daily"), PassThroughLimiter(), GarminClients(personal, FakeGarmin), conn, dates, "backfill")

    assert personal.ranges == [("2025-12-20", "2026-01-08")]
    assert len(result.failed) == 20
    assert result.contiguous_success_boundary is None


def test_range_fetch_is_skipped_without_an_api_key(conn):
    garmin = FakeGarmin()
    garmin.get_hrv_data = lambda cdate: (garmin.calls.append(cdate), {"hrvSummary": {"lastNightAvg": 1}})[1]
    dates = _days(dt.date(2026, 1, 1), 3)

    sync_daily_category(_spec("hrv_daily"), PassThroughLimiter(), GarminClients(None, lambda: garmin), conn, dates, "incremental_sync")

    assert garmin.calls == dates


def _api_activity(conn, aid, begin_ms, source="api"):
    upsert(conn, "activities", {"activity_id": aid, "begin_timestamp_utc_ms": begin_ms, "source": source}, ["activity_id"])


def test_activities_deleted_in_connect_are_removed_with_their_laps(conn):
    from garmin_mcp.sync.activities_sync import remove_deleted_activities

    _api_activity(conn, 1, 1000)
    _api_activity(conn, 2, 2000)
    _api_activity(conn, 3, 3000)
    _api_activity(conn, 4, 2500, source="csv_export")
    _api_activity(conn, 5, 500)
    upsert(conn, "activity_laps", {"activity_id": 2, "lap_index": 1}, ["activity_id", "lap_index"])

    result = ActivitiesSyncResult()
    remove_deleted_activities(conn, seen_ids={1, 3}, oldest_seen_ms=1000, result=result)

    remaining = {r[0] for r in conn.execute("SELECT activity_id FROM activities")}
    assert remaining == {1, 3, 4, 5}
    assert result.deleted_activity_ids == [2]
    assert conn.execute("SELECT COUNT(*) FROM activity_laps").fetchone()[0] == 0


def test_activity_sharing_the_oldest_timestamp_is_kept(conn):
    from garmin_mcp.sync.activities_sync import remove_deleted_activities

    _api_activity(conn, 1, 1000)
    _api_activity(conn, 2, 1000)
    remove_deleted_activities(conn, seen_ids={1}, oldest_seen_ms=1000, result=ActivitiesSyncResult())
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2


def test_mass_disappearance_is_not_deleted(conn):
    from garmin_mcp.sync.activities_sync import (
        MAX_DELETIONS_PER_RUN,
        remove_deleted_activities,
    )

    for aid in range(1, MAX_DELETIONS_PER_RUN + 3):
        _api_activity(conn, aid, 1000 + aid)
    result = ActivitiesSyncResult()
    remove_deleted_activities(conn, seen_ids={1}, oldest_seen_ms=1001, result=result)

    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == MAX_DELETIONS_PER_RUN + 2
    assert result.deleted_activity_ids == [] and result.warnings


class ListOnlyPersonal(FakePersonal):
    def __init__(self, listing):
        super().__init__()
        self.listing = listing

    def get_activities(self, start, limit):
        return self.listing[start : start + limit]


def test_incremental_sync_removes_a_duplicate_deleted_in_connect(conn):
    from garmin_mcp.sync.activities_sync import incremental_sync_activities

    for aid, begin in ((10, 1000), (11, 2000), (12, 3000)):
        _api_activity(conn, aid, begin)
        conn.execute("UPDATE activities SET lap_count = 0 WHERE activity_id = ?", (aid,))
    conn.commit()
    listing = [{"activityId": 12, "beginTimestamp": 3000}, {"activityId": 10, "beginTimestamp": 1000}]

    result = incremental_sync_activities(PassThroughLimiter(), GarminClients(ListOnlyPersonal(listing), FakeGarmin), conn, page_size=2)

    assert result.deleted_activity_ids == [11]
    log = conn.execute("SELECT status, warning FROM sync_log WHERE category = 'activities'").fetchone()
    assert log[0] == "success" and "[11]" in log[1]
