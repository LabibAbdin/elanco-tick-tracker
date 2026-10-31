"""Validation, normalisation and de-duplication of raw upstream records.

Design principles
-----------------
* **Pure functions.** No I/O here, so every rule is unit-testable in isolation.
* **Keep what is usable, quarantine what is not.** A record is only rejected when
  it cannot support the core questions (when? where?). Everything else is kept
  and annotated with quality flags, so analysts can filter on them later.
* **Never silently change data.** Every repair (fuzzy-matched city, inferred
  species, generated id, ...) leaves a flag; the raw values are stored as lineage.

Decision table
--------------
==========================  =========================================================
Problem                     Handling
==========================  =========================================================
not a JSON object           reject ``not_an_object``
missing / unparseable date  reject ``missing_date`` / ``invalid_date``
date in the future / <1990  reject ``future_date`` / ``implausible_date``
missing location            reject ``missing_location``
location case/space/typo    normalise to canonical city, flag ``location_fuzzy_matched``
unknown town                keep (title-cased), flag ``location_unmapped``
species name missing        infer from Latin name (and vice versa), flag it
species names disagree      prefer Latin name, flag ``species_conflict_latin_preferred``
no species at all           keep as "Unidentified", flag ``species_missing``
missing id                  deterministic content-hash id, flag ``id_generated``
same id twice in a feed     keep the first copy (or a strictly more complete one), count it
same city+species+time      kept, but marked ``duplicate_of`` (see ``flag_near_duplicates``)
==========================  =========================================================
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from app.reference import (
    UNIDENTIFIED_SPECIES,
    UNMAPPED_NATION,
    UNMAPPED_REGION,
    resolve_location,
    resolve_species,
)

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "_id", "sighting_id", "sightingid", "uuid"),
    "date": ("date", "datetime", "timestamp", "observed_at", "observedat", "sighted_at", "time"),
    "location": ("location", "city", "town", "place"),
    "species": ("species", "common_name", "commonname", "species_name"),
    "latin": ("latinname", "latin_name", "scientific_name", "scientificname", "latin"),
}

_NULLISH = {"", "null", "none", "nan", "n/a", "na", "unknown", "-", "?"}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
)


@dataclass
class CleanRecord:
    id: str
    observed_at: datetime
    city: str
    region: str
    nation: str
    latitude: float | None
    longitude: float | None
    species: str
    latin_name: str
    raw_location: str | None
    raw_species: str | None
    flags: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        payload = f"{self.observed_at.isoformat()}|{self.city}|{self.species}|{self.latin_name}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def completeness(self) -> int:
        return -len(self.flags)


@dataclass
class Rejection:
    source_id: str | None
    reasons: list[str]
    raw: Any

    def raw_json(self) -> str:
        try:
            return json.dumps(self.raw, default=str)[:4000]
        except Exception:  # pragma: no cover - defensive
            return repr(self.raw)[:4000]


@dataclass
class CleanResult:
    records: list[CleanRecord]
    rejections: list[Rejection]
    stats: Counter

    @property
    def accepted(self) -> int:
        return len(self.records)


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = " ".join(value.split())  # trim + collapse internal whitespace (faster than a regex)
    return None if value.lower() in _NULLISH else value


@lru_cache(maxsize=1024)
def _normalise_key(key: str) -> str:
    return re.sub(r"[\s_\-]", "", key.lower())


def _normalise_keys(item: dict[str, Any]) -> dict[str, Any]:
    """Lower-case keys and drop separators so latinName / latin_name / Latin-Name all match."""
    return {_normalise_key(str(k)): v for k, v in item.items()}


def _pick(keys: dict[str, Any], key: str) -> Any:
    for alias in FIELD_ALIASES[key]:
        value = keys.get(alias.replace("_", ""))
        if value is not None:
            return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse the many shapes a timestamp arrives in. Returns naive UTC-ish datetimes.

    Timezone-aware inputs are converted to UTC and made naive so they compare with
    the upstream's naive ISO timestamps. Epoch seconds and milliseconds are accepted.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, int | float):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        try:
            dt = datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = _clean_str(value)
        if not text:
            return None
        if re.fullmatch(r"\d{10}(\d{3})?", text):
            return parse_datetime(int(text))
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = None
            for fmt in _DATE_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def generate_id(observed_at: datetime | None, location: str | None, species: str | None) -> str:
    raw = f"{observed_at}|{location}|{species}".lower()
    return "gen_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def clean_record(item: Any, *, now: datetime, min_year: int) -> CleanRecord | Rejection:
    if not isinstance(item, dict):
        return Rejection(None, ["not_an_object"], item)

    reasons: list[str] = []
    flags: list[str] = []

    keys = _normalise_keys(item)
    raw_id = _clean_str(_pick(keys, "id"))
    raw_date = _pick(keys, "date")
    raw_location = _clean_str(_pick(keys, "location"))
    raw_species = _clean_str(_pick(keys, "species"))
    raw_latin = _clean_str(_pick(keys, "latin"))

    observed_at = parse_datetime(raw_date)
    if raw_date is None or (isinstance(raw_date, str) and _clean_str(raw_date) is None):
        reasons.append("missing_date")
    elif observed_at is None:
        reasons.append("invalid_date")
    elif observed_at > now + timedelta(days=1):
        reasons.append("future_date")
    elif observed_at.year < min_year:
        reasons.append("implausible_date")

    if raw_location is None:
        reasons.append("missing_location")

    if reasons:
        return Rejection(raw_id, reasons, item)
    assert observed_at is not None and raw_location is not None

    loc = resolve_location(raw_location)
    if loc.method == "fuzzy":
        flags.append("location_fuzzy_matched")
    elif loc.method == "unmapped":
        flags.append("location_unmapped")

    species, species_flags = resolve_species(raw_species, raw_latin)
    flags.extend(species_flags)

    species_name = species.common_name if species else UNIDENTIFIED_SPECIES
    latin_name = species.latin_name if species else ""

    record_id = raw_id
    if not record_id:
        record_id = generate_id(observed_at, loc.name, species_name)
        flags.append("id_generated")

    return CleanRecord(
        id=record_id[:64],
        observed_at=observed_at,
        city=loc.name,
        region=loc.place.region if loc.place else UNMAPPED_REGION,
        nation=loc.place.nation if loc.place else UNMAPPED_NATION,
        latitude=loc.place.lat if loc.place else None,
        longitude=loc.place.lon if loc.place else None,
        species=species_name,
        latin_name=latin_name,
        raw_location=raw_location,
        raw_species=raw_species or raw_latin,
        flags=flags,
    )


def clean_batch(items: Iterable[Any], *, now: datetime | None = None, min_year: int = 1990) -> CleanResult:
    """Clean a batch of raw items, collapsing duplicate ids within the batch."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    stats: Counter = Counter()
    by_id: dict[str, CleanRecord] = {}
    rejections: list[Rejection] = []

    for item in items:
        stats["seen"] += 1
        result = clean_record(item, now=now, min_year=min_year)
        if isinstance(result, Rejection):
            rejections.append(result)
            for r in result.reasons:
                stats[f"rejected:{r}"] += 1
            continue
        for f in result.flags:
            stats[f"flag:{f}"] += 1
        existing = by_id.get(result.id)
        if existing is not None:
            stats["duplicate_id_in_batch"] += 1
            if existing.content_hash != result.content_hash:
                stats["duplicate_id_conflicting_content"] += 1
            # keep the first copy unless a later one is strictly more complete
            if result.completeness > existing.completeness:
                by_id[result.id] = result
            continue
        by_id[result.id] = result

    stats["accepted"] = len(by_id)
    stats["rejected"] = len(rejections)
    return CleanResult(records=list(by_id.values()), rejections=rejections, stats=stats)


