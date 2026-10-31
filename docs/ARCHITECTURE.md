# Architecture & decisions

This document records *why* the system looks the way it does: the decisions, the alternatives considered,
how data flows, and how each failure mode is handled. The README covers *what* it does.

## 1. Goals and constraints

* **Correct before clever.** Numbers shown to the public about disease vectors must be traceable and
  statistically honest — including saying "there is no pattern" when there isn't one.
* **Never blank, never wrong.** The API should keep serving the last good data when the upstream fails, and
  must never serve cached aggregates that disagree with the stored data.
* **Runs anywhere with one command.** Reviewers should not need Docker, Postgres or Redis to try it.
* **Designed to grow.** 1,000 records today; the shape of the system should not change at 1,000,000.

## 2. Data flow

```
                ┌──────────────────────── ingestion (background) ────────────────────────┐
 Elanco API ──► UpstreamClient ──► batches ──► clean_batch() ──► _BatchWriter ──► DB
   (stream)     timeouts/retries               validate          upsert by id+hash
                breaker/Retry-After            normalise         quarantine rejects
                     │ fallback                enrich            audit counters
                     └─► /city/{city} × N (bounded)                 │
 snapshot.json ─────────────────────────────────────────────────────┘ (cold start / outage)
                                    after load: soft-deactivate missing (full sync only)
                                                SQL LAG() near-duplicate detection
                                                bump data_version  → cache keys change
                └────────────────────────────────────────────────────────────────────────┘

 client ──► FastAPI router ──► filters (validated, shared) ──► cache[ns, data_version, params]
                                                                   │ miss
                                                                   ▼
                                               SQL aggregates / Poisson GLM / narrative
```

## 3. Decision records

### ADR-1 · Store locally, serve from the store (not a pass-through proxy)
*Alternatives:* proxy each request to the Elanco API; cache upstream responses.
*Decision:* periodic ingestion into our own database; all reads hit the database.
*Why:* the upstream has no server-side filtering beyond exact city/species, no pagination, case-sensitive
matching and multi-second latency under load. Owning the data lets us clean it, enrich it (regions,
coordinates), index it, aggregate it in SQL, and stay up when the upstream is down.
*Cost:* data is as fresh as the last sync (default hourly; visible in `/health/ready`).

