"""Statistical insights ("AI/ML" extension).

One coherent model underpins everything here: a Poisson GLM for monthly sighting
counts per city,

    log E[count(city, month)] = intercept + trend*t + season[month] + effect[city]

with effect (sum-to-zero) coding, so ``exp(effect)`` reads directly as "x times the
average". Terms are kept only if a likelihood-ratio test says they earn their place
(parsimony protects forecasts from fitting noise). From that model we derive:

* **overview**   - is there a trend? a seasonal cycle? genuine hotspots?
* **forecast**   - next N months with simulation-based prediction intervals,
                   plus an honest out-of-sample backtest against naive baselines
* **anomalies**  - city-periods with far more (or fewer) sightings than expected,
                   with Benjamini-Hochberg control of false discoveries
* **risk**       - an explainable 0-100 activity score per city for a target month
* **narrative**  - the findings in plain English

Every output reports its uncertainty and, where the data cannot support a claim,
says so rather than inventing a pattern.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
from scipy import stats
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import InvalidQuery
from app.insights.glm import GLMResult, bh_adjust, fit_poisson, lr_test
from app.models import Sighting
from app.reference import PLACES, SEASONAL_PRIOR, UNMAPPED_REGION, haversine_km
from app.services.analytics import MONTH_NAMES
from app.services.query import SightingFilters, bucket_expr, bucket_range, next_bucket

RNG_SEED = 20241101
ALPHA = 0.05


# ---------------------------------------------------------------------------
# Data panel
# ---------------------------------------------------------------------------


# Locations with fewer sightings than this (or no known region) can't support a rate
# estimate and would distort the "average city" baseline, so models leave them out.
MIN_CITY_SIGHTINGS = 10


@dataclass
class Panel:
    cities: list[str]
    months: list[date]
    Y: np.ndarray  # shape (n_cities, n_months)
    excluded: list[dict] = field(default_factory=list)

    @property
    def national(self) -> np.ndarray:
        return self.Y.sum(axis=0)

    @property
    def last_month(self) -> date:
        return self.months[-1]


def load_panel(session: Session, f: SightingFilters) -> Panel:
    dialect = session.get_bind().dialect.name
    bucket = bucket_expr("month", dialect).label("m")
    all_rows = session.execute(
        f.apply(select(Sighting.city, Sighting.region, bucket, func.count().label("n"))).group_by(
            Sighting.city, Sighting.region, bucket
        )
    ).all()
    totals: dict[str, int] = {}
    unmapped: set[str] = set()
    for r in all_rows:
        totals[r.city] = totals.get(r.city, 0) + r.n
        if r.region == UNMAPPED_REGION:
            unmapped.add(r.city)
    keep = {c for c, n in totals.items() if n >= MIN_CITY_SIGHTINGS and c not in unmapped}
    if not keep and totals:  # a narrow filter (e.g. one quiet city): model what there is
        keep = set(totals) - unmapped or set(totals)
    excluded = [
        {
            "location": c,
            "sightings": n,
            "reason": "unmapped location" if c in unmapped else "too few sightings",
        }
        for c, n in sorted(totals.items())
        if c not in keep
    ]
    rows = [r for r in all_rows if r.city in keep]
    if not rows:
        raise InvalidQuery("No sightings match these filters, so no model can be fitted.", code="no_data")
    # span the whole dataset (respecting only the date filters) so zero months are kept
    span = SightingFilters(
        start=f.start, end=f.end, include_duplicates=f.include_duplicates, include_inactive=f.include_inactive
    )
    lo, hi = session.execute(
        span.apply(select(func.min(Sighting.observed_at), func.max(Sighting.observed_at)))
    ).one()
    months = bucket_range(lo.date(), hi.date(), "month")
    cities = sorted({r.city for r in rows})
    ci = {c: i for i, c in enumerate(cities)}
    mi = {m.isoformat(): j for j, m in enumerate(months)}
    Y = np.zeros((len(cities), len(months)))
    for r in rows:
        if r.m in mi:
            Y[ci[r.city], mi[r.m]] += r.n
    return Panel(cities, months, Y, excluded)


# ---------------------------------------------------------------------------
# Design matrices
# ---------------------------------------------------------------------------


def _effect_code(index: int, levels: int) -> np.ndarray:
    """Sum-to-zero coding: level k -> unit vector, last level -> all -1."""
    row = np.zeros(levels - 1)
    if index < levels - 1:
        row[index] = 1.0
    else:
        row[:] = -1.0
    return row


@dataclass
class Spec:
    trend: bool = True
    season: bool = True
    city: bool = True


@dataclass
class FittedModel:
    spec: Spec
    result: GLMResult
    t_centre: float
    cities: list[str]
    month0: date

    def row(self, month: date, city_idx: int | None) -> np.ndarray:
        parts = [np.ones(1)]
        if self.spec.trend:
            parts.append(np.array([_years(self.month0, month) - self.t_centre]))
        if self.spec.season:
            parts.append(_effect_code(month.month - 1, 12))
        if self.spec.city:
            if city_idx is None:
                parts.append(np.zeros(len(self.cities) - 1))  # the average city
            else:
                parts.append(_effect_code(city_idx, len(self.cities)))
        return np.concatenate(parts)

    def coef_index(self, name: str) -> int:
        return self.result.names.index(name)


def _years(month0: date, month: date) -> float:
    return (month.year - month0.year) + (month.month - month0.month) / 12.0


def _design(panel: Panel, spec: Spec, pooled: bool) -> tuple[np.ndarray, np.ndarray, list[str], float]:
    months, cities = panel.months, panel.cities
    t = np.array([_years(months[0], m) for m in months])
    t_centre = float(t.mean())
    names = ["intercept"]
    if spec.trend:
        names.append("trend_per_year")
    if spec.season:
        names += [f"season_{MONTH_NAMES[i]}" for i in range(11)]
    use_city = pooled and spec.city and len(cities) > 1
    if use_city:
        names += [f"city_{c}" for c in cities[:-1]]

    rows, y = [], []
    series = [(i, panel.Y[i]) for i in range(len(cities))] if pooled else [(None, panel.national)]
    for ci, counts in series:
        for j, m in enumerate(months):
            parts = [1.0]
            if spec.trend:
                parts.append(t[j] - t_centre)
            r = np.array(parts)
            if spec.season:
                r = np.concatenate([r, _effect_code(m.month - 1, 12)])
            if use_city:
                r = np.concatenate([r, _effect_code(ci, len(cities))])
            rows.append(r)
            y.append(counts[j])
    return np.vstack(rows), np.array(y), names, t_centre


def fit(panel: Panel, spec: Spec, pooled: bool) -> FittedModel:
    X, y, names, t_centre = _design(panel, spec, pooled)
    res = fit_poisson(X, y, names)
    eff_spec = Spec(spec.trend, spec.season, spec.city and pooled and len(panel.cities) > 1)
    return FittedModel(eff_spec, res, t_centre, panel.cities, panel.months[0])


def select_model(panel: Panel, pooled: bool) -> tuple[FittedModel, dict[str, Any]]:
    """Fit the full model, test each term by likelihood ratio, refit with the terms that matter."""
    full = fit(panel, Spec(True, True, True), pooled)
    tests: dict[str, Any] = {}
    keep = Spec(True, True, full.spec.city)

    no_season = fit(panel, Spec(True, False, full.spec.city), pooled)
    tests["seasonality"] = lr_test(full.result, no_season.result, df=11)
    no_trend = fit(panel, Spec(False, True, full.spec.city), pooled)
    tests["trend"] = lr_test(full.result, no_trend.result, df=1)
    if full.spec.city:
        no_city = fit(panel, Spec(True, True, False), pooled)
        tests["city_differences"] = lr_test(full.result, no_city.result, df=len(panel.cities) - 1)

    keep.season = tests["seasonality"]["p_value"] < ALPHA
    keep.trend = tests["trend"]["p_value"] < ALPHA
    # city effects are always kept in pooled models: they encode genuine exposure
    # differences (city size, reporting) even when not individually significant
    chosen = fit(panel, keep, pooled)
    for v in tests.values():
        v["p_value"] = round(float(v["p_value"]), 4)
        v["significant"] = bool(v["p_value"] < ALPHA)
    return chosen, {
        "full_model": full,
        "tests": tests,
        "selected_terms": [
            n
            for n, on in (("trend", keep.trend), ("seasonality", keep.season), ("city", chosen.spec.city))
            if on
        ],
    }


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def overview(session: Session, f: SightingFilters) -> dict[str, Any]:
    panel = load_panel(session, f)
    _chosen, sel = select_model(panel, pooled=True)
    full: FittedModel = sel["full_model"]
    res = full.result

    # trend
    trend = None
    if full.spec.trend:
        k = full.coef_index("trend_per_year")
        rr, lo, hi = res.rate_ratio(k)
        _, p = res.wald(k)
        trend = {
            "change_per_year_pct": round((rr - 1) * 100, 2),
            "ci95_pct": [round((lo - 1) * 100, 2), round((hi - 1) * 100, 2)],
            "p_value": round(p, 4),
            "significant": p < ALPHA,
            "direction": "flat" if p >= ALPHA else ("increasing" if rr > 1 else "decreasing"),
        }

    # seasonal profile (all 12 months, including the implied last level)
    season_profile = _season_profile(full)

    # city effects vs the average city, BH-adjusted
    hotspots = _city_effects(full, panel)

    nat = panel.national
    findings = _findings(trend, sel["tests"], season_profile, hotspots, panel)
    return {
        "data": {
            "sightings": int(panel.Y.sum()),
            "cities": len(panel.cities),
            "months": len(panel.months),
            "span": f"{panel.months[0]:%Y-%m} to {panel.last_month:%Y-%m}",
            "mean_per_month_national": round(float(nat.mean()), 2),
            "zero_city_months_pct": round(float((panel.Y == 0).mean() * 100), 1),
            "excluded_locations": panel.excluded,
        },
        "model": {
            "family": "Poisson GLM, log link, effect-coded month and city terms",
            "converged": res.converged,
            "dispersion_phi": round(res.phi, 3),
            "overdispersed": res.phi > 1.1,
            "deviance_explained_pct": round(max(0.0, 1 - res.deviance / res.null_deviance) * 100, 2)
            if res.null_deviance > 0
            else None,
            "selected_terms": sel["selected_terms"],
        },
        "tests": sel["tests"],
        "trend": trend,
        "seasonality": {"significant": sel["tests"]["seasonality"]["significant"], "profile": season_profile},
        "city_effects": hotspots,
        "findings": findings,
    }


def _season_profile(model: FittedModel) -> list[dict[str, Any]]:
    res = model.result
    if not model.spec.season:
        return []
    idx = [model.coef_index(f"season_{MONTH_NAMES[i]}") for i in range(11)]
    beta = res.beta[idx]
    cov = res.cov[np.ix_(idx, idx)]
    effects = list(beta) + [-beta.sum()]
    var_last = float(np.ones(11) @ cov @ np.ones(11))
    variances = list(np.diag(cov)) + [var_last]
    out = []
    for m in range(12):
        se = math.sqrt(max(variances[m], 0))
        out.append(
            {
                "month": m + 1,
                "name": MONTH_NAMES[m],
                "index": round(math.exp(effects[m]), 3),
                "ci95": [
                    round(math.exp(effects[m] - 1.96 * se), 3),
                    round(math.exp(effects[m] + 1.96 * se), 3),
                ],
            }
        )
    return out


def _city_effects(model: FittedModel, panel: Panel) -> list[dict[str, Any]]:
    if not model.spec.city:
        return []
    res = model.result
    idx = [model.coef_index(f"city_{c}") for c in panel.cities[:-1]]
    beta = res.beta[idx]
    cov = res.cov[np.ix_(idx, idx)]
    effects = list(beta) + [-beta.sum()]
    variances = list(np.diag(cov)) + [float(np.ones(len(idx)) @ cov @ np.ones(len(idx)))]
    pvals = []
    rows = []
    for i, c in enumerate(panel.cities):
        se = math.sqrt(max(variances[i], 1e-12))
        z = effects[i] / se
        p = float(2 * stats.norm.sf(abs(z)))
        pvals.append(p)
        rows.append(
            {
                "city": c,
                "sightings": int(panel.Y[i].sum()),
                "rate_ratio_vs_average": round(math.exp(effects[i]), 3),
                "ci95": [
                    round(math.exp(effects[i] - 1.96 * se), 3),
                    round(math.exp(effects[i] + 1.96 * se), 3),
                ],
                "p_value": round(p, 4),
            }
        )
    for row, q in zip(rows, bh_adjust(pvals), strict=True):
        row["q_value"] = round(q, 4)
        row["classification"] = (
            "hotspot"
            if q < ALPHA and row["rate_ratio_vs_average"] > 1
            else "coldspot"
            if q < ALPHA
            else "typical"
        )
    return sorted(rows, key=lambda r: -r["rate_ratio_vs_average"])


def _signed_pct(x: float) -> str:
    return "0.0%" if abs(x) < 0.05 else f"{x:+.1f}%"


def _findings(trend, tests, season_profile, hotspots, panel: Panel) -> list[str]:
    out = []
    if trend:
        if trend["significant"]:
            out.append(
                f"Sightings are {trend['direction']} by about {abs(trend['change_per_year_pct']):.1f}% per year "
                f"(95% CI {trend['ci95_pct'][0]:.1f}% to {trend['ci95_pct'][1]:.1f}%, p={trend['p_value']})."
            )
        else:
            out.append(
                f"No significant long-term trend (estimated {_signed_pct(trend['change_per_year_pct'])}/year, "
                f"p={trend['p_value']}): activity has been broadly stable over {panel.months[0].year}-"
                f"{panel.last_month.year}."
            )
    s = tests["seasonality"]
    if s["significant"]:
        peak = sorted(season_profile, key=lambda r: -r["index"])[:3]
        out.append(
            f"Clear seasonal cycle (p={s['p_value']}); peak months: {', '.join(p['name'] for p in peak)}."
        )
    else:
        out.append(
            f"No significant seasonal cycle in this dataset (p={s['p_value']}). Real UK tick activity is "
            "strongly seasonal (spring/early-summer peak), so month-level patterns here should be treated "
            "with caution; the risk score falls back to a documented seasonal prior."
        )
    hot = [h for h in hotspots if h["classification"] == "hotspot"]
    cold = [h for h in hotspots if h["classification"] == "coldspot"]
    omnibus = tests.get("city_differences")
    if hot:
        msg = (
            "Statistically significant hotspots (FDR 5%): "
            + ", ".join(f"{h['city']} ({h['rate_ratio_vs_average']:.2f}x average)" for h in hot)
            + "."
        )
        if omnibus and not omnibus["significant"]:
            msg += (
                f" Overall variation across all {len(hotspots)} cities is within chance "
                f"(omnibus p={omnibus['p_value']}): a single stand-out rather than a broad gradient."
            )
        out.append(msg)
    elif hotspots:
        top = hotspots[0]
        out.append(
            f"{top['city']} has the highest rate ({top['rate_ratio_vs_average']:.2f}x the average city) but no "
            "city differs from the average by more than chance once multiple comparisons are accounted for."
        )
    if cold:
        out.append("Significantly below average: " + ", ".join(c["city"] for c in cold) + ".")
    return out


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


def _simulate(model: FittedModel, rows: np.ndarray, n_sims: int = 4000) -> np.ndarray:
    """Posterior-predictive draws: parameter uncertainty + count noise (+ overdispersion)."""
    rng = np.random.default_rng(RNG_SEED)
    res = model.result
    try:
        betas = rng.multivariate_normal(res.beta, res.cov, size=n_sims, method="cholesky")
    except np.linalg.LinAlgError:
        betas = rng.multivariate_normal(res.beta, res.cov + np.eye(len(res.beta)) * 1e-9, size=n_sims)
    mu = np.exp(np.clip(betas @ rows.T, -30, 30))  # (n_sims, horizon)
    if res.phi > 1.1:
        shape = mu / (res.phi - 1)  # gamma-Poisson (negative binomial) with Var = phi * mu
        mu = rng.gamma(shape, (res.phi - 1), size=mu.shape)
    return rng.poisson(mu)


def forecast(
    session: Session, f: SightingFilters, city: str | None, horizon: int = 12, backtest_months: int = 12
) -> dict[str, Any]:
    if not 1 <= horizon <= 36:
        raise InvalidQuery("horizon must be between 1 and 36 months", code="invalid_horizon")
    panel = load_panel(session, f)
    if city is not None and city not in panel.cities:
        raise InvalidQuery(
            f"No sightings for '{city}' with these filters.",
            code="no_data",
            details={"cities_with_data": panel.cities},
        )
    pooled = city is not None
    model, sel = select_model(panel, pooled=pooled)
    city_idx = panel.cities.index(city) if city else None

    future = []
    m = panel.last_month
    for _ in range(horizon):
        m = next_bucket(m, "month")
        future.append(m)
    rows = np.vstack([model.row(fm, city_idx) for fm in future])
    expected = model.result.predict_mu(rows)
    sims = _simulate(model, rows)
    q = np.percentile(sims, [2.5, 10, 50, 90, 97.5], axis=0)

    history_series = panel.Y[city_idx] if city_idx is not None else panel.national
    points = [
        {
            "period": fm.isoformat(),
            "label": f"{fm:%Y-%m}",
            "expected": round(float(expected[i]), 3),
            "median": int(q[2, i]),
            "pi80": [int(q[1, i]), int(q[3, i])],
            "pi95": [int(q[0, i]), int(q[4, i])],
            "p_at_least_one": round(float((sims[:, i] > 0).mean()), 3),
        }
        for i, fm in enumerate(future)
    ]
    history = [
        {"period": mm.isoformat(), "label": f"{mm:%Y-%m}", "count": int(history_series[j])}
        for j, mm in enumerate(panel.months[-24:], start=len(panel.months) - min(24, len(panel.months)))
    ]

    return {
        "scope": city or "UK (all cities)",
        "horizon_months": horizon,
        "forecast_from": f"{next_bucket(panel.last_month, 'month'):%Y-%m}",
        "model": {
            "selected_terms": sel["selected_terms"],
            "term_tests": sel["tests"],
            "dispersion_phi": round(model.result.phi, 3),
            "pooled_across_cities": pooled,
            "intervals": "posterior-predictive simulation (4,000 draws) incl. parameter uncertainty",
        },
        "total_expected": round(float(expected.sum()), 2),
        "points": points,
        "recent_history": history,
        "backtest": _backtest(panel, city_idx, backtest_months),
    }


def _backtest(panel: Panel, city_idx: int | None, holdout: int) -> dict[str, Any]:
    """Rolling-origin style check: train on everything but the last `holdout` months."""
    if len(panel.months) < holdout + 24:
        return {"status": "insufficient_history"}
    train = Panel(panel.cities, panel.months[:-holdout], panel.Y[:, :-holdout])
    test_months = panel.months[-holdout:]
    actual = (panel.Y[city_idx] if city_idx is not None else panel.national)[-holdout:]
    model, _ = select_model(train, pooled=city_idx is not None)
    rows = np.vstack([model.row(m, city_idx) for m in test_months])
    pred = model.result.predict_mu(rows)
    sims = _simulate(model, rows, n_sims=2000)
    lo, hi = np.percentile(sims, [10, 90], axis=0)

    hist = train.Y[city_idx] if city_idx is not None else train.national
    mean_baseline = np.full(holdout, hist.mean())
    seasonal_naive = hist[-12:][:holdout] if len(hist) >= 12 else mean_baseline
    if len(seasonal_naive) < holdout:
        seasonal_naive = np.resize(seasonal_naive, holdout)

    def mae(p: np.ndarray) -> float:
        return float(np.mean(np.abs(actual - p)))

    m_model, m_mean, m_naive = mae(pred), mae(mean_baseline), mae(seasonal_naive)
    best_baseline = min(m_mean, m_naive)
    return {
        "status": "ok",
        "holdout": f"{test_months[0]:%Y-%m} to {test_months[-1]:%Y-%m}",
        "mae_model": round(m_model, 3),
        "mae_historical_mean": round(m_mean, 3),
        "mae_seasonal_naive": round(m_naive, 3),
        "skill_vs_best_baseline_pct": round((1 - m_model / best_baseline) * 100, 1)
        if best_baseline
        else None,
        "pi80_coverage_pct": round(float(np.mean((actual >= lo) & (actual <= hi)) * 100), 1),
        "note": "Skill > 0 means the model beats the naive baseline; PI80 coverage near 80% means "
        "the uncertainty bands are honest.",
    }


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def anomalies(
    session: Session, f: SightingFilters, granularity: str = "quarter", alpha: float = ALPHA
) -> dict:
    if granularity not in ("month", "quarter", "year"):
        raise InvalidQuery("granularity must be month, quarter or year", code="invalid_granularity")
    panel = load_panel(session, f)
    model, sel = select_model(panel, pooled=True)
    res = model.result
    k = {"month": 1, "quarter": 3, "year": 12}[granularity]

    # expected per city-month from the selected model
    X, _, _, _ = _design(panel, model.spec, pooled=model.spec.city)
    mu = res.predict_mu(X).reshape(len(panel.cities) if model.spec.city else 1, len(panel.months))
    if not model.spec.city:  # national model -> split by each city's share
        shares = panel.Y.sum(axis=1, keepdims=True) / max(panel.Y.sum(), 1)
        mu = shares * mu

    groups: dict[str, list[int]] = {}
    for j, m in enumerate(panel.months):
        key = f"{m.year}-Q{(m.month - 1) // 3 + 1}" if k == 3 else f"{m:%Y-%m}" if k == 1 else f"{m.year}"
        groups.setdefault(key, []).append(j)

    phi = max(res.phi, 1.0)
    tests = []
    for ci, city in enumerate(panel.cities):
        for label, js in groups.items():
            obs = float(panel.Y[ci, js].sum())
            exp_ = float(mu[ci, js].sum())
            p_high, p_low = _tail_probs(obs, exp_, phi)
            direction, p = ("spike", p_high) if obs >= exp_ else ("drop", p_low)
            tests.append(
                {
                    "city": city,
                    "period": label,
                    "observed": int(obs),
                    "expected": round(exp_, 2),
                    "ratio": round(obs / exp_, 2) if exp_ > 0 else None,
                    "direction": direction,
                    "p_value": p,
                }
            )
    qvals = bh_adjust([t["p_value"] for t in tests])
    for t, qv in zip(tests, qvals, strict=True):
        t["q_value"] = round(qv, 4)
        t["p_value"] = round(t["p_value"], 5)
    flagged = sorted([t for t in tests if t["q_value"] < alpha], key=lambda t: t["p_value"])
    most_unusual = sorted(tests, key=lambda t: t["p_value"])[:10]
    return {
        "granularity": granularity,
        "alpha_fdr": alpha,
        "tests_run": len(tests),
        "method": "observed vs model-expected count; Poisson/neg-binomial tail probability; "
        "Benjamini-Hochberg FDR across all city-periods",
        "model_terms": sel["selected_terms"],
        "anomalies": flagged,
        "anomaly_count": len(flagged),
        "most_unusual": most_unusual,
        "interpretation": (
            f"{len(flagged)} city-period(s) are unusual at a {alpha:.0%} false discovery rate."
            if flagged
            else "No city-period is unusual once the number of comparisons is accounted for. 'most_unusual' lists "
            "the closest candidates for manual review."
        ),
    }


def _tail_probs(obs: float, mu: float, phi: float) -> tuple[float, float]:
    """(P[X >= obs], P[X <= obs]) under Poisson, or negative binomial when overdispersed."""
    mu = max(mu, 1e-9)
    if phi > 1.1:
        n = mu / (phi - 1)
        p = n / (n + mu)
        return float(stats.nbinom.sf(obs - 1, n, p)), float(stats.nbinom.cdf(obs, n, p))
    return float(stats.poisson.sf(obs - 1, mu)), float(stats.poisson.cdf(obs, mu))


# ---------------------------------------------------------------------------
# Risk score
# ---------------------------------------------------------------------------

LEVELS = [(30, "Low"), (55, "Moderate"), (75, "High"), (101, "Very high")]


def risk(
    session: Session, f: SightingFilters, target_month: str | None = None, window_months: int = 24
) -> dict:
    if not 6 <= window_months <= 120:
        raise InvalidQuery("window_months must be between 6 and 120", code="invalid_window")
    target = _parse_month(target_month) if target_month else date.today().replace(day=1)
    panel = load_panel(session, f)
    model, sel = select_model(panel, pooled=True)
    window = min(window_months, len(panel.months))
    post, shrink, y, baseline = _recent_rates(panel, window)
    rel = post / post.mean()

    seasonal_source = "data" if sel["tests"]["seasonality"]["significant"] else "prior"
    if seasonal_source == "data":
        prof = {p["month"]: p["index"] for p in _season_profile(sel["full_model"])}
        season = prof[target.month]
    else:
        season = SEASONAL_PRIOR[target.month]

    trend_factor, trend_note = 1.0, "no significant trend"
    if sel["tests"]["trend"]["significant"] and model.spec.trend:
        k = model.coef_index("trend_per_year")
        years_ahead = _years(panel.last_month, target)
        trend_factor = float(np.clip(math.exp(model.result.beta[k] * years_ahead), 0.5, 2.0))
        trend_note = f"trend extrapolated {years_ahead:.1f} years ahead"

    staleness_days = (date.today() - panel.last_month).days
    out = []
    for i, city in enumerate(panel.cities):
        combined = float(rel[i] * season * trend_factor)
        score = 100.0 / (1.0 + math.exp(-2.0 * math.log(max(combined, 1e-6))))
        expected = float(post[i] * season * trend_factor)
        level = next(name for cut, name in LEVELS if score < cut)
        place = PLACES.get(city)
        out.append(
            {
                "city": city,
                "region": place.region if place else None,
                "lat": place.lat if place else None,
                "lon": place.lon if place else None,
                "score": round(score, 1),
                "level": level,
                "expected_sightings": round(expected, 2),
                "p_at_least_one_sighting": round(1 - math.exp(-expected), 3),
                "components": {
                    "relative_activity": round(float(rel[i]), 3),
                    "recent_sightings": int(y[i]),
                    "long_term_monthly_rate": round(float(baseline[i]), 3),
                    "weight_on_long_term": round(float(shrink[i]), 3),
                    "seasonal_factor": round(float(season), 3),
                    "seasonal_source": seasonal_source,
                    "trend_factor": round(trend_factor, 3),
                },
                "explanation": (
                    f"{city}: activity is {rel[i]:.2f}x the city average "
                    f"({int(y[i])} sightings in the last {window} months of data, blended with its long-term rate); "
                    f"{MONTH_NAMES[target.month - 1]} seasonal factor {season:.2f} ({seasonal_source}); "
                    f"{trend_note}. Score {score:.0f}/100 -> {level}."
                ),
            }
        )
    out.sort(key=lambda r: -r["score"])
    for rank, r in enumerate(out, start=1):
        r["rank"] = rank
    warnings = []
    if staleness_days > 180:
        warnings.append(
            f"The latest data is from {panel.last_month:%B %Y} ({staleness_days} days ago); scores describe "
            "typical activity, not live conditions."
        )
    if seasonal_source == "prior":
        warnings.append(
            "Seasonality is not detectable in this dataset, so a general UK tick-activity profile "
            "(spring/early-summer peak) is used as a configurable prior."
        )
    return {
        "target_month": f"{target:%Y-%m}",
        "window_months": window,
        "method": "empirical-Bayes city rate (recent window blended with long-term rate) x seasonal factor "
        "x trend factor -> logistic 0-100 score (50 = an average city in an average month)",
        "levels": {name: f"< {cut}" for cut, name in LEVELS},
        "warnings": warnings,
        "cities": out,
    }


def _recent_rates(panel: Panel, window: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Empirical-Bayes blend of each city's *recent* rate with its *long-term* rate.

    Prior for city c's recent monthly rate: Gamma with mean = long-term rate (from the
    months before the window) and a multiplicative spread tau^2 estimated from the data
    by method of moments. If cities' recent counts wobble no more than Poisson noise,
    tau^2 -> 0 and we trust the long-term rate; if they genuinely shift, recent data wins.
    Returns (posterior rate, weight on long-term, recent counts, long-term rate).
    """
    y = panel.Y[:, -window:].sum(axis=1)
    history = panel.Y[:, :-window] if panel.Y.shape[1] > window + 12 else panel.Y
    baseline = np.maximum(history.mean(axis=1), 1e-6)
    expected = baseline * window
    excess = ((y - expected) ** 2 - expected) / np.maximum(expected**2, 1e-9)
    tau2 = float(np.clip(excess.mean(), 0.01, 25.0))
    alpha = 1.0 / tau2
    beta = alpha / baseline
    post = (alpha + y) / (beta + window)
    weight_on_prior = beta / (beta + window)
    return post, weight_on_prior, y, baseline


