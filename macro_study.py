"""
macro_study.py -- test a macro score against equity returns without fooling yourself.

Pipeline:
  1. align macro score and prices, difference the score, build forward returns
  2. residualize returns on factors (market, or FF3) to strip beta
  3. OLS with Newey-West (HAC) standard errors -- valid under overlapping windows
  4. circular-shift bootstrap p-value: preserves each series' autocorrelation,
     destroys the cross-alignment. This is the honest null for persistent series.
  5. Benjamini-Hochberg FDR across the universe
  6. out-of-sample confirmation on a held-out tail of the history

Dependencies: numpy, pandas, scipy.

Usage:
    python macro_study.py --demo
    python macro_study.py --macro macro.csv --prices prices.csv [--factors factors.csv]

CSV formats (first column = date, parsed as the index):
    macro.csv    date, score
    prices.csv   date, TICKER_A, TICKER_B, ...   (levels, not returns)
    factors.csv  date, mkt_rf, smb, hml, ...     (optional; per-period, decimals)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats


# ----------------------------------------------------------------------------- config


@dataclass
class Config:
    horizon: int = 1              # forward-return horizon, in periods
    diff_score: bool = True       # test CHANGES in the score, not levels
    log_returns: bool = True
    residualize: bool = True      # strip factor exposure before testing
    holdout_frac: float = 0.30    # tail of the sample reserved for confirmation
    min_obs: int = 30
    n_boot: int = 2000
    fdr_alpha: float = 0.10
    oos_alpha: float = 0.10
    seed: int = 0
    factor_cols: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------- linear algebra


def ols(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Least squares via lstsq (stable under near-collinear factors)."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta, y - X @ beta


def hac_se(X: np.ndarray, resid: np.ndarray, lag: int) -> np.ndarray:
    """
    Newey-West sandwich standard errors with a Bartlett kernel.
    Necessary whenever forward returns overlap: OLS SEs are badly understated there.
    """
    n, k = X.shape
    u = X * resid[:, None]                      # score contributions
    S = u.T @ u
    for j in range(1, lag + 1):
        w = 1.0 - j / (lag + 1.0)               # Bartlett weight
        G = u[j:].T @ u[:-j]
        S += w * (G + G.T)
    XtX_inv = np.linalg.pinv(X.T @ X)
    V = XtX_inv @ S @ XtX_inv * (n / max(n - k, 1))   # small-sample correction
    return np.sqrt(np.maximum(np.diag(V), 0.0))


def nw_lag(n_obs: int, horizon: int) -> int:
    """Andrews-style bandwidth, floored at the overlap length."""
    rule = int(np.floor(4 * (n_obs / 100.0) ** (2.0 / 9.0)))
    return max(rule, horizon - 1, 1)


def benjamini_hochberg(p: np.ndarray, alpha: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (q_values, reject_flags) controlling the false discovery rate."""
    p = np.asarray(p, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    q_sorted = np.minimum.accumulate((ranked * n / np.arange(1, n + 1))[::-1])[::-1]
    q_sorted = np.minimum(q_sorted, 1.0)
    q = np.empty(n)
    q[order] = q_sorted
    return q, q <= alpha


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    xc, yc = x - x.mean(), y - y.mean()
    denom = np.sqrt((xc @ xc) * (yc @ yc))
    return 0.0 if denom == 0 else float((xc @ yc) / denom)


def min_detectable_r(n_obs: int, alpha: float = 0.05) -> float:
    """Smallest |r| that clears nominal significance at this sample size."""
    if n_obs < 4:
        return float("nan")
    t = stats.t.ppf(1 - alpha / 2, n_obs - 2)
    return float(np.sqrt(t**2 / (t**2 + n_obs - 2)))


# ----------------------------------------------------------------------------- prep


def load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True).sort_index()


def forward_returns(prices: pd.DataFrame, horizon: int, log: bool) -> pd.DataFrame:
    """Return at time t = the return earned from t to t+horizon (unknown at t)."""
    if log:
        return np.log(prices).diff(horizon).shift(-horizon)
    return prices.pct_change(horizon).shift(-horizon)


