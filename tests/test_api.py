from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Sighting
from app.services.query import bucket_expr, bucket_start


def test_health_and_readiness(client):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["data"]["active_sightings"] == 1000
    assert body["status"] == "degraded"  # only the bundled snapshot so far, live API not reached
    assert "snapshot" in " ".join(body["degraded_reasons"])


def test_list_defaults_and_pagination(client):
    r = client.get("/api/v1/sightings", params={"page_size": 25, "page": 2})
    body = r.json()
    assert r.status_code == 200
    assert body["meta"]["total"] == 1000 and body["meta"]["total_pages"] == 40
    assert len(body["data"]) == 25
    dates = [d["observed_at"] for d in body["data"]]
    assert dates == sorted(dates, reverse=True)
    assert "page=3" in body["links"]["next"] and "page=1" in body["links"]["prev"]


def test_page_size_is_capped(client):
    assert client.get("/api/v1/sightings", params={"page_size": 5000}).json()["meta"]["page_size"] == 500


def test_filter_by_time_range_is_inclusive_of_end_day(client, snapshot_records):
    r = client.get("/api/v1/sightings", params={"start": "2020-01-01", "end": "2020-12-31", "page_size": 500})
    expected = [x for x in snapshot_records if "2020-01-01" <= x["date"][:10] <= "2020-12-31"]
    assert r.json()["meta"]["total"] == len(expected)
    assert all(d["observed_at"].startswith("2020") for d in r.json()["data"])


def test_year_and_month_shorthands(client, snapshot_records):
    r = client.get("/api/v1/sightings", params={"start": "2021-03", "end": "2021-05"})
    expected = [x for x in snapshot_records if "2021-03" <= x["date"][:7] <= "2021-05"]
    assert r.json()["meta"]["total"] == len(expected)


def test_filter_by_city_is_case_and_typo_tolerant(client, snapshot_records):
    n = sum(1 for x in snapshot_records if x["location"] == "Manchester")
    for raw in ("Manchester", "manchester", "  MANCHESTER", "Manchster"):
        assert client.get("/api/v1/sightings", params={"city": raw}).json()["meta"]["total"] == n


def test_multiple_cities_comma_or_repeated(client, snapshot_records):
    n = sum(1 for x in snapshot_records if x["location"] in ("Leeds", "York", "London"))
    assert client.get("/api/v1/sightings?city=Leeds,London").json()["meta"]["total"] == n
    assert client.get("/api/v1/sightings?city=Leeds&city=London").json()["meta"]["total"] == n


def test_filter_by_region_nation_species(client, snapshot_records):
    scot = sum(1 for x in snapshot_records if x["location"] in ("Glasgow", "Edinburgh"))
    assert client.get("/api/v1/sightings?region=scotland").json()["meta"]["total"] == scot
    assert client.get("/api/v1/sightings?nation=Scotland").json()["meta"]["total"] == scot
    marsh = sum(1 for x in snapshot_records if x["species"] == "Marsh tick")
    assert client.get("/api/v1/sightings?species=marsh tick").json()["meta"]["total"] == marsh
    assert client.get("/api/v1/sightings?species=Ixodes apronophorus").json()["meta"]["total"] == marsh


def test_radius_search(client, snapshot_records):
    # Liverpool is ~50 km from Manchester city centre, Sheffield ~53 km
    r40 = client.get("/api/v1/sightings", params={"lat": 53.48, "lon": -2.24, "radius_km": 40}).json()
    r51 = client.get("/api/v1/sightings", params={"lat": 53.48, "lon": -2.24, "radius_km": 51}).json()
    assert r40["meta"]["filters"]["near"]["matched_cities"] == ["Manchester"]
    assert r51["meta"]["filters"]["near"]["matched_cities"] == ["Manchester", "Liverpool"]
    assert r51["meta"]["total"] == sum(
        1 for x in snapshot_records if x["location"] in ("Manchester", "Liverpool")
    )


