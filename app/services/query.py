"""Search & filter primitives shared by every read endpoint.

``SightingFilters`` turns validated query parameters into SQL WHERE clauses. Every
listing, aggregate and model in the API accepts the same filters, so "London,
Marsh ticks, 2020-2022" means the same thing everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Integer, Select, and_, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from app.errors import InvalidQuery
from app.ingestion.cleaning import parse_datetime
from app.models import Sighting
from app.reference import (
    PLACES,
    UNIDENTIFIED_SPECIES,
    UNMAPPED_REGION,
    lookup_species,
    places_within,
    resolve_location,
    suggest_locations,
)

INTERVALS = ("day", "week", "month", "quarter", "year")


@dataclass
class SightingFilters:
    start: datetime | None = None
    end: datetime | None = None  # exclusive
    cities: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    nations: list[str] = field(default_factory=list)
    species: list[str] = field(default_factory=list)
    near: tuple[float, float, float] | None = None  # lat, lon, radius_km
    near_cities: list[str] = field(default_factory=list)
    text: str | None = None
    include_duplicates: bool = False
    include_inactive: bool = False

    def conditions(self) -> list[ColumnElement[bool]]:
        c: list[ColumnElement[bool]] = []
        if not self.include_inactive:
            c.append(Sighting.is_active.is_(True))
        if not self.include_duplicates:
            c.append(Sighting.duplicate_of.is_(None))
        if self.start:
            c.append(Sighting.observed_at >= self.start)
        if self.end:
            c.append(Sighting.observed_at < self.end)
        if self.cities:
            c.append(Sighting.city.in_(self.cities))
        if self.regions:
            c.append(Sighting.region.in_(self.regions))
        if self.nations:
            c.append(Sighting.nation.in_(self.nations))
        if self.species:
            c.append(Sighting.species.in_(self.species))
        if self.near is not None:
            c.append(Sighting.city.in_(self.near_cities) if self.near_cities else literal(False))
        if self.text:
            like = f"%{self.text.lower()}%"
            c.append(
                or_(
                    func.lower(Sighting.city).like(like),
                    func.lower(Sighting.region).like(like),
                    func.lower(Sighting.species).like(like),
                    func.lower(Sighting.latin_name).like(like),
                )
            )
        return c

    def apply(self, stmt: Select) -> Select:
        conds = self.conditions()
        return stmt.where(and_(*conds)) if conds else stmt

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.start:
            out["start"] = self.start.isoformat()
        if self.end:
            out["end_exclusive"] = self.end.isoformat()
        for name in ("cities", "regions", "nations", "species"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.near:
            out["near"] = {
                "lat": self.near[0],
                "lon": self.near[1],
                "radius_km": self.near[2],
                "matched_cities": self.near_cities,
            }
        if self.text:
            out["q"] = self.text
        if self.include_duplicates:
            out["include_duplicates"] = True
        if self.include_inactive:
            out["include_inactive"] = True
        return out

    def cache_params(self) -> dict[str, Any]:
        return self.describe()


# ---------------------------------------------------------------------------
# Parameter parsing / validation
# ---------------------------------------------------------------------------


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def _parse_bound(raw: str | None, name: str, *, is_end: bool) -> datetime | None:
    if raw is None or raw == "":
        return None
    raw = raw.strip()
    dt = parse_datetime(raw)
    if dt is None:
        # allow "2021" and "2021-06" shorthands
        try:
            if len(raw) == 4:
                d = date(int(raw), 1, 1)
                dt = datetime(d.year + 1, 1, 1) if is_end else datetime(d.year, 1, 1)
                return dt
            if len(raw) == 7 and raw[4] == "-":
                y, m = int(raw[:4]), int(raw[5:])
                if is_end:
                    return datetime(y + (m == 12), m % 12 + 1, 1)
                return datetime(y, m, 1)
        except ValueError:
            pass
        raise InvalidQuery(
            f"'{name}' must be an ISO date or datetime (e.g. 2023-05-01 or 2023-05-01T09:30:00), "
            f"a month (2023-05) or a year (2023).",
            details={"parameter": name, "value": raw},
        )
    if is_end and len(raw) == 10:  # a bare date as the end means "through the end of that day"
        dt = dt + timedelta(days=1)
    return dt


def known_values(session: Session, column) -> list[str]:
    return [v for (v,) in session.execute(select(column).distinct()) if v]


def build_filters(
    session: Session,
    *,
    start: str | None = None,
    end: str | None = None,
    city: list[str] | None = None,
    region: list[str] | None = None,
    nation: list[str] | None = None,
    species: list[str] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    q: str | None = None,
    include_duplicates: bool = False,
    include_inactive: bool = False,
) -> SightingFilters:
    f = SightingFilters(include_duplicates=include_duplicates, include_inactive=include_inactive)
    f.start = _parse_bound(start, "start", is_end=False)
    f.end = _parse_bound(end, "end", is_end=True)
    if f.start and f.end and f.start >= f.end:
        raise InvalidQuery("'start' must be before 'end'.", details={"start": start, "end": end})

    cities = _split(city)
    if cities:
        db_cities = known_values(session, Sighting.city)
        resolved = []
        for raw in cities:
            match = resolve_location(raw)
            if match.place is None and match.name not in db_cities:
                raise InvalidQuery(
                    f"Unknown location '{raw}'.",
                    details={
                        "parameter": "city",
                        "suggestions": suggest_locations(raw, db_cities + list(PLACES)),
                    },
                    code="unknown_location",
                )
            resolved.append(match.name)
        f.cities = sorted(set(resolved))

    regions = _split(region)
    if regions:
        known = sorted({p.region for p in PLACES.values()} | {UNMAPPED_REGION})
        lookup = {r.lower(): r for r in known}
        for raw in regions:
            if raw.lower() not in lookup:
                raise InvalidQuery(
                    f"Unknown region '{raw}'.",
                    details={
                        "parameter": "region",
                        "suggestions": suggest_locations(raw, known),
                        "valid": known,
                    },
                    code="unknown_region",
                )
        f.regions = sorted({lookup[r.lower()] for r in regions})

    nations = _split(nation)
    if nations:
        known_n = sorted({p.nation for p in PLACES.values()})
        lookup_n = {n.lower(): n for n in known_n}
        bad = [n for n in nations if n.lower() not in lookup_n]
        if bad:
            raise InvalidQuery(
                f"Unknown nation '{bad[0]}'.", details={"valid": known_n}, code="unknown_nation"
            )
        f.nations = sorted({lookup_n[n.lower()] for n in nations})

    sp = _split(species)
    if sp:
        db_species = known_values(session, Sighting.species)
        by_lower = {s.lower(): s for s in db_species}
        resolved_sp = []
        for raw in sp:
            if raw.lower() in by_lower:
                resolved_sp.append(by_lower[raw.lower()])
                continue
            hit = lookup_species(raw)
            if hit is None and raw.lower() != UNIDENTIFIED_SPECIES.lower():
                raise InvalidQuery(
                    f"Unknown species '{raw}'.",
                    details={"parameter": "species", "suggestions": suggest_locations(raw, db_species)},
                    code="unknown_species",
                )
            resolved_sp.append(hit.common_name if hit else UNIDENTIFIED_SPECIES)
        f.species = sorted(set(resolved_sp))

    if (lat is None) != (lon is None):
        raise InvalidQuery(
            "Provide both 'lat' and 'lon' for a location search.", code="incomplete_coordinates"
        )
    if lat is not None and lon is not None:
        radius = radius_km if radius_km is not None else 50.0
        f.near = (lat, lon, radius)
        f.near_cities = [p.name for p, _ in places_within(lat, lon, radius)]

    f.text = q.strip() if q and q.strip() else None
    return f


# ---------------------------------------------------------------------------
# Time bucketing (the only dialect-specific SQL in the project)
# ---------------------------------------------------------------------------


def bucket_expr(interval: str, dialect: str):
    col = Sighting.observed_at
    if interval not in INTERVALS:
        raise InvalidQuery(f"interval must be one of {INTERVALS}", code="invalid_interval")
    if dialect == "postgresql":
        return func.to_char(func.date_trunc(interval, col), "YYYY-MM-DD")
    # SQLite
    if interval == "day":
        return func.strftime("%Y-%m-%d", col)
    if interval == "week":  # ISO week, Monday start
        return func.date(col, "weekday 0", "-6 days")
    if interval == "month":
        return func.strftime("%Y-%m-01", col)
    if interval == "quarter":
        month = cast(func.strftime("%m", col), Integer)
        return func.printf("%s-%02d-01", func.strftime("%Y", col), ((month - 1) // 3) * 3 + 1)
    return func.strftime("%Y-01-01", col)


def bucket_start(d: date, interval: str) -> date:
    if interval == "day":
        return d
    if interval == "week":
        return d - timedelta(days=d.weekday())
    if interval == "month":
        return d.replace(day=1)
    if interval == "quarter":
        return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    return date(d.year, 1, 1)


def next_bucket(d: date, interval: str) -> date:
    if interval == "day":
        return d + timedelta(days=1)
    if interval == "week":
        return d + timedelta(days=7)
    if interval == "month":
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    if interval == "quarter":
        m = d.month + 3
        return date(d.year + (m > 12), (m - 1) % 12 + 1, 1)
    return date(d.year + 1, 1, 1)


def bucket_range(first: date, last: date, interval: str, max_buckets: int = 20_000) -> list[date]:
    out, cur, end = [], bucket_start(first, interval), bucket_start(last, interval)
    while cur <= end and len(out) < max_buckets:
        out.append(cur)
        cur = next_bucket(cur, interval)
    return out


def bucket_label(d: date, interval: str) -> str:
    if interval == "week":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if interval == "month":
        return f"{d:%Y-%m}"
    if interval == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if interval == "year":
        return f"{d.year}"
    return d.isoformat()