def prepare(macro, prices, factors, cfg: Config):
    """Align everything on a common index. Returns (x, forward_returns, factors)."""
    x = macro.diff() if cfg.diff_score else macro.copy()
    y = forward_returns(prices, cfg.horizon, cfg.log_returns)

    if factors is None:
        # fallback market proxy: equal-weighted cross-sectional mean of the panel
        step = np.log(prices).diff() if cfg.log_returns else prices.pct_change()
        mkt = step.mean(axis=1)
        fwd = mkt.rolling(cfg.horizon).sum().shift(-cfg.horizon)
        f = fwd.to_frame("mkt")
    else:
        cols = cfg.factor_cols or list(factors.columns)
        f = factors[cols].rolling(cfg.horizon).sum().shift(-cfg.horizon)

    idx = x.index.intersection(y.index).intersection(f.index)
    return x.loc[idx], y.loc[idx], f.loc[idx]


def residualize(y: pd.Series, f: pd.DataFrame) -> pd.Series:
    """Regress returns on factors; keep only what the factors fail to explain."""
    d = pd.concat([y.rename("__y"), f], axis=1).dropna()
    if len(d) < f.shape[1] + 10:
        return pd.Series(dtype=float)
    X = np.column_stack([np.ones(len(d)), d[f.columns].to_numpy(float)])
    _, resid = ols(X, d["__y"].to_numpy(float))
    return pd.Series(resid, index=d.index)


# ----------------------------------------------------------------------------- tests


def circular_shift_p(x, y, observed, cfg: Config, rng) -> float:
    """
    Null: the two series are unrelated. Random circular shifts of x preserve its
    autocorrelation exactly while breaking alignment with y -- so persistence
    alone cannot manufacture significance.
    """
    T = len(x)
    buffer = max(cfg.horizon * 2, 5)
    lo, hi = buffer, T - buffer
    if hi <= lo:
        return float("nan")
    shifts = rng.integers(lo, hi, size=cfg.n_boot)
    hits = sum(abs(pearson(np.roll(x, int(s)), y)) >= abs(observed) for s in shifts)
    return (hits + 1) / (cfg.n_boot + 1)


def test_asset(x, y, f, cfg: Config, rng) -> dict | None:
    target = residualize(y, f) if cfg.residualize else y.dropna()
    d = pd.concat([x.rename("x"), target.rename("y")], axis=1).dropna()
    if len(d) < cfg.min_obs:
        return None

    xv, yv = d["x"].to_numpy(float), d["y"].to_numpy(float)
    n = len(d)
    X = np.column_stack([np.ones(n), xv])
    beta, resid = ols(X, yv)
    lag = nw_lag(n, cfg.horizon)
    se = hac_se(X, resid, lag)
    t = beta[1] / se[1] if se[1] > 0 else 0.0
    r = pearson(xv, yv)

    return {
        "n": n,
        "r": r,
        "beta": float(beta[1]),
        "t_hac": float(t),
        "p_hac": float(2 * stats.t.sf(abs(t), n - 2)),
        "nw_lag": lag,
        "p_boot": circular_shift_p(xv, yv, r, cfg, rng),
    }


# ----------------------------------------------------------------------------- study