def test_free_text_search(client):
    body = client.get("/api/v1/sightings", params={"q": "canisuga", "page_size": 500}).json()
    assert body["meta"]["total"] > 0 and {d["species"] for d in body["data"]} == {"Fox/badger tick"}


def test_sorting(client):
    body = client.get("/api/v1/sightings", params={"sort": "city", "order": "asc", "page_size": 100}).json()
    cities = [d["city"] for d in body["data"]]
    assert cities == sorted(cities) and cities[0] == "Birmingham"


@pytest.mark.parametrize(
    "params, code",
    [
        ({"city": "Atlantis"}, "unknown_location"),
        ({"region": "Narnia"}, "unknown_region"),
        ({"species": "spider"}, "unknown_species"),
        ({"start": "not-a-date"}, "invalid_query"),
        ({"start": "2022-01-01", "end": "2021-01-01"}, "invalid_query"),
        ({"lat": 51.5}, "incomplete_coordinates"),
        ({"lat": 200, "lon": 0}, "validation_error"),
        ({"sort": "bogus"}, "validation_error"),
    ],
)
def test_bad_input_returns_consistent_error_envelope(client, params, code):
    r = client.get("/api/v1/sightings", params=params)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == code and err["message"] and err["request_id"]


def test_unknown_city_error_suggests_close_matches(client):
    err = client.get("/api/v1/sightings", params={"city": "Sheffeld Town"}).json()["error"]
    assert "Sheffield" in err["details"]["suggestions"]


