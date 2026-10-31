"""A small, dependency-light Poisson GLM (log link) fitted by IRLS.

Why a Poisson GLM? Sightings are *counts of events per period*: small,
non-negative integers with variance that grows with the mean. Ordinary least
squares on such data gives nonsense like "-150% per year" and over-confident
p-values. A log-linear Poisson model is the textbook tool, and it is also
interpretable: ``exp(beta)`` is a rate ratio ("Nottingham sees 1.3x the average").

Overdispersion (variance > mean, common in crowdsourced data) is handled the
quasi-Poisson way: standard errors are inflated by the Pearson dispersion phi.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass
class GLMResult:
    beta: np.ndarray
    cov: np.ndarray  # dispersion-adjusted covariance
    mu: np.ndarray
    deviance: float
    null_deviance: float
    df_resid: int
    phi: float  # Pearson dispersion estimate (>=1 used for inference)
    converged: bool
    iterations: int
    names: list[str]

    @property
    def se(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 0, None))

    def wald(self, idx: int) -> tuple[float, float]:
        """(z, two-sided p) for coefficient ``idx``."""
        se = self.se[idx]
        if se == 0 or not np.isfinite(se):
            return 0.0, 1.0
        z = float(self.beta[idx] / se)
        return z, float(2 * stats.norm.sf(abs(z)))

    def rate_ratio(self, idx: int, scale: float = 1.0) -> tuple[float, float, float]:
        """exp(scale*beta) with a 95% CI."""
        b, se = self.beta[idx] * scale, self.se[idx] * abs(scale)
        return float(np.exp(b)), float(np.exp(b - 1.96 * se)), float(np.exp(b + 1.96 * se))

    def predict_mu(self, X: np.ndarray) -> np.ndarray:
        return np.exp(np.clip(X @ self.beta, -30, 30))


def fit_poisson(
    X: np.ndarray,
    y: np.ndarray,
    names: list[str] | None = None,
    *,
    ridge: float = 1e-6,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> GLMResult:
    """Fit log(mu) = X @ beta by iteratively re-weighted least squares.

    A tiny ridge penalty (not applied to the intercept, assumed to be column 0)
    keeps the solve stable when a dummy column is all zeros.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, p = X.shape
    penalty = np.eye(p) * ridge
    penalty[0, 0] = 0.0

    beta = np.zeros(p)
    beta[0] = np.log(max(y.mean(), 1e-6))
    converged = False
    it = 0
    for it in range(1, max_iter + 1):  # noqa: B007
        eta = np.clip(X @ beta, -30, 30)
        mu = np.exp(eta)
        z = eta + (y - mu) / mu
        XtW = X.T * mu
        H = XtW @ X + penalty
        beta_new = np.linalg.solve(H, XtW @ z)
        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            converged = True
            break
        beta = beta_new

    mu = np.exp(np.clip(X @ beta, -30, 30))
    deviance = _deviance(y, mu)
    null_dev = _deviance(y, np.full_like(y, y.mean() if y.mean() > 0 else 1e-9))
    df_resid = max(n - p, 1)
    pearson = float(np.sum((y - mu) ** 2 / np.maximum(mu, 1e-12)))
    phi = pearson / df_resid
    H = (X.T * mu) @ X + penalty
    cov = np.linalg.pinv(H) * max(phi, 1.0)
    return GLMResult(
        beta, cov, mu, deviance, null_dev, df_resid, phi, converged, it, names or [f"x{i}" for i in range(p)]
    )


def _deviance(y: np.ndarray, mu: np.ndarray) -> float:
    with np.errstate(divide="ignore", invalid="ignore"):
        term = np.where(y > 0, y * np.log(y / mu), 0.0)
    return float(2 * np.sum(term - (y - mu)))


def lr_test(full: GLMResult, reduced: GLMResult, df: int) -> dict[str, float | bool]:
    """Likelihood-ratio (deviance) test for nested models, with an F version under overdispersion."""
    delta = max(reduced.deviance - full.deviance, 0.0)
    if full.phi > 1.1:
        f_stat = (delta / df) / full.phi
        p = float(stats.f.sf(f_stat, df, full.df_resid))
        return {"statistic": round(f_stat, 4), "df": df, "p_value": p, "method": "F (quasi-Poisson)"}
    p = float(stats.chi2.sf(delta, df))
    return {"statistic": round(delta, 4), "df": df, "p_value": p, "method": "chi-square LR"}


def bh_adjust(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg false discovery rate adjustment."""
    m = len(pvalues)
    if m == 0:
        return []
    order = np.argsort(pvalues)
    ranked = np.array(pvalues)[order] * m / (np.arange(m) + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(adjusted, 1.0)
    return out.tolist()


def poisson_trend(t_years: list[float], counts: list[int]) -> dict:
    """Log-linear trend test for a single count series."""
    y = np.asarray(counts, dtype=float)
    if len(y) < 4 or y.sum() < 10:
        return {
            "method": "poisson_glm",
            "status": "insufficient_data",
            "significant": False,
            "direction": "unknown",
            "change_per_year_pct": None,
            "p_value": None,
        }
    t = np.asarray(t_years, dtype=float)
    X = np.column_stack([np.ones_like(t), t - t.mean()])
    res = fit_poisson(X, y, ["intercept", "year"])
    _, p = res.wald(1)
    rr, lo, hi = res.rate_ratio(1)
    significant = p < 0.05
    return {
        "method": "poisson_glm",
        "status": "ok",
        "change_per_year_pct": round((rr - 1) * 100, 2),
        "ci95_pct": [round((lo - 1) * 100, 2), round((hi - 1) * 100, 2)],
        "p_value": round(p, 4),
        "dispersion": round(res.phi, 3),
        "significant": bool(significant),
        "direction": "flat" if not significant else ("increasing" if rr > 1 else "decreasing"),
    }