def risk_near(
    session: Session, f: SightingFilters, lat: float, lon: float, radius_km: float, target_month: str | None
) -> dict[str, Any]:
    result = risk(session, f, target_month)
    nearby = []
    for r in result["cities"]:
        if r["lat"] is None:
            continue
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d <= radius_km:
            nearby.append({**r, "distance_km": round(d, 1)})
    nearby.sort(key=lambda r: r["distance_km"])
    if nearby:
        weights = [1 / max(n["distance_km"], 5.0) for n in nearby]
        local = sum(w * n["score"] for w, n in zip(weights, nearby, strict=True)) / sum(weights)
        level = next(name for cut, name in LEVELS if local < cut)
    else:
        local, level = None, None
    return {
        "location": {"lat": lat, "lon": lon, "radius_km": radius_km},
        "target_month": result["target_month"],
        "local_score": round(local, 1) if local is not None else None,
        "local_level": level,
        "method": "inverse-distance weighted average of city scores within the radius",
        "nearby": nearby,
        "warnings": result["warnings"]
        + ([] if nearby else [f"No tracked city within {radius_km:g} km; try a larger radius."]),
    }


def _parse_month(text: str) -> date:
    try:
        d = datetime.strptime(text.strip()[:7], "%Y-%m")
        return date(d.year, d.month, 1)
    except ValueError as exc:
        raise InvalidQuery("month must look like 2025-05", code="invalid_month") from exc
