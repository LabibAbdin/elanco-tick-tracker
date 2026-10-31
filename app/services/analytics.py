"""Aggregate insights computed in SQL (GROUP BY) so they scale with the database,
not with Python memory. Every function takes the shared ``SightingFilters``.

Where a descriptive statistic could be over-read, a significance test is attached
(e.g. "is this city really higher than the others, or is it noise?").
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from scipy import stats
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.errors import InvalidQuery
from app.insights.glm import poisson_trend
from app.models import IngestionRun, Sighting
from app.reference import PLACES, UNMAPPED_NATION, UNMAPPED_REGION
from app.services.query import (
    SightingFilters,
    bucket_expr,
    bucket_label,
    bucket_range,
)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GROUP_COLUMNS = {
    "city": Sighting.city,
    "region": Sighting.region,
    "nation": Sighting.nation,
    "species": Sighting.species,
}


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


def _round(x: float | None, n: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def uniformity_test(counts: list[int]) -> dict[str, Any]:
    """Chi-square goodness-of-fit against 'all groups equally likely'."""
    if len(counts) < 2 or sum(counts) == 0:
        return {"test": "chi_square_uniform", "p_value": None, "significant": False}
    chi2, p = stats.chisquare(counts)
    return {
        "test": "chi_square_uniform",
        "statistic": _round(float(chi2), 3),
        "p_value": _round(float(p), 4),
        "significant": bool(p < 0.05),
    }


def _date_span(session: Session, f: SightingFilters) -> tuple[datetime | None, datetime | None, int]:
    row = session.execute(
        f.apply(select(func.min(Sighting.observed_at), func.max(Sighting.observed_at), func.count()))
    ).one()
    return row[0], row[1], row[2]


# ---------------------------------------------------------------------------


def summary(session: Session, f: SightingFilters) -> dict[str, Any]:
    first, last, total = _date_span(session, f)
    by_city = _group_counts(session, f, "city")
    by_species = _group_counts(session, f, "species")
    by_nation = _group_counts(session, f, "nation")
    years = ((last - first).days / 365.25) if first and last and last > first else None

    flagged_dups = session.scalar(
        select(func.count())
        .select_from(Sighting)
        .where(Sighting.is_active, Sighting.duplicate_of.is_not(None))
    )
    last_run = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.status.in_(("success", "partial")))
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).first()

    recent = None
    if last:
        # compare the last 12 months *of data* with the 12 before (data can lag real time)
        cutoff = date(last.year - 1, last.month, 1)
        prior_cutoff = date(last.year - 2, last.month, 1)

        def _count(lo: date, hi: date | None) -> int:
            stmt = f.apply(select(func.count()).select_from(Sighting)).where(Sighting.observed_at >= lo)
            if hi:
                stmt = stmt.where(Sighting.observed_at < hi)
            return session.scalar(stmt) or 0

        last12, prev12 = _count(cutoff, None), _count(prior_cutoff, cutoff)
        recent = {
            "window": f"{cutoff:%Y-%m} to {last:%Y-%m}",
            "last_12_months": last12,
            "previous_12_months": prev12,
            "change_pct": _round((last12 - prev12) / prev12 * 100, 1) if prev12 else None,
        }

    return {
        "total_sightings": total,
        "first_sighting": first.isoformat() if first else None,
        "latest_sighting": last.isoformat() if last else None,
        "days_since_latest_sighting": (datetime.now() - last).days if last else None,
        "years_covered": _round(years, 1),
        "average_per_month": _round(total / (years * 12), 2) if years else None,
        "cities": len(by_city),
        "species": len(by_species),
        "top_city": by_city[0] if by_city else None,
        "top_species": by_species[0] if by_species else None,
        "by_nation": by_nation,
        "recent_change": recent,
        "probable_duplicates_excluded": flagged_dups or 0,
        "last_sync": {
            "at": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None,
            "source": last_run.source if last_run else None,
            "status": last_run.status if last_run else None,
        },
    }


def _group_counts(session: Session, f: SightingFilters, group_by: str) -> list[dict[str, Any]]:
    col = GROUP_COLUMNS[group_by]
    rows = session.execute(
        f.apply(select(col.label("key"), func.count().label("n")).group_by(col)).order_by(
            func.count().desc(), col
        )
    ).all()
    total = sum(r.n for r in rows) or 1
    return [{"key": r.key, "count": r.n, "share": round(r.n / total, 4)} for r in rows]


def by_group(session: Session, f: SightingFilters, group_by: str) -> dict[str, Any]:
    if group_by not in GROUP_COLUMNS:
        raise InvalidQuery(f"group_by must be one of {list(GROUP_COLUMNS)}", code="invalid_group_by")
    rows = _group_counts(session, f, group_by)
    first, last, total = _date_span(session, f)
    years = max((last - first).days / 365.25, 1 / 12) if first and last else None
    mean = total / len(rows) if rows else 0

    geo: dict[str, tuple] = {}
    if group_by == "city":
        geo = {
            r.city: (r.region, r.nation, r.latitude, r.longitude)
            for r in session.execute(
                select(
                    Sighting.city, Sighting.region, Sighting.nation, Sighting.latitude, Sighting.longitude
                ).distinct()
            )
        }
    latin = {}
    if group_by == "species":
        latin = dict(session.execute(select(Sighting.species, Sighting.latin_name).distinct()).all())

    data = []
    for rank, r in enumerate(rows, start=1):
        item = {
            "rank": rank,
            **r,
            "per_year": _round(r["count"] / years, 2) if years else None,
            "vs_average": _round(r["count"] / mean, 3) if mean else None,
        }
        if group_by == "city":
            region, nation, lat, lon = geo.get(r["key"], (None, None, None, None))
            item.update(region=region, nation=nation, lat=lat, lon=lon)
        if group_by == "species":
            item["latin_name"] = latin.get(r["key"])
        data.append(item)

    return {
        "group_by": group_by,
        "total": total,
        "groups": len(rows),
        "data": data,
        "difference_test": _difference_test(session, f, group_by, rows),
    }


def _difference_test(session: Session, f: SightingFilters, group_by: str, rows: list[dict]) -> dict[str, Any]:
    if group_by in ("region", "nation"):
        # unknown towns are not a real region; keep them out of the comparison
        rows = [r for r in rows if r["key"] not in (UNMAPPED_REGION, UNMAPPED_NATION)]
    counts = [r["count"] for r in rows]
    if group_by in ("region", "nation") and len(rows) >= 2 and sum(counts):
        # Regions contain different numbers of cities, so "equal counts" is the wrong null.
        # Expect counts proportional to how many cities each group contributes.
        col = GROUP_COLUMNS[group_by]
        n_cities = dict(
            session.execute(
                f.apply(select(col, func.count(func.distinct(Sighting.city))).group_by(col))
            ).all()
        )
        weights = [n_cities.get(r["key"], 1) for r in rows]
        expected = [sum(counts) * w / sum(weights) for w in weights]
        chi2, p = stats.chisquare(counts, expected)
        return {
            "test": "chi_square_vs_city_count",
            "statistic": _round(float(chi2), 3),
            "p_value": _round(float(p), 4),
            "significant": bool(p < 0.05),
            "question": f"Do {group_by}s differ by more than their number of cities explains?",
        }
    return {
        **uniformity_test(counts),
        "question": f"Do sightings differ between {group_by} groups by more than chance?",
    }


def by_city_geojson(session: Session, f: SightingFilters) -> dict[str, Any]:
    result = by_group(session, f, "city")
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {k: v for k, v in d.items() if k not in ("lat", "lon")},
        }
        for d in result["data"]
        if d.get("lat") is not None
    ]
    return {"type": "FeatureCollection", "features": features, "properties": {"total": result["total"]}}


def trends(
    session: Session,
    f: SightingFilters,
    interval: str,
    split_by: str | None = None,
    top: int = 5,
    moving_average: int = 3,
) -> dict[str, Any]:
    bucket = bucket_expr(interval, _dialect(session)).label("bucket")
    cols = [bucket]
    split_col = None
    if split_by:
        if split_by not in GROUP_COLUMNS:
            raise InvalidQuery(f"split_by must be one of {list(GROUP_COLUMNS)}", code="invalid_split_by")
        split_col = GROUP_COLUMNS[split_by].label("series")
        cols.append(split_col)
    stmt = f.apply(select(*cols, func.count().label("n"))).group_by(*cols)
    rows = session.execute(stmt).all()

    # Align every series to the dataset's span (not the filtered subset's), so a city whose
    # last sighting was in March still shows the zero months after it.
    span = SightingFilters(
        start=f.start, end=f.end, include_duplicates=f.include_duplicates, include_inactive=f.include_inactive
    )
    span_first, span_last, _ = _date_span(session, span)
    first_dt = f.start.date() if f.start else (span_first.date() if span_first else None)
    last_dt = span_last.date() if span_last else None
    if f.end and last_dt:
        last_dt = min(last_dt, (f.end - timedelta(seconds=1)).date())
    if not first_dt or not last_dt or first_dt > last_dt:
        return {"interval": interval, "split_by": split_by, "series": [], "summary": None}

    buckets = bucket_range(first_dt, last_dt, interval)
    if len(buckets) >= 20_000:
        raise InvalidQuery(
            "Too many buckets; use a wider interval or a narrower date range.", code="too_many_buckets"
        )

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        key = r.series if split_col is not None else "all"
        counts[key][r.bucket] += r.n

    # keep the top-N series and roll the rest into "Other" so charts stay readable
    ranked = sorted(counts, key=lambda k: -sum(counts[k].values()))
    if split_by and len(ranked) > top:
        other = defaultdict(int)
        for k in ranked[top:]:
            for b, n in counts[k].items():
                other[b] += n
        counts = {k: counts[k] for k in ranked[:top]}
        counts["Other"] = other
        ranked = ranked[:top] + ["Other"]

    series = []
    for key in ranked:
        values = [counts[key].get(b.isoformat(), 0) for b in buckets]
        points = []
        for i, (b, v) in enumerate(zip(buckets, values, strict=True)):
            window = values[max(0, i - moving_average + 1) : i + 1]
            points.append(
                {
                    "period": b.isoformat(),
                    "label": bucket_label(b, interval),
                    "count": v,
                    "moving_avg": round(sum(window) / len(window), 2),
                }
            )
        series.append(
            {"key": key, "total": sum(values), "points": points, "trend": _linear_trend(buckets, values)}
        )

    totals = [sum(s["points"][i]["count"] for s in series) for i in range(len(buckets))]
    peak_i = max(range(len(totals)), key=lambda i: totals[i]) if totals else None
    return {
        "interval": interval,
        "split_by": split_by,
        "moving_average_window": moving_average,
        "periods": len(buckets),
        "series": series,
        "summary": {
            "total": sum(totals),
            "mean_per_period": _round(sum(totals) / len(totals), 3) if totals else None,
            "zero_periods": sum(1 for t in totals if t == 0),
            "peak": {
                "period": buckets[peak_i].isoformat(),
                "label": bucket_label(buckets[peak_i], interval),
                "count": totals[peak_i],
            }
            if peak_i is not None
            else None,
            "trend": _linear_trend(buckets, totals),
        },
    }


def _linear_trend(buckets: list[date], values: list[int]) -> dict[str, Any]:
    """Trend test on a count series via a log-linear Poisson model (see app.insights.glm)."""
    if not buckets:
        return {"status": "insufficient_data", "significant": False, "direction": "unknown"}
    t0 = buckets[0].toordinal()
    return poisson_trend([(b.toordinal() - t0) / 365.25 for b in buckets], values)


def date_part(part: str, dialect: str):
    """Portable EXTRACT(): month 1-12, year, hour 0-23, dow 0=Sunday."""
    col = Sighting.observed_at
    if dialect == "postgresql":
        return cast(func.extract({"dow": "dow"}.get(part, part), col), Integer)
    fmt = {"month": "%m", "year": "%Y", "hour": "%H", "dow": "%w"}[part]
    return cast(func.strftime(fmt, col), Integer)


def seasonality(session: Session, f: SightingFilters) -> dict[str, Any]:
    d = _dialect(session)
    month, hour, dow = date_part("month", d), date_part("hour", d), date_part("dow", d)

    def dist(expr, size: int, offset: int = 0) -> list[int]:
        out = [0] * size
        for k, n in session.execute(f.apply(select(expr, func.count()).group_by(expr))).all():
            out[int(k) - offset] += n
        return out

    months = dist(month, 12, offset=1)
    hours = dist(hour, 24)
    dows_sun0 = dist(dow, 7)
    dows = dows_sun0[1:] + dows_sun0[:1]  # Monday first
    total = sum(months)
    mean_m = total / 12 if total else 0
    test = uniformity_test(months)

    month_rows = [
        {
            "month": i + 1,
            "name": MONTH_NAMES[i],
            "count": c,
            "share": _round(c / total, 4) if total else 0,
            "index": _round(c / mean_m, 3) if mean_m else None,
        }
        for i, c in enumerate(months)
    ]
    peak = sorted(month_rows, key=lambda r: -r["count"])[:3]
    seasons = {
        "Winter (Dec-Feb)": months[11] + months[0] + months[1],
        "Spring (Mar-May)": sum(months[2:5]),
        "Summer (Jun-Aug)": sum(months[5:8]),
        "Autumn (Sep-Nov)": sum(months[8:11]),
    }
    return {
        "total": total,
        "by_month": month_rows,
        "by_season": [
            {"season": k, "count": v, "share": _round(v / total, 4) if total else 0}
            for k, v in seasons.items()
        ],
        "by_weekday": [{"day": WEEKDAYS[i], "count": c} for i, c in enumerate(dows)],
        "by_hour": [{"hour": h, "count": c} for h, c in enumerate(hours)],
        "peak_months": [p["name"] for p in peak],
        "seasonality_test": {
            **test,
            "question": "Are sightings spread unevenly across the calendar year?",
            "interpretation": (
                "Sightings cluster in particular months (seasonal pattern detected)."
                if test["significant"]
                else "No statistically significant seasonal pattern: month-to-month differences are "
                "consistent with random variation."
            ),
        },
    }


HEATMAP_AXES = ("city", "region", "nation", "species", "month", "year")


def heatmap(session: Session, f: SightingFilters, rows: str, cols: str) -> dict[str, Any]:
    if rows not in HEATMAP_AXES or cols not in HEATMAP_AXES or rows == cols:
        raise InvalidQuery(f"rows/cols must be two different values from {HEATMAP_AXES}", code="invalid_axes")

    def axis_expr(name: str):
        if name in GROUP_COLUMNS:
            return GROUP_COLUMNS[name]
        return date_part(name, _dialect(session))

    r_expr, c_expr = axis_expr(rows).label("r"), axis_expr(cols).label("c")
    data = session.execute(
        f.apply(select(r_expr, c_expr, func.count().label("n"))).group_by(r_expr, c_expr)
    ).all()
    row_keys = sorted({d.r for d in data}, key=_sort_key)
    col_keys = sorted({d.c for d in data}, key=_sort_key)
    if cols == "month":
        col_keys = list(range(1, 13))
    lookup = {(d.r, d.c): d.n for d in data}
    matrix = [[lookup.get((r, c), 0) for c in col_keys] for r in row_keys]
    col_labels = [MONTH_NAMES[c - 1] if cols == "month" else str(c) for c in col_keys]
    row_labels = [MONTH_NAMES[r - 1] if rows == "month" else str(r) for r in row_keys]
    flat = [v for row in matrix for v in row]
    return {
        "rows": rows,
        "cols": cols,
        "row_labels": row_labels,
        "col_labels": col_labels,
        "matrix": matrix,
        "row_totals": [sum(r) for r in matrix],
        "col_totals": [sum(matrix[i][j] for i in range(len(matrix))) for j in range(len(col_keys))],
        "max_cell": max(flat) if flat else 0,
        "independence_test": _independence(matrix),
    }


def _independence(matrix: list[list[int]]) -> dict[str, Any]:
    pruned = [r for r in matrix if sum(r) > 0]
    if len(pruned) < 2 or not pruned or len(pruned[0]) < 2:
        return {"test": "chi_square_independence", "p_value": None, "significant": False}
    cols_nonzero = [j for j in range(len(pruned[0])) if any(r[j] for r in pruned)]
    pruned = [[r[j] for j in cols_nonzero] for r in pruned]
    if len(cols_nonzero) < 2:
        return {"test": "chi_square_independence", "p_value": None, "significant": False}
    chi2, p, dof, _ = stats.chi2_contingency(pruned)
    return {
        "test": "chi_square_independence",
        "statistic": _round(float(chi2), 3),
        "dof": int(dof),
        "p_value": _round(float(p), 4),
        "significant": bool(p < 0.05),
        "question": "Does the column distribution depend on the row (e.g. species mix differs by city)?",
    }


def _sort_key(v: Any):
    return (0, v) if isinstance(v, int | float) else (1, str(v))


def locations(session: Session) -> list[dict[str, Any]]:
    counts = dict(
        session.execute(
            select(Sighting.city, func.count())
            .where(Sighting.is_active, Sighting.duplicate_of.is_(None))
            .group_by(Sighting.city)
        ).all()
    )
    out = []
    names = sorted(set(counts) | {p for p in PLACES if p in counts})
    for name in names:
        p = PLACES.get(name)
        out.append(
            {
                "city": name,
                "region": p.region if p else None,
                "nation": p.nation if p else None,
                "lat": p.lat if p else None,
                "lon": p.lon if p else None,
                "sightings": counts.get(name, 0),
            }
        )
    return out


def regions(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Sighting.region, Sighting.nation, func.count(func.distinct(Sighting.city)), func.count())
        .where(Sighting.is_active, Sighting.duplicate_of.is_(None))
        .group_by(Sighting.region, Sighting.nation)
        .order_by(Sighting.region)
    ).all()
    return [{"region": r, "nation": n, "cities": c, "sightings": k} for r, n, c, k in rows]
