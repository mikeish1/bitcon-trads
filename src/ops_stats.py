"""
Statistics for the ops agent's degradation detection (pure numpy, no SciPy).

All tests are intentionally lightweight and reproducible:

  * bootstrap_mean_ci / bootstrap_diff_ci - percentile confidence intervals on a
    mean (or a difference of means) by resampling with replacement (seeded).
  * mann_whitney_u - non-parametric two-sample test on daily returns, p-value via
    the tie-corrected normal approximation (exact enough for n >= ~20).
  * welch_t - Welch's unequal-variance t statistic; p-value via the normal
    approximation (valid for the df we have, ~20-90 daily obs; documented).
  * zscore_vs_distribution - how many sigma a live metric sits below a backtest
    distribution of the same metric (for "worse than historically normal").

`flag_degradation` combines these into severity-graded flags against configured
thresholds. Everything is read-only and deterministic given a seed.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def _norm_cdf(z: float) -> float:
    """Standard-normal CDF via erf (no SciPy)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def daily_returns(equity: Any) -> np.ndarray:
    """Daily simple returns from an equity series; drops non-finite / non-positive."""
    eq = np.asarray(equity, dtype="float64")
    eq = eq[np.isfinite(eq) & (eq > 0)]
    if len(eq) < 2:
        return np.array([], dtype="float64")
    r = np.diff(eq) / eq[:-1]
    return r[np.isfinite(r)]