def run_study(macro, prices, factors=None, cfg: Config | None = None) -> pd.DataFrame:
    cfg = cfg or Config()
    rng = np.random.default_rng(cfg.seed)
    x, y_all, f = prepare(macro, prices, factors, cfg)

    split = int(len(x) * (1 - cfg.holdout_frac))
    tr, ho = slice(0, split), slice(split, len(x))

    rows = []
    for ticker in y_all.columns:
        ins = test_asset(x.iloc[tr], y_all[ticker].iloc[tr], f.iloc[tr], cfg, rng)
        if ins is None:
            continue
        row = {"ticker": ticker, **{f"is_{k}": v for k, v in ins.items()}}
        oos = test_asset(x.iloc[ho], y_all[ticker].iloc[ho], f.iloc[ho], cfg, rng)
        row.update({f"oos_{k}": v for k, v in (oos or {}).items()})
        rows.append(row)

    res = pd.DataFrame(rows)
    if res.empty:
        return res

    # p_boot leads: it is the one that respects autocorrelation
    pv = res["is_p_boot"].fillna(res["is_p_hac"]).to_numpy(float)
    res["q_value"], res["passes_fdr"] = benjamini_hochberg(pv, cfg.fdr_alpha)

    if "oos_r" in res:
        res["oos_confirms"] = (
            res["oos_r"].notna()
            & (np.sign(res["oos_r"].fillna(0)) == np.sign(res["is_r"]))
            & (res["oos_p_hac"].fillna(1.0) < cfg.oos_alpha)
        )
    else:
        res["oos_confirms"] = False

    res["survives"] = res["passes_fdr"] & res["oos_confirms"]
    return res.sort_values("q_value").reset_index(drop=True)


def summarize(res: pd.DataFrame) -> str:
    if res.empty:
        return "No asset had enough overlapping observations to test."
    n_train = int(res["is_n"].median())
    cols = ["ticker", "is_r", "is_t_hac", "is_p_boot", "q_value", "oos_r", "survives"]
    return "\n".join([
        f"Universe tested:            {len(res)}",
        f"In-sample observations:     {n_train} (median)",
        f"Min detectable |r| @ p<.05: {min_detectable_r(n_train):.3f}"
        "   <- below this is noise",
        f"Passing FDR:                {int(res['passes_fdr'].sum())}",
        f"Also confirmed OOS:         {int(res['survives'].sum())}",
        "",
        res.head(10)[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"),
    ])


# ----------------------------------------------------------------------------- demo


def make_demo(n=180, n_assets=40, seed=7):
    """Synthetic panel with exactly ONE planted relationship (T00), to check the pipeline."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2010-01-31", periods=n, freq="ME")

    score = np.zeros(n)                       # persistent AR(1) macro level
    for t in range(1, n):
        score[t] = 0.95 * score[t - 1] + rng.normal(0, 1)
    macro = pd.Series(score, index=idx, name="score")
    dscore = np.diff(score, prepend=score[0])

    mkt = rng.normal(0.006, 0.045, n)
    data = {}
    for i in range(n_assets):
        r = rng.uniform(0.6, 1.5) * mkt + rng.normal(0, 0.05, n)
        if i == 0:                            # planted: next period loads on this Ds
            r += 0.020 * np.roll(dscore, 1)
        data[f"T{i:02d}"] = 100 * np.exp(np.cumsum(r))

    return macro, pd.DataFrame(data, index=idx)


# ----------------------------------------------------------------------------- cli


def main() -> None:
    ap = argparse.ArgumentParser(description="Macro score vs. equity return study.")
    ap.add_argument("--macro")
    ap.add_argument("--prices")
    ap.add_argument("--factors")
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--holdout", type=float, default=0.30)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--alpha", type=float, default=0.10)
    ap.add_argument("--no-residualize", action="store_true")
    ap.add_argument("--levels", action="store_true",
                    help="test levels rather than changes (rarely correct)")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default="macro_study_results.csv")
    a = ap.parse_args()

    cfg = Config(
        horizon=a.horizon,
        diff_score=not a.levels,
        residualize=not a.no_residualize,
        holdout_frac=a.holdout,
        n_boot=a.boot,
        fdr_alpha=a.alpha,
    )

    if a.demo:
        macro, prices, factors = *make_demo(), None
        print("[demo] 40 synthetic assets, 1 planted signal (T00), 180 months\n")
    else:
        if not (a.macro and a.prices):
            ap.error("--macro and --prices are required unless --demo")
        macro = load_csv(a.macro).iloc[:, 0]
        prices = load_csv(a.prices)
        factors = load_csv(a.factors) if a.factors else None

    res = run_study(macro, prices, factors, cfg)
    print(summarize(res))
    if not res.empty:
        res.to_csv(a.out, index=False)
        print(f"\nFull results -> {a.out}")


if __name__ == "__main__":
    main()
