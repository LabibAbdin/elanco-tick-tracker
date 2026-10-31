"""Search, filter, fetch and export individual sightings."""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import FiltersDep, SessionDep
from app.db import session_scope
from app.errors import NotFound
from app.models import Sighting
from app.schemas import ERROR_RESPONSES, SightingDetail, SightingOut, SightingPage

router = APIRouter(prefix="/api/v1/sightings", tags=["Sightings"])

SORT_FIELDS = {
    "observed_at": Sighting.observed_at,
    "city": Sighting.city,
    "region": Sighting.region,
    "species": Sighting.species,
}


def _to_out(s: Sighting) -> SightingOut:
    return SightingOut(
        id=s.id,
        observed_at=s.observed_at,
        city=s.city,
        region=s.region,
        nation=s.nation,
        latitude=s.latitude,
        longitude=s.longitude,
        species=s.species,
        latin_name=s.latin_name,
        quality_flags=s.flags,
        duplicate_of=s.duplicate_of,
    )


@router.get("", response_model=SightingPage, responses=ERROR_RESPONSES, summary="Search and filter sightings")
def list_sightings(
    request: Request,
    session: SessionDep,
    filters: FiltersDep,
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[int | None, Query(ge=1, description="Items per page (max 500)")] = None,
    sort: Annotated[Literal["observed_at", "city", "region", "species"], Query()] = "observed_at",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> SightingPage:
    """Filter by **time range** (`start`/`end`), **location** (`city`, `region`, `nation`, or a
    `lat`/`lon`/`radius_km` circle), **species** and free text `q`. All filters combine with AND;
    repeated values of one filter combine with OR.
    """
    settings = request.app.state.settings
    size = min(page_size or settings.default_page_size, settings.max_page_size)
    total = session.scalar(filters.apply(select(func.count()).select_from(Sighting))) or 0
    col = SORT_FIELDS[sort]
    ordering = [col.desc() if order == "desc" else col.asc(), Sighting.id.asc()]
    rows = session.scalars(
        filters.apply(select(Sighting)).order_by(*ordering).offset((page - 1) * size).limit(size)
    ).all()
    total_pages = max(1, math.ceil(total / size))

    def link(p: int) -> str:
        return str(request.url.include_query_params(page=p, page_size=size))

    return SightingPage(
        data=[_to_out(r) for r in rows],
        meta={
            "page": page,
            "page_size": size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "sort": sort,
            "order": order,
            "filters": filters.describe(),
        },
        links={
            "self": link(page),
            "next": link(page + 1) if page < total_pages else None,
            "prev": link(page - 1) if page > 1 else None,
        },
    )


@router.get("/export", summary="Stream all matching sightings as CSV or NDJSON")
def export_sightings(
    filters: FiltersDep,
    format: Annotated[Literal["csv", "ndjson"], Query()] = "csv",
) -> StreamingResponse:
    """Streams rows straight from a server-side cursor, so exports of any size use constant memory."""
    fields = [
        "id",
        "observed_at",
        "city",
        "region",
        "nation",
        "latitude",
        "longitude",
        "species",
        "latin_name",
        "quality_flags",
    ]

    def rows() -> Iterator[str]:
        with session_scope() as s:
            stmt = filters.apply(select(Sighting)).order_by(Sighting.observed_at, Sighting.id)
            if format == "csv":
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(fields)
                for r in s.scalars(stmt.execution_options(yield_per=1000)):
                    writer.writerow(
                        [getattr(r, f) if f != "observed_at" else r.observed_at.isoformat() for f in fields]
                    )
                    if buf.tell() > 64_000:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate()
                yield buf.getvalue()
            else:
                for r in s.scalars(stmt.execution_options(yield_per=1000)):
                    yield json.dumps(_to_out(r).model_dump(mode="json")) + "\n"

    media = "text/csv" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        rows(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="tick-sightings.{format}"'},
    )


@router.get(
    "/{sighting_id}",
    response_model=SightingDetail,
    responses=ERROR_RESPONSES,
    summary="Get one sighting, with lineage",
)
def get_sighting(sighting_id: str, session: SessionDep) -> SightingDetail:
    s = session.get(Sighting, sighting_id)
    if s is None:
        raise NotFound(f"No sighting with id '{sighting_id}'.", details={"id": sighting_id})
    dups = [i for (i,) in session.execute(select(Sighting.id).where(Sighting.duplicate_of == s.id))]
    return SightingDetail(
        **_to_out(s).model_dump(),
        source=s.source,
        is_active=s.is_active,
        raw_location=s.raw_location,
        raw_species=s.raw_species,
        first_seen_at=s.first_seen_at,
        last_seen_at=s.last_seen_at,
        updated_at=s.updated_at,
        probable_duplicates=dups,
    )
