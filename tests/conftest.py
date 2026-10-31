from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.ingestion.client import UpstreamClient
from app.main import create_app

UPSTREAM = "https://upstream.test/data"
# Run the whole suite against PostgreSQL with e.g.
#   TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/ticks_test pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
SNAPSHOT = PROJECT_ROOT / "data" / "snapshot" / "tick_sightings_snapshot.json"


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _fresh_shared_database() -> Iterator[None]:
    """SQLite tests get a new file per test; a shared Postgres database is wiped instead."""
    if TEST_DATABASE_URL:
        from sqlalchemy import create_engine

        from app import models  # noqa: F401
        from app.db import Base

        engine = create_engine(TEST_DATABASE_URL)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        engine.dispose()
    yield


@pytest.fixture
def snapshot_records() -> list[dict]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_url=TEST_DATABASE_URL or f"sqlite:///{tmp_path / 'test.db'}",
        upstream_base_url=UPSTREAM,
        sync_on_startup=False,
        sync_interval_minutes=0,
        http_max_attempts=3,
        http_backoff_base_s=0.0,
        circuit_failure_threshold=5,
        log_json=False,
        log_level="WARNING",
        snapshot_path=SNAPSHOT,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def upstream_client(settings: Settings) -> UpstreamClient:
    return UpstreamClient(settings, sleep=no_sleep)


@pytest.fixture
def app(settings: Settings, upstream_client: UpstreamClient):
    return create_app(settings, start_scheduler=False, client=upstream_client)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    # entering the context runs the lifespan, which seeds the DB from the snapshot
    with TestClient(app) as c:
        yield c


def write_snapshot(tmp_path: Path, records: list) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path
