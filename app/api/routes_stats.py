"""Aggregate reporting endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api.deps import FiltersDep, SessionDep, cached_json
from app.schemas import ERROR_RESPONSES
from app.services import analytics

router = APIRouter(prefix="/api/v1/stats", tags=["Reporting"], responses=ERROR_RESPONSES)


@router.get("/summary", summary="Headline numbers")
def summary(request: Request, session: SessionDep, filters: FiltersDep) -> Response:
    """Totals, date span, top city/species, last-12-months change and sync status.
    Accepts the same filters as `/api/v1/sightings`."""
    return cached_json(
        request, session, "summary", filters.cache_params(), lambda: analytics.summary(session, filters)
    )


@router.get("/by-location", summary="Sightings per city, region or nation")
def by_location(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    group_by: Annotated[Literal["city", "region", "nation"], Query()] = "region",
) -> Response:
    """Counts, share, per-year rate and an index vs the average group, with coordinates for cities.
    Includes a chi-square test of whether groups differ by more than chance (for regions/nations the
    null hypothesis accounts for how many cities each contains)."""
    return cached_json(
        request,
        session,
        "by_location",
        {**filters.cache_params(), "g": group_by},
        lambda: analytics.by_group(session, filters, group_by),
    )


@router.get("/by-location.geojson", summary="City counts as GeoJSON (for maps)")
def by_location_geojson(request: Request, session: SessionDep, filters: FiltersDep) -> Response:
    return cached_json(
        request,
        session,
        "geojson",
        filters.cache_params(),
        lambda: analytics.by_city_geojson(session, filters),
    )


@router.get("/species", summary="Sightings per species")
def by_species(request: Request, session: SessionDep, filters: FiltersDep) -> Response:
    return cached_json(
        request,
        session,
        "species",
        filters.cache_params(),
        lambda: analytics.by_group(session, filters, "species"),
    )


@router.get("/trends", summary="Time series (weekly, monthly, ...) with trend test")
def trends(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    interval: Annotated[Literal["day", "week", "month", "quarter", "year"], Query()] = "month",
    split_by: Annotated[
        Literal["city", "region", "nation", "species"] | None,
        Query(description="Return one series per group (top N + 'Other')"),
    ] = None,
    top: Annotated[int, Query(ge=1, le=20)] = 5,
    moving_average: Annotated[int, Query(ge=1, le=24, description="Trailing window, in periods")] = 3,
) -> Response:
    """Zero-filled series (periods with no sightings are present with count 0, so charts don't lie),
    a trailing moving average, the peak period, and a Poisson-regression trend test
    (% change per year with 95% CI and p-value)."""
    params = {**filters.cache_params(), "i": interval, "s": split_by, "t": top, "ma": moving_average}
    return cached_json(
        request,
        session,
        "trends",
        params,
        lambda: analytics.trends(session, filters, interval, split_by, top, moving_average),
    )


@router.get("/seasonality", summary="Month-of-year, season, weekday and hour patterns")
def seasonality(request: Request, session: SessionDep, filters: FiltersDep) -> Response:
    return cached_json(
        request,
        session,
        "seasonality",
        filters.cache_params(),
        lambda: analytics.seasonality(session, filters),
    )


@router.get("/heatmap", summary="Two-way count matrix (e.g. city x month)")
def heatmap(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    rows: Annotated[Literal["city", "region", "nation", "species", "month", "year"], Query()] = "city",
    cols: Annotated[Literal["city", "region", "nation", "species", "month", "year"], Query()] = "month",
) -> Response:
    """Includes a chi-square test of independence (e.g. 'does the species mix differ by city?')."""
    return cached_json(
        request,
        session,
        "heatmap",
        {**filters.cache_params(), "r": rows, "c": cols},
        lambda: analytics.heatmap(session, filters, rows, cols),
    )
