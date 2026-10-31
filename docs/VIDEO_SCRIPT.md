# Walkthrough video plan (≤ 10 minutes)

A suggested structure with timings and exactly what to show. Record at 1080p, terminal font ≥ 16 pt,
browser zoom 110%. Have three terminals ready (API, chaos upstream, commands) and the console open.

**Before recording**

```bash
rm -f data/*.db*                      # start fresh so the bootstrap + first sync are visible
LOG_JSON=false uvicorn app.main:app --port 8000             # terminal 1 (readable logs)
python -m tools.chaos_upstream --port 9000 --preset dirty   # terminal 2 (used in part 5)
```

On Windows (PowerShell):

```powershell
Remove-Item data\*.db* -ErrorAction SilentlyContinue
$env:LOG_JSON = "false"; uvicorn app.main:app --port 8000          # window 1
python -m tools.chaos_upstream --port 9000 --preset dirty         # window 2
# run scripts/demo.sh from Git Bash:  bash scripts/demo.sh
```

---

### 0:00 – 0:40 · Intro
* "Hi, I'm <name>. This is the backend for a UK tick-sightings tracker, built on the Elanco API."
* One sentence on the goal: turn a raw feed into clean, searchable, *trustworthy* insights — and stay up
  when the feed misbehaves.
* Show the README headline table (requirements → what was delivered).

### 0:40 – 1:40 · What I found in the upstream API (shows thinking before coding)
* 1,000 records, one unpaginated array, 14 cities, 5 species, 2012–2024.
* Case-sensitive city/species endpoints; latency jumps to ~4 s under concurrency.
* Data is clean and pattern-free → "so I built a chaos upstream to prove the cleaning and resilience paths,
  and made the insights engine report the absence of patterns honestly."

### 1:40 – 2:40 · Architecture (README diagram)
* Ingestion → store → read path; the read path never touches the upstream.
* Shared filters; version-keyed cache + ETags; one statistical model.
* Terminal 1 logs: bootstrap from snapshot, then the live sync run.

### 2:40 – 4:00 · Search & filtering (`./scripts/demo.sh`, steps 1–4, or Swagger at `/docs`)
* Marsh ticks in Scotland 2020–2022 (species by common *or* Latin name, month/year shorthands).
* Radius search around Manchester → matched cities in `meta.filters`.
* Typo'd city → 422 with "did you mean Sheffield?" and a request id.

### 4:00 – 5:20 · Reporting (demo steps 5–6, then the console)
* Sightings per region + why the χ² test uses "number of cities" as the null.
* Weekly trend: zero-filled weeks; Poisson trend test instead of a misleading straight line.
* Console: map, region bars, trend with interval toggle, seasonality vs prior.

### 5:20 – 7:00 · Data quality & resilience (the differentiator)
* Point a second instance at the chaos upstream:
  ```bash
  UPSTREAM_BASE_URL=http://127.0.0.1:9000/data DATABASE_URL=sqlite:///data/chaos.db \
    uvicorn app.main:app --port 8001
  ```
  (PowerShell: `$env:UPSTREAM_BASE_URL="http://127.0.0.1:9000/data"; $env:DATABASE_URL="sqlite:///data/chaos.db"; uvicorn app.main:app --port 8001` in a third window.)
* Open `localhost:8001` → data-quality chips: fuzzy-matched cities, inferred species, 16 quarantined, 10 duplicates.
* `curl -X POST localhost:9000/admin/preset/outage` (PowerShell: `curl.exe -X POST ...`) then **Sync now** in the console:
  readiness turns *degraded · circuit open*, the run shows *failed*, every chart still works.
* `curl -X POST localhost:9000/admin/preset/chaos` → sync succeeds despite 5xx, 429s and truncated
  streams (show the retries in the logs and the run warnings).
* Mention soft-deletes + the 50% safety valve.

### 7:00 – 8:40 · ML insights (demo steps 7–9)
* Overview: LR tests say no trend, no seasonality; one hotspot after FDR correction.
* Forecast: flat with honest intervals; backtest shows 0% skill vs the mean — "and that's the correct answer".
* Then: "does it find patterns when they exist?" → show `tests/test_insights.py::test_detects_seasonality_trend_and_hotspot_when_present`.
* Risk score: explanation sentence; May vs January; the seasonal-prior warning.

### 8:40 – 9:30 · Quality
* `pytest -q` → 125 passed in ~12 s; CI workflow; `make bench` numbers (200k rows, flat memory).

### 9:30 – 10:00 · Wrap-up
* What I'd do next: Postgres/PostGIS, worker + queue, incremental sync, write path for crowdsourced reports,
  richer models with weather covariates.
* "Thanks for watching."