### ADR-2 · SQLite by default, Postgres-ready
*Decision:* SQLAlchemy 2.0 + SQLite in WAL mode; the only dialect-specific SQL (time bucketing, the duplicate
window's time difference, date-part extraction) is isolated and has a Postgres branch.
*Why:* zero setup for reviewers; WAL lets API reads proceed during ingestion writes. The complete test suite
runs against PostgreSQL 16 as well (CI job `postgres`), so the dialect branches are exercised, not just written.
*When to switch:* multiple API instances, >10M rows, or geo queries beyond city-level radius → Postgres/PostGIS.

### ADR-3 · Streaming + batching + idempotent upserts
*Decision:* stream the array with `ijson`, process fixed-size batches, upsert by upstream id with a content hash.
*Why:* memory stays flat as data grows (measured: +44 MB for 200k rows); a retried or re-delivered batch is a
no-op; an unchanged re-sync writes nothing but `last_seen_at` and does not bump the data version (so caches survive).
*Alternative rejected:* "truncate and reload" — simple, but a mid-load failure leaves the API empty or partial.

### ADR-4 · Soft deletes, only after a complete view
*Decision:* rows missing from a *complete* full sync are marked inactive (never hard-deleted); partial/per-city
syncs never deactivate anything; a safety valve refuses to deactivate >50% in one run.
*Why:* the most dangerous upstream bug is "returns 200 with an almost empty list". This design turns that into a
warning instead of data loss.

### ADR-5 · Keep, flag and quarantine — don't drop silently
*Decision:* reject only records that cannot answer *when* and *where*; repair the rest with explicit flags and
keep raw values; store rejects with reasons and payload in `rejected_records`.
*Why:* for crowdsourced public-health data, silently discarding reports biases counts; silently "fixing" them
hides problems. Flags make every decision auditable and filterable.

### ADR-6 · Probable duplicates are linked, not deleted
*Decision:* a report with the same city and species within 5 minutes of the previous one is linked via
`duplicate_of` to the first report in the burst and excluded from statistics by default. Detection runs in SQL
(`LAG() OVER (PARTITION BY city, species ORDER BY observed_at)`), with a pure-Python reference implementation
that a test proves equivalent.
*Why:* double-submitted forms are the classic crowdsourcing duplicate and they have *different ids*, so id-based
de-duplication can't catch them. Linking (rather than deleting) keeps the decision reversible and the window is
configurable (`DUPLICATE_WINDOW_S`).

### ADR-7 · One interpretable statistical model, with tests that it can say "no"
*Alternatives:* Prophet/ARIMA per city, gradient boosting, an LLM summary.
*Decision:* a Poisson GLM with trend, month and city terms, model selection by likelihood-ratio tests,
posterior-predictive intervals, Benjamini–Hochberg FDR for multiple comparisons, empirical-Bayes shrinkage for
risk, and template-based narrative.
*Why:* ~60% of city-months have zero sightings — counts this sparse need a count model, and per-city time-series
models would overfit noise. Every output is explainable ("1.34× the average city, 95% CI 1.10–1.64"). The
backtest and simulation tests guard against both over- and under-claiming. An LLM narrative was rejected because
a public-health number must never be hallucinated.

### ADR-8 · Version-keyed caching with ETags
*Decision:* cache aggregate/model responses under `(namespace, data_version, params)` and expose the same key as
an ETag.
*Why:* analytics are pure functions of (data, params); the data only changes during ingestion. Keying on the
version gives correct invalidation for free, and ETags let browsers/CDNs skip the payload entirely (304).

### ADR-9 · Health means "can serve useful data"
*Decision:* `/health/ready` returns 200 with `status: degraded` (and reasons) when serving stale data, and 503 only
with no data at all.
*Why:* a load balancer should not take down an instance that is serving yesterday's data perfectly well just
because the upstream is having a bad day.

## 4. Failure-mode catalogue

| # | Failure | Detection | Behaviour | Visible where |
|---|---|---|---|---|
| 1 | Upstream timeout | httpx timeout | retry w/ jitter → fallback → keep data | run warnings, `/api/v1/upstream` |
| 2 | 5xx / 429 | status code | retry (honour `Retry-After`) | same |
| 3 | Sustained outage | breaker threshold | fail fast for 60 s, half-open probe | `/health/ready` → degraded |
| 4 | Truncated / HTML body | JSON parse / content-type | retry whole stream, skip re-delivered rows | run warnings |
| 5 | 4xx / wrong shape | status / type check | no retry, step fails, next strategy | run error |
| 6 | Near-empty response | deactivation share > 50% | refuse to deactivate | run warnings |
| 7 | Dirty records | validation | quarantine with reasons | `/api/v1/data-quality` |
| 8 | Double submissions | SQL window function | link `duplicate_of`, exclude from stats | data-quality, record detail |
| 9 | Empty DB + upstream down | fallback step | load bundled snapshot | readiness reasons |
| 10 | Bad client input | validation | 422 envelope + suggestions | response body |
| 11 | Bug in our code | global handler | 500 envelope, no internals, logged with request id | logs |
| 12 | Concurrent sync triggers | asyncio lock | 409 Conflict | response |

## 5. Scaling path

| Load | Change |
|---|---|
| 10× data (10k) | nothing |
| 1,000× (1M) | Postgres; keep streaming ingestion; `make bench` shows the pipeline is O(n) with flat memory |
| Many API replicas | move the scheduler to a single worker (or use a DB advisory lock); shared cache (Redis) keyed the same way |
| Heavy read traffic | CDN in front of the GET endpoints — the ETags already make them cacheable |
| Real-time crowdsourcing | write endpoint → queue → the same `clean_batch` + upsert path; incremental model refits |

## 6. Things I would do better with more time

* Contract tests against the live upstream (nightly) to detect schema drift before it reaches users.
* Property-based tests (Hypothesis) for the cleaning rules.
* Keyset pagination for very deep pages; per-client rate limiting.
* Hierarchical Bayesian model (city random effects, spatial smoothing) once the data has real signal, and
  covariates such as temperature and humidity, which drive tick activity.
* Metrics and tracing (Prometheus/OpenTelemetry) instead of logs alone.
