"""Chaos upstream: a drop-in stand-in for the Elanco API that misbehaves on demand.

The real API is clean and reliable, which makes it impossible to *demonstrate*
the pipeline's resilience against it. This server mirrors the real endpoints
(``/data/tick-sightings``, ``/data/tick-sightings/city/{city}``, ...) using the
bundled snapshot, and can inject:

* failures      - HTTP 500/503 at a configurable rate, or a full outage
* rate limiting - HTTP 429 with a Retry-After header
* latency       - slow responses (to exercise timeouts)
* truncation    - responses cut off mid-JSON (to exercise stream restarts)
* dirty data    - duplicates, near-duplicates, missing/invalid/future dates,
                  messy or misspelt cities, unknown towns, missing ids, species
                  given only in Latin, contradictory species names, non-objects

Run it and point the API at it::

    python -m tools.chaos_upstream --port 9000 --preset chaos
    UPSTREAM_BASE_URL=http://localhost:9000/data uvicorn app.main:app

Switch behaviour live (great for a demo)::

    curl -X POST "localhost:9000/admin/preset/outage"
    curl -X POST "localhost:9000/admin/preset/healthy"
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "snapshot" / "tick_sightings_snapshot.json"

PRESETS: dict[str, dict[str, Any]] = {
    "healthy": {
        "failure_rate": 0.0,
        "rate_limit_rate": 0.0,
        "latency_ms": 0,
        "truncate_rate": 0.0,
        "dirty": False,
    },
    "dirty": {
        "failure_rate": 0.0,
        "rate_limit_rate": 0.0,
        "latency_ms": 0,
        "truncate_rate": 0.0,
        "dirty": True,
    },
    "flaky": {
        "failure_rate": 0.4,
        "rate_limit_rate": 0.1,
        "latency_ms": 300,
        "truncate_rate": 0.2,
        "dirty": False,
    },
    "chaos": {
        "failure_rate": 0.35,
        "rate_limit_rate": 0.1,
        "latency_ms": 200,
        "truncate_rate": 0.25,
        "dirty": True,
    },
    "outage": {
        "failure_rate": 1.0,
        "rate_limit_rate": 0.0,
        "latency_ms": 0,
        "truncate_rate": 0.0,
        "dirty": False,
    },
    "slow": {
        "failure_rate": 0.0,
        "rate_limit_rate": 0.0,
        "latency_ms": 30_000,
        "truncate_rate": 0.0,
        "dirty": False,
    },
}


def make_dirty(records: list[dict[str, Any]], seed: int = 7) -> list[Any]:
    """Return the dataset with ~10% realistic data-quality problems mixed in."""
    rng = random.Random(seed)
    data: list[Any] = copy.deepcopy(records)
    sample = rng.sample(records, 120)
    extra: list[Any] = []

    for r in sample[:15]:  # exact duplicates (same id, same content)
        extra.append(copy.deepcopy(r))
    for r in sample[15:25]:  # double-submitted reports: new id, same event seconds later
        d = copy.deepcopy(r)
        d["id"] = "dup" + r["id"][3:]
        d["date"] = r["date"][:-2] + f"{min(59, int(r['date'][-2:]) + 40) % 60:02d}"
        extra.append(d)
    for r in sample[25:35]:  # messy casing / whitespace
        r2 = next(x for x in data if x["id"] == r["id"])
        r2["location"] = f"  {r['location'].upper()} "
    typos = {
        "Manchester": "Manchster",
        "Nottingham": "Nottingam",
        "Edinburgh": "Edinburg",
        "Liverpool": "Liverpol",
        "Birmingham": "Birmingam",
        "Newcastle": "Newcastle upon Tyne",
        "London": "Greater London",
    }
    for r in sample[35:45]:
        r2 = next(x for x in data if x["id"] == r["id"])
        r2["location"] = typos.get(r["location"], r["location"].lower())
    for r in sample[45:55]:  # common name missing, only the Latin name
        r2 = next(x for x in data if x["id"] == r["id"])
        r2.pop("species", None)
    for r in sample[55:60]:  # Latin name missing
        r2 = next(x for x in data if x["id"] == r["id"])
        r2["latinName"] = ""
    for r in sample[60:65]:  # contradictory names
        r2 = next(x for x in data if x["id"] == r["id"])
        r2["species"] = "Marsh tick" if r["species"] != "Marsh tick" else "Tree-hole tick"
    for i, r in enumerate(sample[65:73]):  # unparseable / missing / future / ancient dates
        bad = [
            "not-a-date",
            "",
            None,
            "2099-01-01T00:00:00",
            "1850-06-01T00:00:00",
            "31/02/2021",
            "yesterday",
            "2021-13-45",
        ][i]
        d = copy.deepcopy(r)
        d["id"] = "bad" + r["id"][3:]
        d["date"] = bad
        extra.append(d)
    for r in sample[73:78]:  # missing location
        d = copy.deepcopy(r)
        d["id"] = "noloc" + r["id"][5:]
        d["location"] = None
        extra.append(d)
    for r in sample[78:83]:  # towns not in the gazetteer
        d = copy.deepcopy(r)
        d["id"] = "town" + r["id"][4:]
        d["location"] = rng.choice(["Little Snoring", "Wetwang", "Great Snoring", "Barton in Fabis"])
        extra.append(d)
    for r in sample[83:88]:  # no id at all
        d = copy.deepcopy(r)
        d.pop("id")
        d["date"] = r["date"][:-5] + "00:00"
        extra.append(d)
    for r in sample[88:93]:  # alternative field names and UK date format
        d = {
            "sighting_id": "alt" + r["id"][3:],
            "Date": f"{r['date'][8:10]}/{r['date'][5:7]}/{r['date'][:4]}",
            "City": r["location"],
            "common_name": r["species"],
            "latin_name": r["latinName"],
        }
        extra.append(d)
    for r in sample[93:96]:  # same id re-sent with different content (an upstream correction)
        d = copy.deepcopy(r)
        d["location"] = "Leeds" if r["location"] != "Leeds" else "York"
        extra.append(d)
    extra += ["garbage", 42, ["not", "a", "record"]]  # not objects at all
    data += extra
    rng.shuffle(data)
    return data


def create_chaos_app(preset: str = "healthy", seed: int = 7) -> FastAPI:
    app = FastAPI(title="Chaos upstream (Elanco API stand-in)")
    clean = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    dirty = make_dirty(clean, seed)
    state = {"config": dict(PRESETS[preset]), "preset": preset, "requests": 0, "injected": {}}
    rng = random.Random(seed)

    def bump(kind: str) -> None:
        state["injected"][kind] = state["injected"].get(kind, 0) + 1

    async def maybe_fail() -> Response | None:
        state["requests"] += 1
        cfg = state["config"]
        if cfg["latency_ms"]:
            await asyncio.sleep(cfg["latency_ms"] / 1000 * rng.uniform(0.5, 1.5))
        if rng.random() < cfg["failure_rate"]:
            bump("5xx")
            return JSONResponse({"error": "Injected failure"}, status_code=rng.choice([500, 502, 503]))
        if rng.random() < cfg["rate_limit_rate"]:
            bump("429")
            return JSONResponse({"error": "Too many requests"}, status_code=429, headers={"Retry-After": "1"})
        return None

    def payload(items: list[Any]) -> Response:
        body = json.dumps(items).encode()
        if rng.random() < state["config"]["truncate_rate"]:
            bump("truncated")
            cut = body[: rng.randint(len(body) // 4, len(body) - 10)]

            async def broken():
                yield cut

            return StreamingResponse(broken(), media_type="application/json")
        return Response(body, media_type="application/json")

    def dataset() -> list[Any]:
        return dirty if state["config"]["dirty"] else clean

    @app.get("/data/health")
    async def health():
        if state["config"]["failure_rate"] >= 1.0:
            return JSONResponse({"status": "down"}, status_code=503)
        return {"status": "healthy"}

    @app.get("/data/stats")
    async def stats():
        return {"api_version": "1.0.0-chaos", "total_sightings": len(dataset())}

    @app.get("/data/tick-sightings")
    async def all_sightings():
        return await maybe_fail() or payload(dataset())

    @app.get("/data/tick-sightings/city/{city}")
    async def by_city(city: str):
        failed = await maybe_fail()
        if failed:
            return failed
        return payload([r for r in dataset() if isinstance(r, dict) and r.get("location") == city])

    @app.get("/data/tick-sightings/{sighting_id}")
    async def one(sighting_id: str):
        for r in clean:
            if r["id"] == sighting_id:
                return r
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/admin")
    async def admin():
        return {
            "preset": state["preset"],
            "config": state["config"],
            "requests": state["requests"],
            "injected": state["injected"],
            "presets": list(PRESETS),
        }

    @app.post("/admin/preset/{name}")
    async def set_preset(name: str):
        if name not in PRESETS:
            return JSONResponse({"error": f"unknown preset; choose from {list(PRESETS)}"}, status_code=404)
        state["config"] = dict(PRESETS[name])
        state["preset"] = name
        return await admin()

    @app.post("/admin/config")
    async def set_config(request: Request):
        state["config"].update(await request.json())
        state["preset"] = "custom"
        return await admin()

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--preset", choices=list(PRESETS), default="chaos")
    args = parser.parse_args()
    uvicorn.run(create_chaos_app(args.preset), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