def bootstrap_mean_ci(x: np.ndarray, n_boot: int = 2000, alpha: float = 0.05,
                      seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean. (nan, nan) if too few points."""
    x = np.asarray(x, dtype="float64")
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def bootstrap_diff_ci(a: np.ndarray, b: np.ndarray, n_boot: int = 2000,
                      alpha: float = 0.05, seed: int = 0) -> dict[str, float]:
    """Bootstrap CI for mean(a) - mean(b) (a=live, b=backtest). 'adverse' is True
    when the whole CI is below 0 (live mean significantly below backtest)."""
    a = np.asarray(a, dtype="float64"); a = a[np.isfinite(a)]
    b = np.asarray(b, dtype="float64"); b = b[np.isfinite(b)]
    if len(a) < 3 or len(b) < 3:
        return {"point": float("nan"), "lo": float("nan"), "hi": float("nan"), "adverse": False}
    rng = np.random.default_rng(seed)
    da = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    db = b[rng.integers(0, len(b), size=(n_boot, len(b)))].mean(axis=1)
    diff = da - db
    lo, hi = np.percentile(diff, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"point": float(a.mean() - b.mean()), "lo": float(lo), "hi": float(hi),
            "adverse": bool(hi < 0)}


def mann_whitney_u(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """Mann-Whitney U two-sample test; two-sided p via tie-corrected normal approx."""
    a = np.asarray(a, dtype="float64"); a = a[np.isfinite(a)]
    b = np.asarray(b, dtype="float64"); b = b[np.isfinite(b)]
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return {"U": float("nan"), "p": float("nan")}
    allv = np.concatenate([a, b])
    order = allv.argsort()
    ranks = np.empty(len(allv), dtype="float64")
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ranks for ties
    _, inv, counts = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts)); np.add.at(sums, inv, ranks)
    avg = sums / counts
    ranks = avg[inv]
    r1 = ranks[:n1].sum()
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u = min(u1, n1 * n2 - u1)
    mu = n1 * n2 / 2.0
    tie = np.sum(counts ** 3 - counts)
    n = n1 + n2
    sigma2 = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1)))
    if sigma2 <= 0:
        return {"U": float(u), "p": 1.0}
    z = (u - mu) / math.sqrt(sigma2)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return {"U": float(u), "p": float(min(1.0, max(0.0, p)))}


def welch_t(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """Welch's t (unequal variance). Two-sided p via normal approximation (the df
    here, ~20-90 obs, makes this close to the exact t-test; documented)."""
    a = np.asarray(a, dtype="float64"); a = a[np.isfinite(a)]
    b = np.asarray(b, dtype="float64"); b = b[np.isfinite(b)]
    if len(a) < 3 or len(b) < 3:
        return {"t": float("nan"), "p": float("nan")}
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return {"t": 0.0, "p": 1.0}
    t = (a.mean() - b.mean()) / se
    p = 2.0 * (1.0 - _norm_cdf(abs(t)))
    return {"t": float(t), "p": float(min(1.0, max(0.0, p)))}


def zscore_vs_distribution(value: float, samples: Any) -> float:
    """Signed z of `value` within `samples` (negative = value below the mean)."""
    s = np.asarray(samples, dtype="float64"); s = s[np.isfinite(s)]
    if len(s) < 3 or s.std() == 0 or not np.isfinite(value):
        return float("nan")
    return float((value - s.mean()) / s.std())


def flag_degradation(live_ret: np.ndarray, bt_ret: np.ndarray,
                     live_window_metrics: dict[str, float],
                     bt_window_dist: dict[str, list[float]],
                     thresholds: dict[str, Any], seed: int = 0) -> dict[str, Any]:
    """Combine the tests into severity-graded degradation flags.

    Returns {"flags": [...], "severity": none|low|medium|high, "stats": {...}}.
    A flag fires when the live return distribution is significantly worse than the
    backtest's (p < pvalue AND bootstrap diff CI adverse), or a live window metric
    sits worse than `dd_z` sigma from the backtest distribution of that metric."""
    pmax = float(thresholds.get("pvalue", 0.05))
    dd_z = float(thresholds.get("dd_z", 2.0))
    flags: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}

    if len(live_ret) >= 3 and len(bt_ret) >= 3:
        mwu = mann_whitney_u(live_ret, bt_ret)
        wt = welch_t(live_ret, bt_ret)
        diff = bootstrap_diff_ci(live_ret, bt_ret, seed=seed)
        stats.update({"mann_whitney": mwu, "welch_t": wt, "mean_diff_ci": diff})
        worse_mean = float(np.mean(live_ret)) < float(np.mean(bt_ret))
        sig = (mwu["p"] == mwu["p"] and mwu["p"] < pmax) or (wt["p"] == wt["p"] and wt["p"] < pmax)
        if worse_mean and sig and diff["adverse"]:
            flags.append({"metric": "daily_return_distribution", "severity": "high",
                          "detail": f"live mean {np.mean(live_ret):.4%}/day < backtest "
                                    f"{np.mean(bt_ret):.4%}/day; MWU p={mwu['p']:.3f}, "
                                    f"Welch p={wt['p']:.3f}, mean-diff CI "
                                    f"[{diff['lo']:.4%},{diff['hi']:.4%}] (adverse)",
                          "investigate": "execution slippage, regime mismatch, signal decay"})
        elif worse_mean and sig:
            flags.append({"metric": "daily_return_distribution", "severity": "medium",
                          "detail": f"live mean below backtest; MWU p={mwu['p']:.3f} but the "
                                    f"mean-diff CI still spans 0 - monitor",
                          "investigate": "collect more live days; check slippage"})

    # Window-metric distribution flags (calmar / max_dd / total_return).
    for key, direction in (("calmar", "low"), ("max_dd", "low"), ("total_return", "low")):
        lv = live_window_metrics.get(key)
        dist = bt_window_dist.get(key)
        if lv is None or lv != lv or not dist:
            continue
        z = zscore_vs_distribution(lv, dist)
        stats[f"{key}_z"] = z
        if z == z and z <= -dd_z:
            sev = "high" if z <= -(dd_z + 1.0) else "medium"
            flags.append({"metric": key, "severity": sev,
                          "detail": f"live {key}={lv:.3f} is {z:.1f}σ below the backtest "
                                    f"distribution (n={len(dist)})",
                          "investigate": "regime shift, parameter drift, or position sizing"})

    sev_rank = {"low": 1, "medium": 2, "high": 3}
    severity = "none"
    if flags:
        top = max(flags, key=lambda f: sev_rank[f["severity"]])["severity"]
        severity = top
    return {"flags": flags, "severity": severity, "stats": stats}
