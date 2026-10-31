import httpx
import respx
from sqlalchemy import func, select

from app.db import create_all, init_engine, session_scope
from app.ingestion.client import UpstreamClient
from app.ingestion.pipeline import IngestionService, get_data_version
from app.models import IngestionRun, RejectedRecord, Sighting
from tests.conftest import UPSTREAM, make_settings, no_sleep
from tools.chaos_upstream import make_dirty


def service(tmp_path, **overrides) -> IngestionService:
    s = make_settings(tmp_path, **overrides)
    init_engine(s.database_url)
    create_all()
    return IngestionService(s, UpstreamClient(s, sleep=no_sleep))


def count(**where) -> int:
    with session_scope() as s:
        stmt = select(func.count()).select_from(Sighting)
        for k, v in where.items():
            stmt = (
                stmt.where(getattr(Sighting, k) == v)
                if v is not None
                else stmt.where(getattr(Sighting, k).is_(None))
            )
        return s.scalar(stmt)


@respx.mock
async def test_full_sync_is_idempotent(tmp_path, snapshot_records):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(return_value=httpx.Response(200, json=snapshot_records))
    svc = service(tmp_path)
    first = await svc.sync("test")
    second = await svc.sync("test")
    assert (first.status, first.source, first.inserted) == ("success", "api", 1000)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1000)
    assert count() == 1000
    with session_scope() as s:
        assert get_data_version(s) == 1  # the no-op second run did not invalidate caches


@respx.mock
async def test_changed_record_is_updated(tmp_path, snapshot_records):
    route = respx.get(f"{UPSTREAM}/tick-sightings")
    route.mock(return_value=httpx.Response(200, json=snapshot_records))
    svc = service(tmp_path)
    await svc.sync("test")
    changed = [dict(r) for r in snapshot_records]
    changed[0]["location"] = "York"
    route.mock(return_value=httpx.Response(200, json=changed))
    report = await svc.sync("test")
    assert report.updated == 1
    with session_scope() as s:
        assert s.get(Sighting, changed[0]["id"]).city == "York"


@respx.mock
async def test_records_missing_upstream_are_soft_deactivated(tmp_path, snapshot_records):
    route = respx.get(f"{UPSTREAM}/tick-sightings")
    route.mock(return_value=httpx.Response(200, json=snapshot_records))
    svc = service(tmp_path)
    await svc.sync("test")
    route.mock(return_value=httpx.Response(200, json=snapshot_records[:900]))
    report = await svc.sync("test")
    assert report.deactivated == 100
    assert count(is_active=True) == 900 and count() == 1000  # nothing hard-deleted


@respx.mock
async def test_deactivation_safety_valve(tmp_path, snapshot_records):
    route = respx.get(f"{UPSTREAM}/tick-sightings")
    route.mock(return_value=httpx.Response(200, json=snapshot_records))
    svc = service(tmp_path)
    await svc.sync("test")
    route.mock(return_value=httpx.Response(200, json=snapshot_records[:10]))  # upstream bug: near-empty list
    report = await svc.sync("test")
    assert report.deactivated == 0
    assert any("refused to deactivate" in w for w in report.warnings)
    assert count(is_active=True) == 1000


@respx.mock
async def test_falls_back_to_per_city_requests(tmp_path, snapshot_records):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(return_value=httpx.Response(504))
    respx.get(url__regex=rf"{UPSTREAM}/tick-sightings/city/.*").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[r for r in snapshot_records if r["location"] == req.url.path.rsplit("/", 1)[-1]]
        )
    )
    svc = service(tmp_path, circuit_failure_threshold=50)
    report = await svc.sync("test")
    assert (report.status, report.source, report.inserted) == ("partial", "api_partitioned", 1000)
    assert report.deactivated == 0  # never deactivate after a partial view of the data


