"""FastAPI application factory and lifecycle.

Start-up sequence
-----------------
1. create tables (idempotent)
2. if the database is empty, load the bundled snapshot so the API is useful in
   under a second, even offline
3. start the background scheduler: an immediate live sync, then every N minutes
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app import __version__
from app.api import routes_insights, routes_ops, routes_sightings, routes_stats
from app.cache import VersionedCache
from app.config import Settings, get_settings
from app.db import create_all, init_engine, session_scope
from app.errors import install_error_handlers
from app.ingestion.client import UpstreamClient
from app.ingestion.pipeline import IngestionService
from app.ingestion.scheduler import run_scheduler
from app.logging_setup import configure_logging, request_id_ctx
from app.models import Sighting

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Backend MVP for a **UK tick sightings tracker** built on the Elanco Tick Sightings API.

* **Ingestion** - streamed, batched, validated, de-duplicated and audited; survives upstream outages
  (retries with jittered backoff, circuit breaker, per-city fallback, last-known-good data).
* **Search & filter** - time range, city/region/nation, radius around a point, species, free text.
* **Reporting** - per-region counts, weekly/monthly trends (zero-filled), seasonality, heatmaps.
* **Insights** - Poisson GLM diagnostics, forecasts with prediction intervals and backtests,
  FDR-controlled anomaly detection and an explainable risk score.

Every error uses one envelope: `{"error": {"code", "message", "details", "request_id"}}`.
"""


async def _bootstrap(service: IngestionService) -> None:
    with session_scope() as s:
        has_data = (s.scalar(select(func.count()).select_from(Sighting)) or 0) > 0
    if not has_data:
        log.info("empty database: seeding from bundled snapshot")
        await service.sync(trigger="bootstrap", strategy="snapshot")


def create_app(
    settings: Settings | None = None, *, start_scheduler: bool = True, client: UpstreamClient | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    init_engine(settings.database_url)
    create_all()

    cache = VersionedCache(settings.cache_size)
    upstream = client or UpstreamClient(settings)
    ingestion = IngestionService(settings, upstream, on_data_changed=cache.clear)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _bootstrap(ingestion)
        task = asyncio.create_task(run_scheduler(ingestion, settings)) if start_scheduler else None
        log.info("api ready", extra={"upstream": settings.upstream_base_url, "db": settings.database_url})
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Sightings", "description": "Search, filter and export individual sightings"},
            {"name": "Reporting", "description": "Aggregates: per region, trends, seasonality, heatmaps"},
            {
                "name": "Insights (ML)",
                "description": "Statistical modelling: diagnostics, forecasts, anomalies, risk",
            },
            {"name": "Data management", "description": "Ingestion runs, data quality, upstream status"},
            {"name": "Reference data", "description": "Cities, regions, species"},
            {"name": "Health", "description": "Liveness and readiness probes"},
        ],
    )
    app.state.settings = settings
    app.state.cache = cache
    app.state.ingestion = ingestion

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers["X-Response-Time-ms"] = f"{elapsed:.1f}"
        if request.url.path.startswith("/api") or request.url.path.startswith("/health"):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round(elapsed, 1),
                    "request_id": rid,
                },
            )
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Data-Version", "ETag", "X-Response-Time-ms"],
    )
    install_error_handlers(app)

    for r in (
        routes_sightings.router,
        routes_stats.router,
        routes_insights.router,
        routes_ops.router,
        routes_ops.meta_router,
        routes_ops.health_router,
    ):
        app.include_router(r)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/v1", tags=["Health"], summary="API index")
    def index() -> dict:
        return {
            "name": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "dashboard": "/",
            "endpoints": {
                path: sorted(m.upper() for m in ops)
                for path, ops in app.openapi()["paths"].items()
                if path != "/api/v1"
            },
        }

    return app


def __getattr__(name: str):
    """Lazily build the default app so ``uvicorn app.main:app`` works, while importing this
    module (e.g. from tests) has no side effects."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
