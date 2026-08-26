#!/usr/bin/env python3
"""
Direct metric -> asset signals, bypassing the discrete regime-bucket
architecture entirely.

Why: the regime-threshold and regime-mapping work both hit real ceilings —
tuning either the input thresholds or the output direction table couldn't
meaningfully improve the backtest, which is more consistent with the
DISCRETIZATION ITSELF (forcing a continuous multi-dimensional picture into
one of 6 labels every month) losing information, than with either specific
piece being miscalibrated. Meanwhile the earlier per-metric calibration —
testing a metric's raw score directly against an asset's forward return,
no regime bucket in between — found real signal (Credit r=+0.60, Bank
Reserves momentum r=+0.41). This script generalizes that: for EVERY asset,
find which single metric (if any) has real, out-of-sample predictive
power for it — independently, with no shared classification step.

Methodology:
1. Fetch ~10 candidate metrics and all 22 assets' price history once.
2. Split chronologically into TRAIN (first 60%) and TEST (last 40%).
3. For each asset, in TRAIN only: grid-search all metrics x {level,
   momentum} x window x horizon, pick whichever single combination has
   the strongest |correlation|.
4. Re-check that SAME combination (not re-optimized) on TEST data alone.
   This is the honest number — if TRAIN and TEST correlations agree in
   sign and are both non-trivial, that's real evidence. If TEST is near
   zero or flips sign, TRAIN's finding didn't generalize and should be
   discarded, not trusted.

Known constraint: BAMLH0A0HYM2 and BAMLC0A0CM (HY/IG credit spreads) only
have ~3 years of history available via FRED as of an April 2026 policy
change limiting distribution to a rolling window. Their TRAIN-period
correlations will be computed on a shorter effective sample than the other
8 metrics — flagged per-result via `metric_obs`, not hidden.

Requires: pandas, numpy, yfinance.
"""

import json
import sys
from datetime import datetime, timezone

try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("This script needs pandas and numpy: pip install pandas numpy yfinance")

import backtest_asset_outlook as bt
from calibrate_sensitivity import (
    fetch_fred_full, rolling_percentile_score, rolling_momentum_percentile_score,
    WINDOWS, HORIZONS,
)

METRICS = ['BAMLH0A0HYM2', 'BAMLC0A0CM', 'DGS2', 'DGS10', 'DTWEXBGS',
           'WALCL', 'WRESBAL', 'WTREGEN', 'VIXCLS', 'NFCI']
MIN_TRAIN_OBS = 20  # below this, a correlation is too noisy to trust as a candidate


def is_significant(r, n):
    """Rough distinguishable-from-zero check: |r| > 2 standard errors,
    using the standard approximate SE of a correlation coefficient
    (1/sqrt(n-3)). Not a rigorous hypothesis test, but a real bar tied to
    sample size — a same-sign correlation on a tiny n is not "held up,"
    it's within the noise a true-zero relationship would produce just as
    often by chance."""
    if r is None or n is None or n < 15:
        return False
    se = 1 / np.sqrt(max(n - 3, 1))
    return abs(r) > 2 * se


def score_metric_asset(metric_series, price_series, window, horizon, method, date_range=None):
    """Correlates one metric's rolling score against one asset's forward
    return, optionally restricted to a date range (for train/test
    splitting). Returns (correlation, n) or (None, 0) if not enough
    overlapping data.

    Uses NON-OVERLAPPING forward-return windows (sampled every `horizon`
    days, not every day) — daily-sampled overlapping windows share nearly
    all of their forward-looking data with their neighbors (a 63-day
    window sampled daily overlaps 62/63 days with the next day's window),
    which makes the nominal row count wildly overstate the true
    independent sample size and can make noise look like a real,
    "held-up" signal. Sampling only every `horizon` days gives an honest,
    much smaller, but truthful n."""
    score_fn = rolling_percentile_score if method == 'level' else rolling_momentum_percentile_score
    score = score_fn(metric_series, window)
    score_df = score.dropna().rename('score').to_frame()
    score_df.index = score_df.index.astype('datetime64[ns]')
    px_df = price_series.rename('price').to_frame()
    px_df.index = px_df.index.astype('datetime64[ns]')
    merged = pd.merge_asof(px_df.sort_index(), score_df.sort_index(),
                            left_index=True, right_index=True, direction='backward').dropna()
    if date_range is not None:
        start, end = date_range
        merged = merged[(merged.index >= start) & (merged.index <= end)]
    if len(merged) < 30:
        return None, 0
    fwd_return = merged['price'].shift(-horizon) / merged['price'] - 1
    valid = pd.DataFrame({'score': merged['score'], 'fwd_return': fwd_return}).dropna()
    valid = valid.iloc[::horizon]  # non-overlapping: one observation per horizon-length window
    if len(valid) < 15:
        return None, 0
    return valid['score'].corr(valid['fwd_return']), len(valid)


