"""Shared FastAPI dependencies: DB session, filters and the cached-response helper."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.ingestion.pipeline import get_data_version
from app.services.query import SightingFilters, build_filters

SessionDep = Annotated[Session, Depends(get_session)]


def filters_dep(
    session: SessionDep,
    start: Annotated[
        str | None,
        Query(
            description="Inclusive start: ISO date/datetime, month (2023-05) or year (2023)",
            examples=["2020-01-01"],
        ),
    ] = None,
    end: Annotated[
        str | None,
        Query(
            description="Inclusive end: a bare date includes that whole day; month/year shorthands allowed",
            examples=["2022-12-31"],
        ),
    ] = None,
    city: Annotated[
        list[str] | None,
        Query(
            description="City name(s). Repeat the parameter or comma-separate. Case-insensitive, typo-tolerant.",
            examples=["London"],
        ),
    ] = None,
    region: Annotated[
        list[str] | None, Query(description="ONS region(s), e.g. 'North West', 'Scotland'")
    ] = None,
    nation: Annotated[
        list[str] | None, Query(description="England, Scotland, Wales, Northern Ireland")
    ] = None,
    species: Annotated[
        list[str] | None,
        Query(description="Common or Latin name(s), e.g. 'Marsh tick' or 'Ixodes apronophorus'"),
    ] = None,
    lat: Annotated[float | None, Query(ge=-90, le=90, description="Latitude for a radius search")] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180, description="Longitude for a radius search")] = None,
    radius_km: Annotated[float | None, Query(gt=0, le=1000, description="Radius in km (default 50)")] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="Free-text search over city, region, species")
    ] = None,
    include_duplicates: Annotated[bool, Query(description="Include probable duplicate reports")] = False,
    include_inactive: Annotated[bool, Query(description="Include records no longer served upstream")] = False,
) -> SightingFilters:
    return build_filters(
        session,
        start=start,
        end=end,
        city=city,
        region=region,
        nation=nation,
        species=species,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        q=q,
        include_duplicates=include_duplicates,
        include_inactive=include_inactive,
    )


FiltersDep = Annotated[SightingFilters, Depends(filters_dep)]


def cached_json(
    request: Request,
    session: Session,
    namespace: str,
    params: dict[str, Any],
    compute: Callable[[], Any],
    max_age: int = 60,
) -> Response:
    """Serve ``compute()`` through the versioned cache, with ETag/304 support.

    The ETag is derived from (namespace, params, data_version), so a client's cached
    copy stays valid exactly until the next ingestion run changes the data.
    """
    version = get_data_version(session)
    cache = request.app.state.cache
    key = cache.make_key(namespace, version, params)
    etag = '"' + hashlib.sha1(key.encode()).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "X-Data-Version": str(version), "Cache-Control": f"public, max-age={max_age}"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    body = cache.get_or_compute(key, lambda: jsonable_encoder(compute()))
    return JSONResponse(body, headers=headers)
