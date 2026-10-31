"""Data-quality report: what came in, what was repaired, what was rejected and why."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import IngestionRun, RejectedRecord, Sighting

KNOWN_FLAGS = {
    "location_fuzzy_matched": "City name had a typo and was matched to a known city",
    "location_unmapped": "City not in the gazetteer (kept, but has no region/coordinates)",
    "species_inferred_from_latin": "Common name missing; inferred from Latin name",
    "latin_inferred_from_species": "Latin name missing; inferred from common name",
    "species_conflict_latin_preferred": "Common and Latin names disagreed; Latin name kept",
    "species_common_name_unrecognised": "Common name not recognised; Latin name used",
    "latin_name_unrecognised": "Latin name not recognised; common name used",
    "species_unrecognised": "Species not in catalogue (kept as reported)",
    "species_missing": "No species given (kept as 'Unidentified')",
    "id_generated": "No id supplied; deterministic id generated from content",
}

REJECTION_REASONS = {
    "not_an_object": "Item was not a JSON object",
    "missing_date": "No date supplied",
    "invalid_date": "Date could not be parsed",
    "future_date": "Date is in the future",
    "implausible_date": "Date is implausibly old",
    "missing_location": "No location supplied",
}


def report(session: Session, sample_size: int = 10) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(Sighting)) or 0
    active = session.scalar(select(func.count()).select_from(Sighting).where(Sighting.is_active)) or 0
    dups = (
        session.scalar(
            select(func.count())
            .select_from(Sighting)
            .where(Sighting.is_active, Sighting.duplicate_of.is_not(None))
        )
        or 0
    )
    usable = active - dups

    flag_counts = {}
    for flag, meaning in KNOWN_FLAGS.items():
        n = (
            session.scalar(
                select(func.count())
                .select_from(Sighting)
                .where(Sighting.is_active, Sighting.quality_flags.like(f"%{flag}%"))
            )
            or 0
        )
        if n:
            flag_counts[flag] = {"count": n, "meaning": meaning}
    clean = (
        session.scalar(
            select(func.count())
            .select_from(Sighting)
            .where(Sighting.is_active, Sighting.duplicate_of.is_(None), Sighting.quality_flags == "")
        )
        or 0
    )

    reasons: Counter = Counter()
    for reason_str, n in session.execute(
        select(RejectedRecord.reasons, func.count()).group_by(RejectedRecord.reasons)
    ):
        for r in reason_str.split(","):
            reasons[r] += n
    rejected_total = session.scalar(select(func.count()).select_from(RejectedRecord)) or 0
    samples = session.scalars(
        select(RejectedRecord).order_by(RejectedRecord.pk.desc()).limit(sample_size)
    ).all()

    def pct(n: int, d: int) -> float | None:
        return round(n / d * 100, 2) if d else None

    mapped = (
        session.scalar(
            select(func.count())
            .select_from(Sighting)
            .where(Sighting.is_active, Sighting.latitude.is_not(None))
        )
        or 0
    )
    identified = (
        session.scalar(
            select(func.count())
            .select_from(Sighting)
            .where(Sighting.is_active, Sighting.species != "Unidentified")
        )
        or 0
    )

    received = active + rejected_total
    return {
        "quality_score": pct(clean, received) if received else None,
        "quality_score_definition": "share of all received records that arrived complete, valid and unique "
        "(no repairs needed)",
        "records": {
            "stored": total,
            "active": active,
            "inactive_removed_upstream": total - active,
            "probable_duplicates": dups,
            "usable_for_analytics": usable,
            "clean_without_repairs": clean,
            "rejected_quarantined": rejected_total,
        },
        "completeness_pct": {
            "date": 100.0 if active else None,  # guaranteed by validation
            "location": 100.0 if active else None,
            "location_mapped_to_region": pct(mapped, active),
            "species_identified": pct(identified, active),
        },
        "repairs": flag_counts,
        "rejections": [
            {"reason": r, "count": n, "meaning": REJECTION_REASONS.get(r, r)}
            for r, n in reasons.most_common()
        ],
        "recent_rejections": [
            {
                "source_id": s.source_id,
                "reasons": s.reasons.split(","),
                "raw": _safe_json(s.raw_payload),
                "at": s.created_at.isoformat(),
            }
            for s in samples
        ],
        "recent_runs": [
            _run_dict(r)
            for r in session.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(5))
        ],
    }


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _run_dict(r: IngestionRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "trigger": r.trigger,
        "strategy": r.strategy,
        "source": r.source,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms,
        "fetched": r.fetched,
        "inserted": r.inserted,
        "updated": r.updated,
        "unchanged": r.unchanged,
        "rejected": r.rejected,
        "duplicates_flagged": r.duplicates_flagged,
        "deactivated": r.deactivated,
        "warnings": _safe_json(r.warnings or "[]"),
        "quality": _safe_json(r.quality or "{}"),
        "error": r.error,
    }