def test_get_one_and_404(client, snapshot_records):
    rid = snapshot_records[0]["id"]
    one = client.get(f"/api/v1/sightings/{rid}").json()
    assert one["id"] == rid and one["raw_location"] == snapshot_records[0]["location"]
    missing = client.get("/api/v1/sightings/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_request_id_is_echoed(client):
    r = client.get("/api/v1/sightings/nope", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"
    assert r.json()["error"]["request_id"] == "trace-123"


def test_csv_export_streams_all_matching_rows(client, snapshot_records):
    r = client.get("/api/v1/sightings/export", params={"city": "Leeds"})
    lines = r.text.strip().splitlines()
    assert r.headers["content-type"].startswith("text/csv")
    assert lines[0].startswith("id,observed_at,city")
    assert len(lines) - 1 == sum(1 for x in snapshot_records if x["location"] == "Leeds")


# --- reporting ---------------------------------------------------------------


def test_by_region_counts_sum_to_total(client):
    body = client.get("/api/v1/stats/by-location", params={"group_by": "region"}).json()
    assert sum(g["count"] for g in body["data"]) == body["total"] == 1000
    assert body["difference_test"]["test"] == "chi_square_vs_city_count"


def test_by_city_includes_coordinates_and_geojson(client):
    body = client.get("/api/v1/stats/by-location", params={"group_by": "city"}).json()
    assert all(d["lat"] is not None for d in body["data"])
    geo = client.get("/api/v1/stats/by-location.geojson").json()
    assert geo["type"] == "FeatureCollection" and len(geo["features"]) == 14


@pytest.mark.parametrize("interval", ["week", "month", "quarter", "year"])
def test_trends_are_zero_filled_and_sum_to_total(client, interval):
    body = client.get("/api/v1/stats/trends", params={"interval": interval}).json()
    points = body["series"][0]["points"]
    assert sum(p["count"] for p in points) == 1000
    periods = [date.fromisoformat(p["period"]) for p in points]
    assert periods == sorted(periods) and len(set(periods)) == len(periods)
    if interval == "week":
        assert all(d.weekday() == 0 for d in periods)  # ISO weeks start on Monday
        assert body["summary"]["zero_periods"] > 0  # gaps are present as explicit zeros


def test_trends_split_by_species_has_top_n_plus_other(client):
    body = client.get(
        "/api/v1/stats/trends", params={"interval": "year", "split_by": "species", "top": 2}
    ).json()
    keys = [s["key"] for s in body["series"]]
    assert len(keys) == 3 and keys[-1] == "Other"
    assert sum(s["total"] for s in body["series"]) == 1000


def test_trends_report_poisson_trend_test(client):
    trend = client.get("/api/v1/stats/trends", params={"interval": "month"}).json()["summary"]["trend"]
    assert trend["method"] == "poisson_glm" and trend["direction"] == "flat" and trend["p_value"] > 0.05


def test_sql_time_buckets_match_python(client):
    """The SQL bucketing (dialect-specific) must agree with the Python zero-filling logic."""
    with session_scope() as s:
        for interval in ("week", "month", "quarter", "year"):
            expr = bucket_expr(interval, s.get_bind().dialect.name)
            for observed, bucket in s.execute(select(Sighting.observed_at, expr).limit(300)):
                assert bucket == bucket_start(observed.date(), interval).isoformat()


def test_seasonality_and_heatmap(client):
    season = client.get("/api/v1/stats/seasonality").json()
    assert sum(m["count"] for m in season["by_month"]) == 1000
    assert season["seasonality_test"]["significant"] is False
    heat = client.get("/api/v1/stats/heatmap", params={"rows": "city", "cols": "species"}).json()
    assert len(heat["matrix"]) == 14 and len(heat["matrix"][0]) == 5
    assert sum(heat["row_totals"]) == 1000


def test_summary(client):
    body = client.get("/api/v1/stats/summary").json()
    assert body["total_sightings"] == 1000 and body["cities"] == 14 and body["species"] == 5
    assert body["top_city"]["key"] == "Nottingham"


def test_filters_apply_to_aggregates(client, snapshot_records):
    body = client.get("/api/v1/stats/summary", params={"city": "Leeds", "start": "2018"}).json()
    expected = sum(1 for x in snapshot_records if x["location"] == "Leeds" and x["date"] >= "2018")
    assert body["total_sightings"] == expected


def test_etag_conditional_get(client):
    first = client.get("/api/v1/stats/summary")
    etag = first.headers["ETag"]
    again = client.get("/api/v1/stats/summary", headers={"If-None-Match": etag})
    assert again.status_code == 304
    other = client.get("/api/v1/stats/summary", params={"city": "Leeds"})
    assert other.headers["ETag"] != etag


# --- data management ---------------------------------------------------------


def test_data_quality_report(client):
    body = client.get("/api/v1/data-quality").json()
    assert body["records"]["active"] == 1000
    assert body["quality_score"] == 100.0


def test_manual_sync_requires_api_key_when_configured(client, app):
    app.state.settings.admin_api_key = "s3cret"
    r = client.post("/api/v1/ingestion/sync", params={"strategy": "snapshot"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    ok = client.post(
        "/api/v1/ingestion/sync", params={"strategy": "snapshot"}, headers={"X-API-Key": "s3cret"}
    )
    assert ok.status_code == 200 and ok.json()["unchanged"] == 1000
    runs = client.get("/api/v1/ingestion/runs").json()["data"]
    assert runs[0]["trigger"] == "manual" and runs[1]["trigger"] == "bootstrap"


def test_reference_endpoints(client):
    locs = client.get("/api/v1/meta/locations").json()["data"]
    assert len(locs) == 14 and sum(x["sightings"] for x in locs) == 1000
    species = client.get("/api/v1/meta/species").json()["data"]
    assert {s["common_name"] for s in species if s["in_dataset"]} == {
        "Fox/badger tick",
        "Marsh tick",
        "Passerine tick",
        "Southern rodent tick",
        "Tree-hole tick",
    }


def test_openapi_lists_all_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/api/v1/sightings", "/api/v1/stats/trends", "/api/v1/insights/risk", "/health/ready"):
        assert p in paths


def test_unhandled_errors_use_the_envelope(client, app, monkeypatch):
    from app.services import analytics

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(analytics, "summary", boom)
    app.state.cache.clear()
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/stats/summary", params={"city": "Cardiff"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "kaboom" not in r.text  # no internals leaked


def test_dates_are_naive_iso(client):
    d = client.get("/api/v1/sightings", params={"page_size": 1}).json()["data"][0]["observed_at"]
    assert datetime.fromisoformat(d).tzinfo is None
