#!/usr/bin/env python3
"""
Regime-threshold recalibration.

Why this exists: switching Credit and Rates to percentile-based scoring
changed their statistical distribution from "pinned near a floor/ceiling
most of the time" (the original bug-formula behavior) to "uniform across
0-100 by construction." The regime-classification rules in
classify_regime_asof() still use the ORIGINAL fixed cutoffs (60, 55, etc.)
that were implicitly tuned against the old, non-uniform distribution — and
a full backtest run showed this now makes things WORSE (44.8% hit rate vs.
52% for the original buggy workbook), which is consistent with that
mismatch.

Scope, deliberately narrow: only the 4 thresholds that directly gate on
Credit (B6) or Rates (B7) — the two inputs whose distribution actually
changed — are searched. Everything else (Liquidity/B5 cutoffs, FX/K10
cutoffs, the Inflationary Pressure/K8 cutoff) is held at its original
value. This is a real, deliberate scope limit: with ~100 historical
month-end samples, grid-searching all ~9-11 threshold constants jointly
would very likely overfit — finding a combination that looks great on this
exact history and means nothing going forward. Four parameters is already
pushing it; treat the result as a real empirical finding worth trying,
not a guarantee.

Requires: pandas, yfinance, and backtest_asset_outlook.py in the same
directory (imports and reuses its fetch/scoring machinery rather than
duplicating it).
"""

import itertools
import json
import sys
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas: pip install pandas yfinance")

import backtest_asset_outlook as bt

# Candidate values for each threshold. Percentile scores are uniform 0-100
# by construction, so a cutoff of X roughly means "the most stressed
# (100-X)% of historical readings" — these candidates span from a loose
# cutoff (50, fires often) to a strict one (90, fires rarely).
CANDIDATES = {
    'defl_b6': [50, 60, 70, 80, 90],
    'defl_b7': [30, 40, 50, 60, 70],   # this one is a LOW-side cutoff ("B7 < X"), not high-side
    'infl_b7': [50, 60, 70, 80, 90],
    'fund_b6': [50, 60, 70, 80, 90],
}


def score_thresholds(S, prices, usable_dates, thresholds):
    """Reuses backtest_asset_outlook's exact scoring methodology (walk-
    forward regime reconstruction -> compare to real forward 3-month
    returns) for one candidate threshold combination."""
    hits = total = 0
    for d in usable_dates:
        regime = bt.classify_regime_asof(S, d, thresholds=thresholds)
        if regime is None:
            continue
        col = bt.REGIME_COL_INDEX.get(regime)
        if col is None:
            continue
        for name, ticker, directions in bt.ASSET_TABLE:
            if ticker not in prices.columns:
                continue
            price_series = prices[ticker].dropna()
            p0 = bt.asof(price_series, d, 0)
            future_window = price_series.loc[d + pd.Timedelta(days=80): d + pd.Timedelta(days=100)]
            if p0 is None or future_window.empty:
                continue
            fwd_return = (future_window.iloc[0] / p0) - 1
            direction = directions[col]
            rank = bt.DIRECTION_RANK.get(direction, 0)
            if rank == 0:
                continue
            hit = (rank > 0 and fwd_return > 0) or (rank < 0 and fwd_return < 0)
            total += 1
            hits += int(hit)
    return hits, total, (round(hits / total, 4) if total else None)


def main():
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("This script needs yfinance: pip install pandas yfinance")

    print('Fetching FRED history (2015-present)...', file=sys.stderr)
    S = {}
    for sid in bt.FRED_SERIES:
        try:
            S[sid] = bt.fetch_fred_full(sid)
            print(f'  {sid}: {len(S[sid])} obs', file=sys.stderr)
        except Exception as e:
            print(f'  {sid}: FAILED ({e})', file=sys.stderr)
            S[sid] = pd.Series(dtype=float)

    tickers = [t for _, t, _ in bt.ASSET_TABLE]
    print(f'Fetching price history for {len(tickers)} tickers via yfinance...', file=sys.stderr)
    prices = yf.download(tickers, start='2017-06-01', progress=False, auto_adjust=True)['Close']
    if isinstance(prices, pd.Series):
        prices = prices.to_frame()

    month_ends = pd.date_range('2018-01-31', datetime.now(timezone.utc).date().isoformat(), freq='ME')
    usable_dates = [d for d in month_ends if d + pd.Timedelta(days=100) <= prices.index.max()]
    print(f'{len(usable_dates)} usable historical month-ends', file=sys.stderr)

    keys = list(CANDIDATES.keys())
    combos = list(itertools.product(*[CANDIDATES[k] for k in keys]))
    print(f'Testing {len(combos)} threshold combinations...', file=sys.stderr)

    results = []
    for combo in combos:
        thresholds = dict(zip(keys, combo))
        hits, total, rate = score_thresholds(S, prices, usable_dates, thresholds)
        results.append({'thresholds': thresholds, 'hits': hits, 'total': total, 'hit_rate': rate})

    # baseline: the original, un-recalibrated thresholds, for direct comparison
    base_hits, base_total, base_rate = score_thresholds(S, prices, usable_dates, bt.DEFAULT_REGIME_THRESHOLDS)

    scored = [r for r in results if r['hit_rate'] is not None and r['total'] >= 50]
    best = max(scored, key=lambda r: r['hit_rate']) if scored else None

    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'baseline': {'thresholds': bt.DEFAULT_REGIME_THRESHOLDS, 'hits': base_hits, 'total': base_total, 'hit_rate': base_rate},
        'best': best,
        'all_results': sorted(results, key=lambda r: (r['hit_rate'] or -1), reverse=True)[:20],  # top 20 only, full grid can be large
    }
    with open('regime_threshold_calibration.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print()
    print('=== SUMMARY ===')
    print(f"Baseline (original thresholds): {base_rate} ({base_hits}/{base_total})")
    if best:
        print(f"Best found:                     {best['hit_rate']} ({best['hits']}/{best['total']})")
        print(f"  thresholds: {best['thresholds']}")
    else:
        print("No combination produced enough scored calls (n>=50) to trust.")
    print()
    print('Top 10 combinations by hit rate (min n=50):')
    for r in sorted(scored, key=lambda r: -r['hit_rate'])[:10]:
        print(f"  {r['hit_rate']:.3f}  (n={r['total']:>3})  {r['thresholds']}")
    print()
    print('Caution: this searched 4 parameters against ~100 historical month-ends.')
    print('Treat the winner as a real lead worth trying, not a guarantee — re-check')
    print('against fresh data over time rather than assuming this is final.')


if __name__ == '__main__':
    main()
