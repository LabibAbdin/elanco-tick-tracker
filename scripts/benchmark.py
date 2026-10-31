"""Scale check: stream-ingest a large synthetic feed, then time the heaviest queries.

    python -m scripts.benchmark --rows 200000

A throwaway HTTP server serves an N-record JSON array (same schema as the Elanco
API); the real ingestion pipeline streams it in batches into a fresh SQLite DB.
Peak memory is reported so you can see that it stays flat as N grows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import Settings
from app.reference import DATASET_CITIES, species_catalogue


def generate(path: Path, rows: int, seed: int = 1) -> None:
    rng = random.Random(seed)
    species = [s for s in species_catalogue() if s.in_dataset]
    start = datetime(2012, 1, 1)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("[")
        for i in range(rows):
            sp = rng.choice(species)
            rec = {
                "id": f"bench{i:09d}",
                "date": (start + timedelta(minutes=rng.randrange(13 * 365 * 24 * 60))).isoformat(),
                "location": rng.choice(DATASET_CITIES),
                "species": sp.common_name,
                "latinName": sp.latin_name,
            }
            fh.write(("," if i else "") + json.dumps(rec))
        fh.write("]")


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass

    def guess_type(self, path):
        return "application/json"


def peak_rss_mb() -> float:
    try:
        import resource  # not available on Windows
    except ImportError:
        return float("nan")
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux reports KiB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200_000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        feed_dir = tmpdir / "data"
        feed_dir.mkdir()
        t0 = time.perf_counter()
        generate(feed_dir / "tick-sightings", args.rows)
        size_mb = (feed_dir / "tick-sightings").stat().st_size / 1e6
        print(f"generated {args.rows:,} records ({size_mb:.1f} MB) in {time.perf_counter() - t0:.1f}s")

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(tmpdir)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        settings = Settings(
            database_url=f"sqlite:///{tmpdir / 'bench.db'}",
            upstream_base_url=f"http://127.0.0.1:{port}/data",
            batch_size=2000,
            log_json=False,
        )
        from app.db import create_all, init_engine, session_scope
        from app.ingestion.client import UpstreamClient
        from app.ingestion.pipeline import IngestionService
        from app.insights import engine
        from app.services import analytics
        from app.services.query import build_filters

        init_engine(settings.database_url)
        create_all()
        rss_before = peak_rss_mb()
        service = IngestionService(settings, UpstreamClient(settings))
        t0 = time.perf_counter()
        report = asyncio.run(service.sync("benchmark", "full"))
        elapsed = time.perf_counter() - t0
        print(
            f"ingested: status={report.status} inserted={report.inserted:,} in {elapsed:.1f}s "
            f"({report.inserted / elapsed:,.0f} rows/s); peak RSS {peak_rss_mb():.0f} MB "
            f"(was {rss_before:.0f} MB before ingestion; payload {size_mb:.0f} MB)"
        )

        t0 = time.perf_counter()
        asyncio.run(service.sync("benchmark", "full"))
        print(f"idempotent re-sync (all unchanged): {time.perf_counter() - t0:.1f}s")

        with session_scope() as s:
            f = build_filters(s)
            for name, fn in [
                ("summary", lambda: analytics.summary(s, f)),
                ("by region", lambda: analytics.by_group(s, f, "region")),
                ("weekly trend", lambda: analytics.trends(s, f, "week")),
                ("seasonality", lambda: analytics.seasonality(s, f)),
                (
                    "filtered page (city+range)",
                    lambda: analytics.summary(s, build_filters(s, city=["Leeds"], start="2020", end="2021")),
                ),
                ("GLM overview", lambda: engine.overview(s, f)),
                ("risk scores", lambda: engine.risk(s, f)),
            ]:
                t0 = time.perf_counter()
                fn()
                print(f"  {name:<28} {1000 * (time.perf_counter() - t0):8.0f} ms")
        server.shutdown()


if __name__ == "__main__":
    main()
