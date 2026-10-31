"""Statistical / ML insight endpoints (the extension task)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api.deps import FiltersDep, SessionDep, cached_json
from app.insights import engine, narrative
from app.reference import resolve_location
from app.schemas import ERROR_RESPONSES

router = APIRouter(prefix="/api/v1/insights", tags=["Insights (ML)"], responses=ERROR_RESPONSES)

MonthQ = Annotated[
    str | None,
    Query(
        pattern=r"^\d{4}-\d{2}$",
        description="Target month YYYY-MM (default: the current month)",
        examples=["2025-05"],
    ),
]


@router.get("/overview", summary="Model-based diagnosis: trend, seasonality, hotspots")
def overview(request: Request, session: SessionDep, filters: FiltersDep) -> Response:
    """Fits a Poisson GLM to monthly counts per city and tests, with likelihood-ratio tests, whether there
    is a long-term trend, a seasonal cycle and genuine differences between cities. City rate ratios are
    reported with 95% CIs and Benjamini-Hochberg-adjusted q-values; 'hotspot' means significantly above
    average at a 5% false discovery rate."""
    return cached_json(
        request, session, "overview", filters.cache_params(), lambda: engine.overview(session, filters)
    )


@router.get("/forecast", summary="Monthly forecast with prediction intervals and backtest")
def forecast(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    target_city: Annotated[
        str | None, Query(alias="for_city", description="Forecast one city (omit for the UK total)")
    ] = None,
    horizon: Annotated[int, Query(ge=1, le=36)] = 12,
) -> Response:
    """Expected sightings per month plus 80%/95% prediction intervals from posterior-predictive simulation.
    The `backtest` block re-fits the model without the last 12 months and scores it against naive baselines,
    so you can see whether the model actually beats 'just use the average'."""
    city = resolve_location(target_city).name if target_city else None
    return cached_json(
        request,
        session,
        "forecast",
        {**filters.cache_params(), "c": city, "h": horizon},
        lambda: engine.forecast(session, filters, city, horizon),
    )


@router.get("/anomalies", summary="Unusual spikes or drops by city and period")
def anomalies(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    granularity: Annotated[Literal["month", "quarter", "year"], Query()] = "quarter",
    alpha: Annotated[float, Query(gt=0, lt=0.5, description="False discovery rate")] = 0.05,
) -> Response:
    return cached_json(
        request,
        session,
        "anomalies",
        {**filters.cache_params(), "g": granularity, "a": alpha},
        lambda: engine.anomalies(session, filters, granularity, alpha),
    )


@router.get("/risk", summary="Explainable 0-100 tick-activity score per city")
def risk(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    month: MonthQ = None,
    window_months: Annotated[int, Query(ge=6, le=120)] = 24,
) -> Response:
    """Each score decomposes into recent activity (empirical-Bayes blend of the recent window and the city's
    long-term rate), a seasonal factor for the target month and a trend factor. The `explanation` field
    states the reasoning in plain English."""
    return cached_json(
        request,
        session,
        "risk",
        {**filters.cache_params(), "m": month, "w": window_months},
        lambda: engine.risk(session, filters, month, window_months),
    )


@router.get("/risk/near", summary="Risk near a point (e.g. the user's location)")
def risk_near(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    at_lat: Annotated[float, Query(alias="at_lat", ge=-90, le=90)],
    at_lon: Annotated[float, Query(alias="at_lon", ge=-180, le=180)],
    within_km: Annotated[float, Query(gt=0, le=500)] = 75,
    month: MonthQ = None,
) -> Response:
    params = {**filters.cache_params(), "la": at_lat, "lo": at_lon, "r": within_km, "m": month}
    return cached_json(
        request,
        session,
        "risk_near",
        params,
        lambda: engine.risk_near(session, filters, at_lat, at_lon, within_km, month),
    )


@router.get("/briefing", summary="Plain-English summary of everything above")
def briefing(request: Request, session: SessionDep, filters: FiltersDep, month: MonthQ = None) -> Response:
    return cached_json(
        request,
        session,
        "briefing",
        {**filters.cache_params(), "m": month},
        lambda: narrative.briefing(session, filters, month),
    )
