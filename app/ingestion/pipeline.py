"""Ingestion orchestrator: fetch -> clean -> upsert -> reconcile -> audit.

Source strategy (``strategy="auto"``), each step only runs if the previous failed:

1. **full**        stream ``/tick-sightings`` in batches (one request, memory-flat).
2. **partitioned** fetch ``/tick-sightings/city/{city}`` per known city with bounded
                   concurrency - smaller requests survive where a big one times out.
3. **keep / snapshot**  if upstream is unreachable we keep serving what we already
                   have (marked stale in /health/ready); if the database is empty we
                   load the bundled snapshot so the API is never blank.

Guarantees
----------
* **Idempotent**: re-running a sync, or a retried stream re-delivering batches,
  never creates duplicates (upsert keyed by upstream id + content hash).
* **Safe deletes**: records that vanish upstream are *soft*-deactivated, and only
  after a *complete* full sync - never after a partial or snapshot load. A sanity
  valve refuses to deactivate more than half the dataset in one go (protects us
  from an upstream bug returning a near-empty list).
* **Auditable**: every run is logged in ``ingestion_runs``; every rejected record is
  quarantined with its reason codes and raw payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.ingestion.cleaning import clean_batch, resolve_duplicate_chains
from app.ingestion.client import UpstreamClient, UpstreamError
from app.models import AppMeta, IngestionRun, RejectedRecord, Sighting
from app.reference import DATASET_CITIES

log = logging.getLogger(__name__)

MAX_QUARANTINE_PER_RUN = 5000
MAX_DEACTIVATION_SHARE = 0.5


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class RunReport:
    run_id: int
    trigger: str
    strategy: str
    source: str | None = None
    status: str = "running"
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    duplicates_flagged: int = 0
    deactivated: int = 0
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    quality: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _BatchWriter:
    """Applies cleaned batches to the database (runs in a worker thread)."""

    def __init__(self, settings: Settings, report: RunReport, run_started: datetime, source: str):
        self.settings = settings
        self.report = report
        self.run_started = run_started
        self.source = source
        self.seen_ids: set[str] = set()
        self.seen_rejections: set[str] = set()
        self.quality: Counter = Counter()
        self.quarantined = 0

    def write(self, raw_items: list[Any]) -> None:
        self.report.fetched += len(raw_items)
        result = clean_batch(raw_items, now=utcnow(), min_year=self.settings.min_valid_year)
        self.quality["duplicate_id_in_batch"] += result.stats.get("duplicate_id_in_batch", 0)

        # Same id already processed in this run: a duplicate later in the feed, or a batch
        # re-delivered after a stream restart. The first copy wins either way.
        fresh = [r for r in result.records if r.id not in self.seen_ids]
        self.quality["duplicate_id_skipped"] += len(result.records) - len(fresh)
        self.seen_ids.update(r.id for r in fresh)
        # Rejected items have no trustworthy id, so de-duplicate them by payload.
        new_rejections = []
        for rej in result.rejections:
            key = hashlib.sha1(rej.raw_json().encode()).hexdigest()
            if key not in self.seen_rejections:
                self.seen_rejections.add(key)
                new_rejections.append(rej)

        for r in fresh:
            self.quality.update(f"flag:{f}" for f in r.flags)
        for rej in new_rejections:
            self.quality.update(f"rejected:{reason}" for reason in rej.reasons)

        with session_scope() as s:
            self._quarantine(s, new_rejections)
            if fresh:
                self._upsert(s, fresh)

    def _quarantine(self, s: Session, rejections) -> None:
        self.report.rejected += len(rejections)
        room = MAX_QUARANTINE_PER_RUN - self.quarantined
        if room <= 0 or not rejections:
            return
        rows = [
            {
                "run_id": self.report.run_id,
                "source_id": rej.source_id,
                "reasons": ",".join(rej.reasons),
                "raw_payload": rej.raw_json(),
                "created_at": utcnow(),
            }
            for rej in rejections[:room]
        ]
        s.execute(RejectedRecord.__table__.insert(), rows)
        self.quarantined += len(rows)

    def _upsert(self, s: Session, records) -> None:
        ids = [r.id for r in records]
        existing = {
            row.id: (row.content_hash, row.quality_flags)
            for row in s.execute(
                select(Sighting.id, Sighting.content_hash, Sighting.quality_flags).where(Sighting.id.in_(ids))
            )
        }
        now = utcnow()
        inserts, updates, touched = [], [], []
        for r in records:
            values = {
                "id": r.id,
                "observed_at": r.observed_at,
                "city": r.city,
                "region": r.region,
                "nation": r.nation,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "species": r.species,
                "latin_name": r.latin_name,
                "raw_location": (r.raw_location or "")[:200],
                "raw_species": (r.raw_species or "")[:200],
                "quality_flags": ",".join(r.flags),
                "content_hash": r.content_hash,
                "source": self.source,
                "is_active": True,
                "last_seen_at": self.run_started,
                "updated_at": now,
            }
            if r.id not in existing:
                values["first_seen_at"] = now
                values["duplicate_of"] = None
                inserts.append(values)
            elif existing[r.id] != (r.content_hash, values["quality_flags"]):
                updates.append(values)
            else:
                touched.append(r.id)

        if inserts:
            s.execute(Sighting.__table__.insert(), inserts)
        if updates:
            s.execute(update(Sighting), updates)  # ORM bulk UPDATE by primary key
        for chunk in _chunks(touched, 900):  # stay under SQLite's bound-parameter limit
            s.execute(
                update(Sighting)
                .where(Sighting.id.in_(chunk))
                .values(last_seen_at=self.run_started, is_active=True, source=self.source)
                .execution_options(synchronize_session=False)
            )
        self.report.inserted += len(inserts)
        self.report.updated += len(updates)
        self.report.unchanged += len(touched)


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


STRATEGY_PLANS: dict[str, list[str]] = {
    "auto": ["full", "partitioned", "fallback"],
    "full": ["full"],
    "partitioned": ["partitioned"],
    "snapshot": ["snapshot"],
}
SOURCE_LABEL = {"full": "api", "partitioned": "api_partitioned", "snapshot": "snapshot", "fallback": "cache"}


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        client: UpstreamClient,
        on_data_changed: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.on_data_changed = on_data_changed or (lambda: None)
        self._lock = asyncio.Lock()
        self._dup_changed = False
        self.last_report: RunReport | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    # -- entry point ---------------------------------------------------------

    async def sync(self, trigger: str = "manual", strategy: str = "auto") -> RunReport:
        if self._lock.locked():
            raise RuntimeError("a sync is already running")
        async with self._lock:
            return await self._sync(trigger, strategy)

    async def _sync(self, trigger: str, strategy: str) -> RunReport:
        started_clock = time.perf_counter()
        run_started = utcnow()
        run_id = await asyncio.to_thread(self._open_run, trigger, strategy, run_started)
        report = RunReport(run_id=run_id, trigger=trigger, strategy=strategy)
        writer = _BatchWriter(self.settings, report, run_started, source="api")
        complete = False
        degraded = False

        try:
            plan = STRATEGY_PLANS.get(strategy)
            if plan is None:
                raise ValueError(f"unknown strategy {strategy!r}; use one of {sorted(STRATEGY_PLANS)}")

            last_error: Exception | None = None
            for step in plan:
                writer.source = SOURCE_LABEL[step]
                try:
                    if step == "full":
                        await self._load_full(writer)
                        complete = True
                    elif step == "partitioned":
                        degraded |= await self._load_partitioned(writer, report)
                    elif step == "snapshot":
                        await self._load_snapshot(writer)
                    elif step == "fallback":
                        if await asyncio.to_thread(self._count_active) > 0:
                            report.source = "cache"
                            raise _KeepExisting(str(last_error))
                        writer.source = SOURCE_LABEL["snapshot"]
                        report.warnings.append("database empty and upstream down: loaded bundled snapshot")
                        await self._load_snapshot(writer)
                    report.source = writer.source
                    break
                except _KeepExisting:
                    raise
                except (UpstreamError, OSError, json.JSONDecodeError) as exc:
                    last_error = exc
                    degraded = True
                    report.warnings.append(f"{step} failed: {exc}")
                    log.warning("ingestion step failed", extra={"step": step, "error": str(exc)})
            else:
                raise last_error or RuntimeError("no ingestion step succeeded")

            if complete:
                report.deactivated = await asyncio.to_thread(self._deactivate_missing, run_started, report)
            report.duplicates_flagged = await asyncio.to_thread(self._reconcile_duplicates)
            report.status = "partial" if degraded else "success"
        except _KeepExisting as exc:
            report.status = "failed"
            report.error = f"upstream unavailable, previously stored data retained ({exc})"
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded, never crash the app
            report.status = "failed"
            report.error = f"{type(exc).__name__}: {exc}"
            log.exception("ingestion failed")
        finally:
            report.quality = dict(writer.quality)
            report.duration_ms = int((time.perf_counter() - started_clock) * 1000)
            changed = report.inserted + report.updated + report.deactivated > 0
            dup_changed = await asyncio.to_thread(self._close_run, report, changed)
            if changed or dup_changed:
                self.on_data_changed()
            self.last_report = report
            log.info("ingestion finished", extra={"report": report.as_dict()})
        return report

    # -- sources ---------------------------------------------------------------

    async def _load_full(self, writer: _BatchWriter) -> None:
        current_attempt, delivered = 1, 0
        async for attempt, batch in self.client.iter_batches("/tick-sightings", self.settings.batch_size):
            if attempt != current_attempt:
                if delivered:
                    writer.report.warnings.append(
                        f"stream dropped after {delivered} rows and restarted (attempt {attempt}); "
                        "re-delivered rows were skipped"
                    )
                current_attempt, delivered = attempt, 0
            delivered += len(batch)
            await asyncio.to_thread(writer.write, batch)

    async def _load_partitioned(self, writer: _BatchWriter, report: RunReport) -> bool:
        """Returns True if some (but not all) partitions failed."""
        cities = sorted(set(await asyncio.to_thread(self._known_cities)))
        sem = asyncio.Semaphore(self.settings.partition_concurrency)
        failures: list[str] = []

        async def one(city: str) -> None:
            async with sem:
                try:
                    items = await self.client.fetch_city(city)
                except UpstreamError as exc:
                    failures.append(f"{city}: {exc}")
                    return
            for chunk in _chunks(items, self.settings.batch_size):
                await asyncio.to_thread(writer.write, chunk)

        await asyncio.gather(*(one(c) for c in cities))
        if failures and len(failures) == len(cities):
            raise UpstreamError(f"all {len(cities)} city partitions failed; first: {failures[0]}")
        if failures:
            report.warnings.append(f"{len(failures)}/{len(cities)} city partitions failed: {failures[:3]}")
        report.warnings.append("partitioned sync cannot discover new cities; deactivation skipped")
        return bool(failures)

    async def _load_snapshot(self, writer: _BatchWriter) -> None:
        path = self.settings.snapshot_path
        data = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(data, list):
            raise OSError(f"snapshot {path} is not a JSON array")
        for chunk in _chunks(data, self.settings.batch_size):
            await asyncio.to_thread(writer.write, chunk)

    # -- DB helpers (sync, run in threads) ----------------------------------------

    def _open_run(self, trigger: str, strategy: str, started: datetime) -> int:
        with session_scope() as s:
            run = IngestionRun(trigger=trigger, strategy=strategy, status="running", started_at=started)
            s.add(run)
            s.flush()
            return run.id

    def _close_run(self, report: RunReport, changed: bool) -> bool:
        with session_scope() as s:
            run = s.get(IngestionRun, report.run_id)
            if run is not None:
                for attr in (
                    "source",
                    "status",
                    "fetched",
                    "inserted",
                    "updated",
                    "unchanged",
                    "rejected",
                    "duplicates_flagged",
                    "deactivated",
                    "duration_ms",
                    "error",
                ):
                    setattr(run, attr, getattr(report, attr))
                run.finished_at = utcnow()
                run.warnings = json.dumps(report.warnings)
                run.quality = json.dumps(report.quality)
            dup_changed = self._dup_changed
            self._dup_changed = False
            if changed or dup_changed:
                bump_data_version(s)
            if report.status in ("success", "partial") and report.source not in (None, "cache"):
                _set_meta(s, "last_successful_sync_at", utcnow().isoformat())
                _set_meta(s, "last_sync_source", report.source or "")
            return dup_changed

    def _count_active(self) -> int:
        with session_scope() as s:
            return s.scalar(select(func.count()).select_from(Sighting).where(Sighting.is_active)) or 0

    def _known_cities(self) -> list[str]:
        with session_scope() as s:
            from_db = [c for (c,) in s.execute(select(Sighting.city).distinct())]
        return from_db + list(DATASET_CITIES)

    def _deactivate_missing(self, run_started: datetime, report: RunReport) -> int:
        with session_scope() as s:
            active = s.scalar(select(func.count()).select_from(Sighting).where(Sighting.is_active)) or 0
            stale_q = (
                select(func.count())
                .select_from(Sighting)
                .where(Sighting.is_active, Sighting.last_seen_at < run_started)
            )
            stale = s.scalar(stale_q) or 0
            if stale == 0:
                return 0
            if active and stale / active > MAX_DEACTIVATION_SHARE:
                report.warnings.append(
                    f"refused to deactivate {stale}/{active} records (> {MAX_DEACTIVATION_SHARE:.0%}); "
                    "upstream response looks incomplete"
                )
                return 0
            s.execute(
                update(Sighting)
                .where(Sighting.is_active, Sighting.last_seen_at < run_started)
                .values(is_active=False, updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            return stale

    def _reconcile_duplicates(self) -> int:
        """Re-evaluate probable duplicates in SQL. Returns the number of rows flagged.

        Only candidate rows (those within the window of the previous same-city,
        same-species report) ever leave the database, so this scales with the
        number of duplicates, not the size of the table.
        """
        with session_scope() as s:
            dialect = s.get_bind().dialect.name
            gap = (
                "EXTRACT(EPOCH FROM (observed_at - prev_at))"
                if dialect == "postgresql"
                else "(julianday(observed_at) - julianday(prev_at)) * 86400.0"
            )
            sql = text(f"""
                SELECT id, prev_id FROM (
                    SELECT id, observed_at,
                           LAG(id) OVER w AS prev_id,
                           LAG(observed_at) OVER w AS prev_at
                    FROM sightings
                    WHERE is_active
                    WINDOW w AS (PARTITION BY city, species ORDER BY observed_at, id)
                ) AS t
                WHERE prev_at IS NOT NULL AND {gap} <= :window
            """)  # noqa: S608 - only the dialect-specific expression is interpolated, values are bound
            prev_of = dict(s.execute(sql, {"window": self.settings.duplicate_window_s}).all())
            wanted = resolve_duplicate_chains(prev_of)
            current = dict(
                s.execute(
                    select(Sighting.id, Sighting.duplicate_of).where(Sighting.duplicate_of.is_not(None))
                ).all()
            )
            changes = [{"id": i, "duplicate_of": d} for i, d in wanted.items() if current.get(i) != d]
            changes += [{"id": i, "duplicate_of": None} for i in current if i not in wanted]
            for chunk in _chunks(changes, 500):
                s.execute(update(Sighting), chunk)
            self._dup_changed = bool(changes)
            return len(wanted)


class _KeepExisting(Exception):
    pass


# -- meta helpers -------------------------------------------------------------


def _set_meta(s: Session, key: str, value: str) -> None:
    row = s.get(AppMeta, key)
    if row is None:
        s.add(AppMeta(key=key, value=value))
    else:
        row.value = value


def get_meta(s: Session, key: str, default: str | None = None) -> str | None:
    row = s.get(AppMeta, key)
    return row.value if row else default


def bump_data_version(s: Session) -> int:
    current = int(get_meta(s, "data_version", "0") or 0) + 1
    _set_meta(s, "data_version", str(current))
    return current


def get_data_version(s: Session) -> int:
    return int(get_meta(s, "data_version", "0") or 0)
