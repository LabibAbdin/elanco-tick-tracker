"""Plain-English briefing generated from the computed statistics.

Deterministic templates (rather than a generative model) guarantee every number in
the text is traceable to an endpoint and that nothing is hallucinated.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.insights import engine
from app.services import analytics, quality
from app.services.query import SightingFilters


def briefing(session: Session, f: SightingFilters, target_month: str | None = None) -> dict[str, Any]:
    s = analytics.summary(session, f)
    o = engine.overview(session, f)
    r = engine.risk(session, f, target_month)
    q = quality.report(session, sample_size=0)
    regions = analytics.by_group(session, f, "region")

    top_city = s["top_city"]
    top_species = s["top_species"]
    top_regions = regions["data"][:3]
    risk_top = r["cities"][:3]

    headline = (
        f"{s['total_sightings']:,} verified tick sightings across {s['cities']} UK cities "
        f"({s['first_sighting'][:4]}-{s['latest_sighting'][:4]})."
    )
    paragraphs = [
        headline + f" {top_city['key']} reports the most ({top_city['count']}, {top_city['share']:.1%}); "
        f"the most common species is the {top_species['key']} ({top_species['share']:.0%}).",
        "By region, "
        + ", ".join(f"{g['key']} ({g['count']})" for g in top_regions)
        + " lead; "
        + (
            "regional differences are larger than city counts explain."
            if regions["difference_test"]["significant"]
            else "once each region's number of cities is accounted for, regional differences are within chance."
        ),
        " ".join(o["findings"]),
        f"Activity outlook for {r['target_month']}: highest scores in "
        + ", ".join(f"{c['city']} ({c['score']:.0f}, {c['level']})" for c in risk_top)
        + ". "
        + " ".join(r["warnings"]),
    ]
    if q["quality_score"] is not None:
        rec = q["records"]
        paragraphs.append(
            f"Data quality: {q['quality_score']:.1f}% of received records were complete and valid as delivered; "
            f"{rec['rejected_quarantined']} were quarantined and {rec['probable_duplicates']} flagged as probable "
            "duplicates (excluded from statistics)."
        )
    return {
        "headline": headline,
        "paragraphs": paragraphs,
        "key_numbers": {
            "total_sightings": s["total_sightings"],
            "top_city": top_city,
            "top_species": top_species,
            "trend": o["trend"],
            "seasonality_significant": o["seasonality"]["significant"],
            "hotspots": [c["city"] for c in o["city_effects"] if c["classification"] == "hotspot"],
            "highest_risk": [{"city": c["city"], "score": c["score"], "level": c["level"]} for c in risk_top],
            "quality_score": q["quality_score"],
        },
        "generated_by": "deterministic templates over computed statistics (no generative model)",
    }
