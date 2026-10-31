"""The statistical engine must (a) recover patterns that are really there and
(b) not invent patterns that are not. Both are tested on simulated data with a
known ground truth, plus the real (pattern-free) snapshot."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import numpy as np
from fastapi.testclient import TestClient

from app.insights.glm import bh_adjust, fit_poisson, poisson_trend
from app.main import create_app
from tests.conftest import make_settings, write_snapshot

CITIES = ["Leeds", "London", "Bristol", "Glasgow"]
SEASON = [0.3, 0.4, 0.8, 1.6, 2.0, 1.8, 1.3, 1.1, 1.2, 0.9, 0.5, 0.3]


def simulate(seed=1, years=8, base=6.0, season=True, hotspot=None, spike=None, growth=0.0):
    """Generate synthetic sightings with a known structure."""
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)
    records, n = [], 0
    for y in range(years):
        for m in range(12):
            for city in CITIES:
                rate = base * (SEASON[m] if season else 1.0) * math.exp(growth * y)
                if city == hotspot:
                    rate *= 3
                if spike and (city, 2015 + y, m + 1) == spike:
                    rate *= 8
                for _ in range(rng.poisson(rate)):
                    day = pyrng.randint(1, 28)
                    records.append(
                        {
                            "id": f"sim{n}",
                            "location": city,
                            "species": "Marsh tick",
                            "latinName": "Ixodes apronophorus",
                            "date": (
                                datetime(2015 + y, m + 1, day) + timedelta(minutes=n % 1400)
                            ).isoformat(),
                        }
                    )
                    n += 1
    return records


def client_for(tmp_path, records):
    snap = write_snapshot(tmp_path, records)
    app = create_app(make_settings(tmp_path, snapshot_path=snap), start_scheduler=False)
    return TestClient(app)


# --- GLM core ------------------------------------------------------------------


def test_glm_recovers_known_coefficients():
    rng = np.random.default_rng(0)
    x = rng.normal(size=4000)
    X = np.column_stack([np.ones_like(x), x])
    y = rng.poisson(np.exp(0.5 + 0.3 * x))
    res = fit_poisson(X, y)
    assert res.converged
    assert abs(res.beta[0] - 0.5) < 0.05 and abs(res.beta[1] - 0.3) < 0.05
    assert 0.9 < res.phi < 1.1


def test_bh_adjustment_matches_reference():
    # sorted p: .01 .03 .04 .2 -> p*m/rank: .04 .06 .0533 .2 -> running min from the top
    assert np.allclose(bh_adjust([0.01, 0.04, 0.03, 0.2]), [0.04, 0.16 / 3, 0.16 / 3, 0.2])


def test_poisson_trend_detects_growth_and_flatness():
    t = [i / 12 for i in range(120)]
    rng = np.random.default_rng(3)
    growing = rng.poisson([5 * math.exp(0.15 * ti) for ti in t])
    flat = rng.poisson([5] * 120)
    g = poisson_trend(t, growing.tolist())
    assert g["significant"] and g["direction"] == "increasing" and 10 < g["change_per_year_pct"] < 22
    assert poisson_trend(t, flat.tolist())["direction"] == "flat"


# --- end-to-end on simulated data ------------------------------------------------


def test_detects_seasonality_trend_and_hotspot_when_present(tmp_path):
    records = simulate(season=True, hotspot="Glasgow", growth=0.1)
    with client_for(tmp_path, records) as c:
        o = c.get("/api/v1/insights/overview").json()
    assert o["tests"]["seasonality"]["significant"]
    assert o["trend"]["significant"] and o["trend"]["direction"] == "increasing"
    peak = max(o["seasonality"]["profile"], key=lambda p: p["index"])
    assert peak["name"] in ("Apr", "May", "Jun")
    hot = [cty["city"] for cty in o["city_effects"] if cty["classification"] == "hotspot"]
    assert hot == ["Glasgow"]


def test_does_not_invent_patterns_in_the_real_dataset(client):
    o = client.get("/api/v1/insights/overview").json()
    assert not o["tests"]["seasonality"]["significant"]
    assert not o["tests"]["trend"]["significant"]
    assert o["model"]["selected_terms"] == ["city"]


def test_anomaly_detection_finds_injected_spike(tmp_path):
    records = simulate(season=False, spike=("London", 2019, 6), base=3.0)
    with client_for(tmp_path, records) as c:
        a = c.get("/api/v1/insights/anomalies", params={"granularity": "month"}).json()
    flagged = {(x["city"], x["period"]) for x in a["anomalies"]}
    assert ("London", "2019-06") in flagged
    assert len(flagged) <= 3  # FDR control: essentially no false alarms


def test_no_anomalies_in_pattern_free_data(client):
    a = client.get("/api/v1/insights/anomalies").json()
    assert a["anomaly_count"] == 0 and len(a["most_unusual"]) == 10


def test_forecast_shape_intervals_and_backtest(client):
    f = client.get("/api/v1/insights/forecast", params={"horizon": 6}).json()
    assert len(f["points"]) == 6 and f["forecast_from"] == "2025-01"
    for p in f["points"]:
        assert p["pi95"][0] <= p["pi80"][0] <= p["median"] <= p["pi80"][1] <= p["pi95"][1]
    bt = f["backtest"]
    assert bt["status"] == "ok" and bt["mae_model"] <= bt["mae_seasonal_naive"]


def test_seasonal_forecast_peaks_in_summer(tmp_path):
    with client_for(tmp_path, simulate(season=True)) as c:
        f = c.get("/api/v1/insights/forecast", params={"horizon": 12}).json()
    by_month = {p["label"][-2:]: p["expected"] for p in f["points"]}
    assert by_month["05"] > 3 * by_month["01"]
    assert f["backtest"]["skill_vs_best_baseline_pct"] > 0  # beats naive baselines when there is signal


def test_city_forecast_uses_pooled_model(client):
    f = client.get("/api/v1/insights/forecast", params={"for_city": "nottingam", "horizon": 3}).json()
    assert f["scope"] == "Nottingham" and f["model"]["pooled_across_cities"] is True


def test_risk_scores_are_explainable_and_ranked(client):
    r = client.get("/api/v1/insights/risk", params={"month": "2025-05"}).json()
    scores = [c["score"] for c in r["cities"]]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 100 for s in scores)
    top = r["cities"][0]
    assert top["city"] == "Nottingham"  # the statistically detected hotspot
    assert {"relative_activity", "seasonal_factor", "trend_factor"} <= set(top["components"])
    assert "Nottingham" in top["explanation"]
    assert top["components"]["seasonal_source"] == "prior"  # dataset has no seasonality
    winter = client.get("/api/v1/insights/risk", params={"month": "2025-01"}).json()
    assert winter["cities"][0]["score"] < top["score"]  # prior: May riskier than January


def test_risk_near_point(client):
    r = client.get(
        "/api/v1/insights/risk/near", params={"at_lat": 53.48, "at_lon": -2.24, "within_km": 51}
    ).json()
    assert [n["city"] for n in r["nearby"]] == ["Manchester", "Liverpool"]
    assert r["local_score"] is not None


def test_briefing_is_grounded_in_numbers(client):
    b = client.get("/api/v1/insights/briefing").json()
    assert "1,000" in b["headline"]
    assert b["key_numbers"]["hotspots"] == ["Nottingham"]
    assert len(b["paragraphs"]) >= 4