@dataclass(frozen=True)
class DupCandidate:
    id: str
    city: str
    species: str
    observed_at: datetime


def resolve_duplicate_chains(prev_of: dict[str, str]) -> dict[str, str]:
    """Collapse "x is a re-report of the report just before it" links to the cluster's first report.

    ``prev_of`` maps a row id to the id of the previous report (same city and species)
    when the two are within the duplicate window. A burst A -> B -> C becomes
    {B: A, C: A}: the earliest report is canonical, the rest point at it.
    """
    roots: dict[str, str] = {}
    for rid in prev_of:
        chain, cur = [], rid
        while cur in prev_of and cur not in roots:
            chain.append(cur)
            cur = prev_of[cur]
        root = roots.get(cur, cur)
        for c in chain:
            roots[c] = root
    return roots


def flag_near_duplicates(rows: Sequence[DupCandidate], window_s: int) -> dict[str, str | None]:
    """Reference (pure-Python) implementation of probable-duplicate detection.

    Two reports are linked when they share city and species and the later one arrives
    within ``window_s`` seconds of the report immediately before it; linked bursts are
    collapsed onto their earliest report. Returns ``{id: duplicate_of_or_None}``.
    The pipeline runs the same logic inside the database with a window function
    (``LAG ... OVER (PARTITION BY city, species ORDER BY observed_at)``), so memory use
    does not grow with the table; a test checks that both implementations agree.
    """
    window_s = max(window_s, 0)
    ordered = sorted(rows, key=lambda r: (r.city, r.species, r.observed_at, r.id))
    prev_of: dict[str, str] = {}
    for prev, row in zip(ordered, ordered[1:], strict=False):
        if (
            row.city == prev.city
            and row.species == prev.species
            and (row.observed_at - prev.observed_at).total_seconds() <= window_s
        ):
            prev_of[row.id] = prev.id
    roots = resolve_duplicate_chains(prev_of)
    return {r.id: roots.get(r.id) for r in rows}
