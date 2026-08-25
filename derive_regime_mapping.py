#!/usr/bin/env python3
"""
Empirically derive the regime -> asset-direction mapping, replacing the
original workbook's qualitative judgment calls with real historical
outcomes.

Why this exists: the earlier regime-threshold recalibration found that
tuning the INPUT thresholds couldn't meaningfully improve the backtest hit
rate, because the OUTPUT mapping — which asset is "favored" in which
regime — was never derived from data in the first place. It's a
transcription of the original spreadsheet author's judgment. No amount of
threshold-tuning fixes a downstream mapping that was never validated.

Methodology — this matters, read before trusting the output:
1. Classify every historical month-end into a regime using
   classify_regime_asof() with the DEFAULT (not threshold-tuned) cutoffs,
   since the tuned ones were found to collapse the classifier into mostly
   one regime.
2. Split chronologically into a TRAIN period (earlier ~60%) and a TEST
   period (later ~40%, held out).
3. For each (regime, asset) pair, derive an empirical direction label
   using ONLY the TRAIN period's actual forward 3-month returns.
4. Score the resulting empirical table's hit rate on the TEST period only
   — data it never saw — and compare against the ORIGINAL hand-typed
   table's hit rate on that SAME held-out period. This is the fair,
   honest comparison: does data-derived beat judgment-derived on data
   neither has been fit to.

If step 4 were run on the same data used in step 3, the empirical table
would trivially look near-perfect by construction — that would be
circular, not a real finding. The train/test split exists specifically to
prevent that.

Real caveat: splitting ~100 month-ends into train/test leaves each half
thin, and some regimes (e.g. Deflationary/Funding Crisis) may simply not
have occurred often enough in the train period to derive a confident
label — those cells are marked INSUFFICIENT DATA rather than guessed at.

Requires: pandas, yfinance, and backtest_asset_outlook.py in the same
directory.
"""

import json
import sys
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas: pip install pandas yfinance")

import backtest_asset_outlook as bt

REGIME_NAMES = ['Liquidity Expansion', 'Neutral / Balanced', 'Inflationary Tightening',
                'Funding / Credit Stress', 'Deflationary / Funding Crisis']
MIN_SAMPLES = 4  # fewer than this and a direction label isn't trustworthy


def derive_direction(returns):
    """Given a list of forward returns for one (regime, asset) pair from
    the TRAINING period only, derive an empirical direction label."""
    n = len(returns)
    if n < MIN_SAMPLES:
        return 'INSUFFICIENT DATA', n, None, None
    frac_pos = sum(1 for r in returns if r > 0) / n
    mean_ret = sum(returns) / n
    if frac_pos >= 0.70:
        direction = 'UP STRONG' if mean_ret > 0.05 else 'UP'
    elif frac_pos <= 0.30:
        direction = 'DOWN STRONG' if mean_ret < -0.05 else 'DOWN'
    else:
        direction = 'MIXED'
    return direction, n, round(mean_ret, 4), round(frac_pos, 3)


def collect_regime_returns(S, prices, dates):
    """For every (month-end, asset), classify the regime (default
    thresholds) and compute the actual forward 3-month return. Returns a
    dict: {regime: {asset_name: [returns...]}}."""
    out = {r: {name: [] for name, _, _ in bt.ASSET_TABLE} for r in REGIME_NAMES}
    for d in dates:
        regime = bt.classify_regime_asof(S, d)  # default thresholds
        if regime is None:
            continue
        regime_key = 'Funding / Credit Stress' if regime == 'General Tightening' else regime
        if regime_key not in out:
            continue
        for name, ticker, _ in bt.ASSET_TABLE:
            if ticker not in prices.columns:
                continue
            price_series = prices[ticker].dropna()
            p0 = bt.asof(price_series, d, 0)
            future_window = price_series.loc[d + pd.Timedelta(days=80): d + pd.Timedelta(days=100)]
            if p0 is None or future_window.empty:
                continue
            fwd_return = (future_window.iloc[0] / p0) - 1
            out[regime_key][name].append(fwd_return)
    return out