def main():
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("This script needs yfinance: pip install pandas numpy yfinance")

    print('Fetching candidate metrics...', file=sys.stderr)
    S = {}
    for m in METRICS:
        S[m] = fetch_fred_full(m)
        print(f'  {m}: {len(S[m])} obs, {S[m].index.min().date() if len(S[m]) else "n/a"} to {S[m].index.max().date() if len(S[m]) else "n/a"}', file=sys.stderr)

    tickers = sorted(set(t for _, t, _ in bt.ASSET_TABLE))
    print(f'Fetching price history for {len(tickers)} assets (bulk)...', file=sys.stderr)
    prices = yf.download(tickers, start='2012-01-01', progress=False, auto_adjust=True)['Close']
    if isinstance(prices, pd.Series):
        prices = prices.to_frame()

    overall_start = min(s.index.min() for s in S.values() if len(s))
    overall_end = min(prices.index.max(), max(s.index.max() for s in S.values() if len(s)))
    full_range = pd.date_range(overall_start, overall_end, freq='D')
    split_point = full_range[int(len(full_range) * 0.6)]
    train_range = (overall_start, split_point)
    test_range = (split_point, overall_end)
    print(f'\nTrain range: {train_range[0].date()} to {train_range[1].date()}', file=sys.stderr)
    print(f'Test range:  {test_range[0].date()} to {test_range[1].date()} (held out)', file=sys.stderr)

    results = {}
    for name, ticker, _ in bt.ASSET_TABLE:
        if ticker not in prices.columns:
            continue
        price_series = prices[ticker].dropna()
        print(f'\n=== {name} ({ticker}) ===', file=sys.stderr)
        candidates = []
        for metric in METRICS:
            if len(S[metric]) < MIN_TRAIN_OBS:
                continue
            for method in ['level', 'momentum']:
                for window in WINDOWS:
                    for horizon in HORIZONS:
                        corr, n = score_metric_asset(S[metric], price_series, window, horizon, method, date_range=train_range)
                        if corr is not None and n >= MIN_TRAIN_OBS:
                            candidates.append({'metric': metric, 'method': method, 'window': window,
                                                'horizon': horizon, 'train_corr': round(corr, 4), 'train_n': n})
        if not candidates:
            print('  no viable candidates (insufficient train-period data)', file=sys.stderr)
            results[name] = {'ticker': ticker, 'status': 'insufficient_data'}
            continue

        best = max(candidates, key=lambda c: abs(c['train_corr']))
        test_corr, test_n = score_metric_asset(S[best['metric']], price_series, best['window'], best['horizon'], best['method'], date_range=test_range)

        held_up = (test_corr is not None and test_corr * best['train_corr'] > 0
                   and is_significant(best['train_corr'], best['train_n']) and is_significant(test_corr, test_n))
        results[name] = {
            'ticker': ticker, 'status': 'ok',
            'best_metric': best['metric'], 'method': best['method'], 'window': best['window'], 'horizon': best['horizon'],
            'train_corr': best['train_corr'], 'train_n': best['train_n'],
            'test_corr': round(test_corr, 4) if test_corr is not None else None, 'test_n': test_n,
            'held_up_out_of_sample': held_up,
        }
        status = 'HELD UP' if held_up else 'did not hold up'
        if test_corr is not None:
            test_corr_str = f'{test_corr:+.3f}'
            print(f"  best: {best['metric']} ({best['method']}, w={best['window']}, h={best['horizon']}) "
                  f"train_r={best['train_corr']:+.3f} (n={best['train_n']})  ->  test_r={test_corr_str} (n={test_n})  [{status}]", file=sys.stderr)
        else:
            print(f"  best: {best['metric']} — train_r={best['train_corr']:+.3f}, but no usable test-period data to confirm", file=sys.stderr)

    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'train_range': [str(train_range[0].date()), str(train_range[1].date())],
        'test_range': [str(test_range[0].date()), str(test_range[1].date())],
        'results': results,
    }
    with open('asset_signals.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print('\n\n=== SUMMARY: signals that HELD UP out-of-sample ===')
    held = [(name, r) for name, r in results.items() if r.get('held_up_out_of_sample')]
    if not held:
        print('None. No metric-asset pairing survived the train/test split for any asset.')
    for name, r in held:
        print(f"  {name:<28} <- {r['best_metric']:<14} ({r['method']:<9}) train_r={r['train_corr']:+.3f}  test_r={r['test_corr']:+.3f}")

    print('\n=== Everything else (found in train, did NOT hold up in test — discard these) ===')
    not_held = [(name, r) for name, r in results.items() if r.get('status') == 'ok' and not r.get('held_up_out_of_sample')]
    for name, r in not_held:
        tc = r['test_corr']
        print(f"  {name:<28} <- {r['best_metric']:<14} train_r={r['train_corr']:+.3f}  test_r={tc if tc is not None else 'n/a'}")


if __name__ == '__main__':
    main()
