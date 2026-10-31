"""ORM models.

``sightings``         cleaned, enriched, de-duplicated records (one row per upstream id)
``rejected_records``  quarantined input that failed validation, kept with reasons + raw payload
``ingestion_runs``    an audit log of every sync attempt (what happened, from where, how long)
``app_meta``          small key/value store (e.g. the data version used for cache invalidation)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sighting(Base):
    __tablename__ = "sightings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    city: Mapped[str] = mapped_column(String(120), index=True)
    region: Mapped[str] = mapped_column(String(80), index=True)
    nation: Mapped[str] = mapped_column(String(40), index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    species: Mapped[str] = mapped_column(String(120), index=True)
    latin_name: Mapped[str] = mapped_column(String(120), default="")

    # Lineage: what upstream actually sent, so cleaning decisions are auditable.
    raw_location: Mapped[str | None] = mapped_column(String(200))
    raw_species: Mapped[str | None] = mapped_column(String(200))
    quality_flags: Mapped[str] = mapped_column(Text, default="")  # comma-separated
    content_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(20))

    duplicate_of: Mapped[str | None] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_sightings_city_time", "city", "observed_at"),
        Index("ix_sightings_species_time", "species", "observed_at"),
        Index("ix_sightings_usable_time", "is_active", "duplicate_of", "observed_at"),
    )

    @property
    def flags(self) -> list[str]:
        return [f for f in self.quality_flags.split(",") if f]


class RejectedRecord(Base):
    __tablename__ = "rejected_records"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_id: Mapped[str | None] = mapped_column(String(64))
    reasons: Mapped[str] = mapped_column(Text)  # comma-separated reason codes
    raw_payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(20))  # startup | schedule | manual | test
    strategy: Mapped[str] = mapped_column(String(20))  # requested strategy
    source: Mapped[str | None] = mapped_column(String(20))  # where data actually came from
    status: Mapped[str] = mapped_column(String(20), index=True)  # running|success|partial|failed
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_flagged: Mapped[int] = mapped_column(Integer, default=0)
    deactivated: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    quality: Mapped[str] = mapped_column(Text, default="{}")  # JSON object of flag/reject counters
    error: Mapped[str | None] = mapped_column(Text)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