def score_table_on_dates(S, prices, dates, direction_lookup):
    """Scores a (regime, asset) -> direction lookup against real forward
    returns on the given dates. `direction_lookup` is
    {regime: {asset_name: direction_string}}."""
    hits = total = 0
    for d in dates:
        regime = bt.classify_regime_asof(S, d)
        if regime is None:
            continue
        regime_key = 'Funding / Credit Stress' if regime == 'General Tightening' else regime
        if regime_key not in direction_lookup:
            continue
        for name, ticker, _ in bt.ASSET_TABLE:
            direction = direction_lookup[regime_key].get(name)
            rank = bt.DIRECTION_RANK.get(direction, 0)
            if rank == 0:
                continue
            if ticker not in prices.columns:
                continue
            price_series = prices[ticker].dropna()
            p0 = bt.asof(price_series, d, 0)
            future_window = price_series.loc[d + pd.Timedelta(days=80): d + pd.Timedelta(days=100)]
            if p0 is None or future_window.empty:
                continue
            fwd_return = (future_window.iloc[0] / p0) - 1
            hit = (rank > 0 and fwd_return > 0) or (rank < 0 and fwd_return < 0)
            total += 1
            hits += int(hit)
    return hits, total, (round(hits / total, 4) if total else None)


def original_table_lookup():
    """Adapts the original hand-typed ASSET_TABLE into the same
    {regime: {asset: direction}} shape as the empirically-derived one, so
    both can be scored with the identical function."""
    out = {r: {} for r in REGIME_NAMES}
    for name, ticker, directions in bt.ASSET_TABLE:
        for regime_name, col in [('Liquidity Expansion', 0), ('Neutral / Balanced', 1),
                                  ('Inflationary Tightening', 2), ('Funding / Credit Stress', 3),
                                  ('Deflationary / Funding Crisis', 4)]:
            out[regime_name][name] = directions[col]
    return out


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

    split_idx = int(len(usable_dates) * 0.6)
    train_dates, test_dates = usable_dates[:split_idx], usable_dates[split_idx:]
    print(f'Train: {len(train_dates)} dates ({train_dates[0].date()} to {train_dates[-1].date()})', file=sys.stderr)
    print(f'Test:  {len(test_dates)} dates ({test_dates[0].date()} to {test_dates[-1].date()}) — held out, never used to derive the table', file=sys.stderr)

    print('\nDeriving empirical direction table from TRAIN period only...', file=sys.stderr)
    train_returns = collect_regime_returns(S, prices, train_dates)
    empirical_table = {}
    empirical_detail = {}
    for regime in REGIME_NAMES:
        empirical_table[regime] = {}
        empirical_detail[regime] = {}
        for name, _, _ in bt.ASSET_TABLE:
            direction, n, mean_ret, frac_pos = derive_direction(train_returns[regime][name])
            empirical_table[regime][name] = direction
            empirical_detail[regime][name] = {'direction': direction, 'n': n, 'mean_return': mean_ret, 'frac_positive': frac_pos}

    print('Scoring empirically-derived table on held-out TEST period...', file=sys.stderr)
    emp_hits, emp_total, emp_rate = score_table_on_dates(S, prices, test_dates, empirical_table)

    print('Scoring ORIGINAL hand-typed table on the SAME held-out TEST period (fair comparison)...', file=sys.stderr)
    orig_hits, orig_total, orig_rate = score_table_on_dates(S, prices, test_dates, original_table_lookup())

    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'train_period': {'start': str(train_dates[0].date()), 'end': str(train_dates[-1].date()), 'n_dates': len(train_dates)},
        'test_period': {'start': str(test_dates[0].date()), 'end': str(test_dates[-1].date()), 'n_dates': len(test_dates)},
        'empirical_table': empirical_table,
        'empirical_detail': empirical_detail,
        'out_of_sample_comparison': {
            'empirical_derived': {'hits': emp_hits, 'total': emp_total, 'hit_rate': emp_rate},
            'original_handtyped': {'hits': orig_hits, 'total': orig_total, 'hit_rate': orig_rate},
        },
    }
    with open('empirical_regime_mapping.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print()
    print('=== OUT-OF-SAMPLE RESULT (test period only, never seen during derivation) ===')
    print(f"Empirically-derived table: {emp_rate} ({emp_hits}/{emp_total})")
    print(f"Original hand-typed table: {orig_rate} ({orig_hits}/{orig_total})")
    print()
    print('=== Empirically-derived table (from TRAIN period) ===')
    for regime in REGIME_NAMES:
        print(f'\n{regime}:')
        for name, _, _ in bt.ASSET_TABLE:
            d = empirical_detail[regime][name]
            if d['direction'] == 'INSUFFICIENT DATA':
                print(f"  {name:<28} INSUFFICIENT DATA (n={d['n']})")
            else:
                print(f"  {name:<28} {d['direction']:<12} n={d['n']:<3} mean_ret={d['mean_return']:+.3f} frac_pos={d['frac_positive']}")


if __name__ == '__main__':
    main()
