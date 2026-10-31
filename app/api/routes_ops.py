"""Operational endpoints: health, ingestion control, data quality, reference data."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app import __version__
from app.api.deps import SessionDep
from app.errors import Conflict, NotFound, Unauthorized
from app.ingestion.pipeline import get_data_version, get_meta, utcnow
from app.models import IngestionRun, Sighting
from app.reference import species_catalogue
from app.schemas import ERROR_RESPONSES
from app.services import analytics, quality

health_router = APIRouter(tags=["Health"])
router = APIRouter(prefix="/api/v1", tags=["Data management"], responses=ERROR_RESPONSES)
meta_router = APIRouter(prefix="/api/v1/meta", tags=["Reference data"])

STALE_AFTER_HOURS = 24


@health_router.get("/health", summary="Liveness probe")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@health_router.get("/health/ready", summary="Readiness: database, data freshness, upstream circuit")
def ready(request: Request, session: SessionDep) -> JSONResponse:
    """Returns 200 when the API can serve useful data (even if upstream is down - we serve the last good
    copy), 503 only when there is no data at all. `status` is `ok` or `degraded`."""
    checks: dict = {}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - only when the DB is broken
        checks["database"] = f"error: {exc}"

    count = session.scalar(select(func.count()).select_from(Sighting).where(Sighting.is_active)) or 0
    last_sync = get_meta(session, "last_successful_sync_at")
    age_h = None
    if last_sync:
        age_h = round((utcnow() - datetime.fromisoformat(last_sync)).total_seconds() / 3600, 2)
    service = request.app.state.ingestion
    upstream = service.client.health_snapshot()
    last = service.last_report

    degraded_reasons = []
    if upstream["circuit_breaker"]["state"] != "closed":
        degraded_reasons.append(f"upstream circuit {upstream['circuit_breaker']['state']}")
    if age_h is None or age_h > STALE_AFTER_HOURS:
        degraded_reasons.append("data not refreshed from upstream recently")
    if get_meta(session, "last_sync_source") == "snapshot":
        degraded_reasons.append("serving bundled snapshot (live API not reached yet)")
    if last and last.status == "failed":
        degraded_reasons.append(f"last sync failed: {last.error}")

    ok = checks["database"] == "ok" and count > 0
    body = {
        "status": ("degraded" if degraded_reasons else "ok") if ok else "unavailable",
        "degraded_reasons": degraded_reasons,
        "checks": checks,
        "data": {
            "active_sightings": count,
            "data_version": get_data_version(session),
            "last_successful_sync_at": last_sync,
            "last_sync_source": get_meta(session, "last_sync_source"),
            "hours_since_sync": age_h,
            "sync_running": service.running,
        },
        "upstream": upstream,
        "cache": request.app.state.cache.stats(),
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/data-quality", tags=["Data management"], summary="What was repaired, rejected or de-duplicated")
def data_quality(session: SessionDep, samples: Annotated[int, Query(ge=0, le=50)] = 10) -> dict:
    return quality.report(session, samples)


@router.get("/ingestion/runs", summary="Audit log of sync runs")
def list_runs(session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> dict:
    runs = session.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(limit)).all()
    return {"data": [quality._run_dict(r) for r in runs]}


@router.get("/ingestion/runs/{run_id}", summary="One sync run")
def get_run(run_id: int, session: SessionDep) -> dict:
    run = session.get(IngestionRun, run_id)
    if run is None:
        raise NotFound(f"No ingestion run {run_id}.")
    return quality._run_dict(run)


@router.post("/ingestion/sync", summary="Trigger a sync now", status_code=200)
async def trigger_sync(
    request: Request,
    strategy: Annotated[Literal["auto", "full", "partitioned", "snapshot"], Query()] = "auto",
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict:
    """`auto` = stream the full dataset, fall back to per-city requests, then to stored data/snapshot.
    Protected by `X-API-Key` when `ADMIN_API_KEY` is configured. Returns the run report."""
    key = request.app.state.settings.admin_api_key
    if key and x_api_key != key:
        raise Unauthorized("A valid X-API-Key header is required to trigger a sync.")
    service = request.app.state.ingestion
    if service.running:
        raise Conflict("A sync is already running; try again shortly.")
    report = await service.sync(trigger="manual", strategy=strategy)
    return report.as_dict()


@router.get("/upstream", summary="Upstream client health: circuit breaker, latency, errors")
def upstream_status(request: Request) -> dict:
    return request.app.state.ingestion.client.health_snapshot()


@meta_router.get("/locations", summary="Cities with coordinates, region and counts")
def locations(session: SessionDep) -> dict:
    return {"data": analytics.locations(session)}


@meta_router.get("/regions", summary="Regions and nations")
def regions(session: SessionDep) -> dict:
    return {"data": analytics.regions(session)}


@meta_router.get("/species", summary="Species catalogue with counts")
def species(session: SessionDep) -> dict:
    counts = dict(
        session.execute(
            select(Sighting.species, func.count())
            .where(Sighting.is_active, Sighting.duplicate_of.is_(None))
            .group_by(Sighting.species)
        ).all()
    )
    data = [
        {
            "common_name": s.common_name,
            "latin_name": s.latin_name,
            "sightings": counts.get(s.common_name, 0),
            "in_dataset": s.common_name in counts,
        }
        for s in species_catalogue()
    ]
    return {"data": sorted(data, key=lambda d: (-d["sightings"], d["common_name"]))}