@respx.mock
async def test_upstream_down_keeps_existing_data(tmp_path, snapshot_records):
    route = respx.get(f"{UPSTREAM}/tick-sightings")
    route.mock(return_value=httpx.Response(200, json=snapshot_records))
    svc = service(tmp_path)
    await svc.sync("test")
    route.mock(return_value=httpx.Response(503))
    respx.get(url__regex=rf"{UPSTREAM}/tick-sightings/city/.*").mock(return_value=httpx.Response(503))
    report = await svc.sync("test")
    assert report.status == "failed" and report.source == "cache"
    assert "retained" in report.error
    assert count(is_active=True) == 1000


@respx.mock
async def test_upstream_down_and_empty_db_loads_snapshot(tmp_path):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(return_value=httpx.Response(503))
    respx.get(url__regex=rf"{UPSTREAM}/tick-sightings/city/.*").mock(return_value=httpx.Response(503))
    report = await service(tmp_path).sync("test")
    assert (report.source, report.inserted, report.status) == ("snapshot", 1000, "partial")


@respx.mock
async def test_dirty_feed_is_cleaned_quarantined_and_deduplicated(tmp_path, snapshot_records):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(
        return_value=httpx.Response(200, json=make_dirty(snapshot_records))
    )
    report = await service(tmp_path).sync("test")
    assert report.status == "success"
    assert report.rejected == 16  # bad dates, missing locations, non-objects
    assert report.duplicates_flagged == 10  # double-submitted reports
    assert report.inserted == 1025
    assert report.quality["flag:location_fuzzy_matched"] >= 5
    assert report.quality["flag:species_conflict_latin_preferred"] == 5
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RejectedRecord)) == 16
        usable = s.scalar(select(func.count()).select_from(Sighting).where(Sighting.duplicate_of.is_(None)))
    assert usable == 1015


@respx.mock
async def test_stream_restart_does_not_double_count(tmp_path, snapshot_records):
    import json

    body = json.dumps(make_dirty(snapshot_records)).encode()
    respx.get(f"{UPSTREAM}/tick-sightings").mock(
        side_effect=[
            httpx.Response(
                200, content=body[: int(len(body) * 0.7)], headers={"content-type": "application/json"}
            ),
            httpx.Response(200, content=body, headers={"content-type": "application/json"}),
        ]
    )
    report = await service(tmp_path, batch_size=200).sync("test")
    assert report.inserted == 1025 and report.rejected == 16
    assert any("restarted" in w for w in report.warnings)


async def test_every_run_is_audited(tmp_path):
    svc = service(tmp_path)
    await svc.sync("test", strategy="snapshot")
    with session_scope() as s:
        run = s.scalars(select(IngestionRun)).one()
    assert (run.status, run.source, run.inserted, run.trigger) == ("success", "snapshot", 1000, "test")
    assert run.finished_at is not None and run.duration_ms is not None


async def test_sql_duplicate_detection_matches_python_reference(tmp_path):
    import random
    from datetime import datetime, timedelta

    from app.ingestion.cleaning import DupCandidate, flag_near_duplicates
    from tests.conftest import write_snapshot

    rng = random.Random(5)
    base = datetime(2020, 1, 1)
    records = [
        {
            "id": f"r{i}",
            "date": (base + timedelta(seconds=rng.randrange(3 * 86400))).isoformat(),
            "location": rng.choice(["Leeds", "York"]),
            "species": rng.choice(["Marsh tick", "Tree-hole tick"]),
        }
        for i in range(600)
    ]
    svc = service(tmp_path, snapshot_path=write_snapshot(tmp_path, records))
    report = await svc.sync("test", strategy="snapshot")
    with session_scope() as s:
        rows = s.execute(
            select(Sighting.id, Sighting.city, Sighting.species, Sighting.observed_at, Sighting.duplicate_of)
        ).all()
    expected = flag_near_duplicates([DupCandidate(r.id, r.city, r.species, r.observed_at) for r in rows], 300)
    assert {r.id: r.duplicate_of for r in rows} == expected
    assert report.duplicates_flagged == sum(v is not None for v in expected.values()) > 20
