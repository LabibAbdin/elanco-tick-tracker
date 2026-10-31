#!/usr/bin/env bash
# A guided tour of the API for the walkthrough video.
#   ./scripts/demo.sh                    # against http://127.0.0.1:8000
#   ./scripts/demo.sh http://127.0.0.1:8001
# Press Enter between steps. Uses jq for pretty output when installed.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
# jq if installed; otherwise Python's JSON pretty-printer (python3 on Mac/Linux, python on Windows Git Bash)
PY=$(command -v python3 || command -v python || true)
pretty() { if command -v jq >/dev/null; then jq "${1:-.}"; else "$PY" -m json.tool; fi; }
step() { printf "\n\033[1;32m== %s\033[0m\n\033[2m%s\033[0m\n" "$1" "$2"; read -r -p "" _ || true; }

step "1. Health & readiness" "GET /health/ready - data freshness, circuit breaker, cache"
curl -s "$BASE/health/ready" | pretty '{status, degraded_reasons, data, circuit: .upstream.circuit_breaker}'

step "2. Search: Marsh ticks in Scotland during 2020-2022, newest first" \
     "GET /api/v1/sightings?species=marsh tick&region=Scotland&start=2020&end=2022"
curl -s -G "$BASE/api/v1/sightings" --data-urlencode "species=marsh tick" -d region=Scotland -d start=2020 \
     -d end=2022 -d page_size=3 | pretty '{meta: .meta | {total, filters}, first: .data[0]}'

step "3. Location search: everything within 60 km of Manchester city centre" \
     "GET /api/v1/sightings?lat=53.48&lon=-2.24&radius_km=60"
curl -s "$BASE/api/v1/sightings?lat=53.48&lon=-2.24&radius_km=60&page_size=1" | pretty '.meta.filters'

step "4. Friendly errors: a typo'd city and a bad date" "422 with suggestions and a request id"
curl -s "$BASE/api/v1/sightings?city=Sheffeld%20Town" | pretty
curl -s "$BASE/api/v1/sightings?start=yesterday" | pretty

step "5. Sightings per region (with a significance test)" "GET /api/v1/stats/by-location?group_by=region"
curl -s "$BASE/api/v1/stats/by-location?group_by=region" | pretty '{difference_test, top3: .data[:3]}'

step "6. Weekly trend for 2024, zero-filled, with a Poisson trend test" "GET /api/v1/stats/trends?interval=week&start=2024"
curl -s "$BASE/api/v1/stats/trends?interval=week&start=2024" | pretty '{periods, summary, first_points: .series[0].points[:4]}'

step "7. ML overview: trend? seasonality? hotspots?" "GET /api/v1/insights/overview"
curl -s "$BASE/api/v1/insights/overview" | pretty '{model, findings}'

step "8. Forecast with prediction intervals + honest backtest" "GET /api/v1/insights/forecast?horizon=6"
curl -s "$BASE/api/v1/insights/forecast?horizon=6" | pretty '{scope, model: .model.selected_terms, points: .points[:3], backtest}'

step "9. Explainable activity score for May (peak season)" "GET /api/v1/insights/risk?month=2025-05"
curl -s "$BASE/api/v1/insights/risk?month=2025-05" | pretty '{warnings, top: [.cities[:3][] | {city, score, level, explanation}]}'

step "10. Data quality & ingestion audit" "GET /api/v1/data-quality"
curl -s "$BASE/api/v1/data-quality?samples=2" | pretty '{quality_score, records, repairs, rejections}'

step "11. Trigger a sync and watch the run report" "POST /api/v1/ingestion/sync"
curl -s -X POST "$BASE/api/v1/ingestion/sync" | pretty

step "12. Caching: conditional GET returns 304 when nothing changed" "ETag / If-None-Match"
ETAG=$(curl -s -D - -o /dev/null "$BASE/api/v1/stats/summary" | awk 'tolower($1)=="etag:"{print $2}' | tr -d '\r')
echo "ETag: $ETAG"
curl -s -o /dev/null -w "second request -> HTTP %{http_code}\n" -H "If-None-Match: $ETAG" "$BASE/api/v1/stats/summary"

printf "\n\033[1;32mDone.\033[0m  Explore the rest at %s/docs  and the console at %s/\n" "$BASE" "$BASE"
