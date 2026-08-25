#!/usr/bin/env python3
"""
Sensitivity calibration for the percentile-based stress scores.

Question this answers: refresh_model.py's percentile_score() currently
uses a fixed 500-trading-day (~2yr) lookback window for every metric. Is
that actually the window length that produces the strongest relationship
between the score and what happens next in the mapped asset's price — or
would a shorter/longer window do better?

Method
------
For each (FRED series, mapped asset) pair:
  1. Pull full daily history for the FRED series and the asset's price.
  2. For a grid of window lengths (60/120/250/500/750/1000 trading days)
     and forward-return horizons (21/63/126 trading days ~ 1/3/6 months):
       - Compute the percentile score at every historical date using ONLY
         that many trailing observations (same no-lookahead logic as
         refresh_model.py's percentile_score).
       - Compute the asset's actual forward return from that date.
       - Correlate the two.
  3. Report which (window, horizon) combination produced the strongest
     |correlation| for each metric, with the actual coefficient and
     sample size — so this is an empirical finding, not a guess.

This does NOT run in this sandboxed environment — it needs outbound
internet access to FRED and Yahoo Finance. Run it locally or via the
included one-off GitHub Actions workflow.

Requires: pandas, numpy, yfinance
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("This script needs pandas and numpy: pip install pandas numpy yfinance")

FRED_API_KEY = os.environ.get('FRED_API_KEY', '').strip()

# The pairs to calibrate — the two metrics we just moved to percentile
# scoring, tested against their most directly-related mapped asset (not
# every asset in the category, to keep the signal clean).
PAIRS = [
    {'metric': 'HY Credit Spread', 'series': 'BAMLH0A0HYM2', 'ticker': 'HYG',
     'note': 'High-yield spread vs. HY bond ETF — higher spread should precede weaker HYG returns'},
    {'metric': 'IG Credit Spread', 'series': 'BAMLC0A0CM', 'ticker': 'LQD',
     'note': 'Investment-grade spread vs. IG bond ETF'},
    {'metric': '10Y Treasury Yield', 'series': 'DGS10', 'ticker': 'TLT',
     'note': 'Long yield vs. long Treasury ETF — yields up should precede TLT down (inverse by construction)'},
    {'metric': '2Y Treasury Yield', 'series': 'DGS2', 'ticker': 'BIL',
     'note': 'Short yield vs. T-Bill ETF — expect a weak relationship, BIL barely moves either way'},
]

WINDOWS = [60, 120, 250, 500, 750, 1000]      # trading days to test as the percentile lookback
HORIZONS = [21, 63, 126]                       # forward-return horizons: ~1mo, ~3mo, ~6mo


def fetch_fred_full(series_id, cosd='2012-01-01'):
    url = f'https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&observation_start={cosd}' if FRED_API_KEY else \
          f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cosd}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode('utf-8', errors='replace')

    rows = []
    if FRED_API_KEY:
        data = json.loads(text)
        for obs in data.get('observations', []):
            v = obs.get('value')
            if v in (None, '.', ''):
                continue
            try:
                rows.append((pd.Timestamp(obs['date']), float(v)))
            except (ValueError, KeyError):
                continue
    else:
        for line in text.strip().splitlines()[1:]:
            parts = line.split(',')
            if len(parts) < 2:
                continue
            try:
                rows.append((pd.Timestamp(parts[0].strip()), float(parts[1].strip())))
            except ValueError:
                continue
    if not rows:
        return pd.Series(dtype=float)
    dates, vals = zip(*rows)
    return pd.Series(vals, index=pd.DatetimeIndex(dates)).sort_index()


def rolling_percentile_score(series, window):
    """Percentile rank of each value within its own trailing `window`
    observations (inclusive of itself) — same definition as
    refresh_model.py's percentile_score(), vectorized via pandas.rolling.
    Answers: "is this metric unusually ELEVATED right now, relative to its
    own recent history?" No lookahead: each point only ever looks
    backward."""
    def pct_of_last(x):
        if len(x) < 30:
            return np.nan
        current = x[-1]
        pool_sorted = np.sort(x)
        idx = np.searchsorted(pool_sorted, current, side='left')
        return idx / len(pool_sorted) * 100
    return series.rolling(window, min_periods=30).apply(pct_of_last, raw=True)


ROC_PERIOD = 20  # trading days over which "recent change" is measured for the momentum score


def rolling_momentum_percentile_score(series, window, roc_period=ROC_PERIOD):
    """Percentile rank of the metric's recent `roc_period`-day CHANGE
    within its own trailing `window` of such changes. Answers a different
    question than the level-based score above: not "is this elevated?"
    but "is this DETERIORATING UNUSUALLY FAST right now?" — a genuine
    momentum/continuation candidate, as opposed to the level score, which
    the first calibration run showed behaves like mean-reversion (positive
    correlation with forward returns — elevated levels tended to fall
    back, pushing the bond ETF price back up)."""
    roc = series.diff(roc_period)
    return rolling_percentile_score(roc, window)


def calibrate_pair(pair):
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("This script needs yfinance: pip install pandas numpy yfinance")

    print(f"\n=== {pair['metric']} vs {pair['ticker']} ===", file=sys.stderr)
    fred = fetch_fred_full(pair['series'])
    if fred.empty:
        print(f"  WARN: no FRED data for {pair['series']}", file=sys.stderr)
        return None
    print(f"  FRED series: {len(fred)} obs, {fred.index.min().date()} to {fred.index.max().date()}", file=sys.stderr)

    px = yf.download(pair['ticker'], start='2012-01-01', progress=False, auto_adjust=True)['Close']
    if isinstance(px, pd.DataFrame):
        px = px.iloc[:, 0]
    px = px.dropna()
    if px.empty:
        print(f"  WARN: no price data for {pair['ticker']}", file=sys.stderr)
        return None
    print(f"  Price series: {len(px)} obs, {px.index.min().date()} to {px.index.max().date()}", file=sys.stderr)

    results = []
    for method_name, score_fn in [('level', rolling_percentile_score), ('momentum', rolling_momentum_percentile_score)]:
        for window in WINDOWS:
            score = score_fn(fred, window)
            # align FRED's (possibly irregular / non-trading-day) dates onto the
            # asset's actual trading dates via forward-fill, same logic the
            # dashboard chart uses, so scores and prices line up on real dates
            score_df = score.dropna().rename('score').to_frame()
            score_df.index.name = 'date'
            score_df.index = score_df.index.astype('datetime64[ns]')  # FRED's API and yfinance
            px_df = px.rename('price').to_frame()                      # sometimes return indices with
            px_df.index.name = 'date'                                  # different internal datetime64
            px_df.index = px_df.index.astype('datetime64[ns]')         # precisions — normalize both
            merged = pd.merge_asof(px_df.sort_index(), score_df.sort_index(),
                                    left_index=True, right_index=True, direction='backward')
            merged = merged.dropna()
            if len(merged) < 100:
                continue

            for horizon in HORIZONS:
                fwd_return = merged['price'].shift(-horizon) / merged['price'] - 1
                valid = pd.DataFrame({'score': merged['score'], 'fwd_return': fwd_return}).dropna()
                if len(valid) < 100:
                    continue
                corr = valid['score'].corr(valid['fwd_return'])
                results.append({'method': method_name, 'window': window, 'horizon': horizon,
                                 'correlation': round(corr, 4), 'n': len(valid)})

    if not results:
        print("  WARN: not enough overlapping data to calibrate", file=sys.stderr)
        return None

    best = max(results, key=lambda r: abs(r['correlation']))
    best_level = max([r for r in results if r['method'] == 'level'], key=lambda r: abs(r['correlation']), default=None)
    best_momentum = max([r for r in results if r['method'] == 'momentum'], key=lambda r: abs(r['correlation']), default=None)
    print(f"  Best overall: method={best['method']}, window={best['window']}d, horizon={best['horizon']}d, "
          f"r={best['correlation']}, n={best['n']}", file=sys.stderr)
    if best_level:
        print(f"    best LEVEL:    window={best_level['window']}d horizon={best_level['horizon']}d r={best_level['correlation']}", file=sys.stderr)
    if best_momentum:
        print(f"    best MOMENTUM: window={best_momentum['window']}d horizon={best_momentum['horizon']}d r={best_momentum['correlation']}", file=sys.stderr)

    return {
        'metric': pair['metric'], 'ticker': pair['ticker'], 'note': pair['note'],
        'all_results': results, 'best': best, 'best_level': best_level, 'best_momentum': best_momentum,
    }


def main():
    print('Calibrating percentile-score sensitivity against real historical data...', file=sys.stderr)
    print(f'Testing both LEVEL (elevated-vs-history) and MOMENTUM ({ROC_PERIOD}d-change-vs-history) scoring methods.', file=sys.stderr)
    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'windows_tested': WINDOWS, 'horizons_tested_days': HORIZONS, 'roc_period_days': ROC_PERIOD,
        'pairs': [],
    }
    for pair in PAIRS:
        result = calibrate_pair(pair)
        if result:
            output['pairs'].append(result)

    with open('sensitivity_calibration.json', 'w') as f:
        json.dump(output, f, indent=2)

    print('\n=== SUMMARY ===')
    print(f"{'Metric':<22} {'Method':<10} {'Window':>7} {'Horizon':>8} {'r':>8} {'n':>7}")
    for p in output['pairs']:
        for label, b in [('level', p['best_level']), ('momentum', p['best_momentum'])]:
            if b:
                print(f"{p['metric']:<22} {label:<10} {b['window']:>6}d {b['horizon']:>7}d {b['correlation']:>+8.3f} {b['n']:>7}")
    print('\nWhat to look for: LEVEL correlations came back POSITIVE in the first run (elevated')
    print('readings tended to mean-revert, pushing the bond price back UP). A NEGATIVE MOMENTUM')
    print('correlation would mean something meaningfully different: rapid deterioration right now')
    print('tends to precede further price weakness — a genuine continuation/foretelling signal,')
    print('not a reversion one. Compare the sign and strength of level vs momentum for each metric')
    print('before deciding which (if either, or both) belongs in the live scoring.')
    print('\nFull grid saved to sensitivity_calibration.json')
    print('\nNote on interpreting these numbers: a correlation around ±0.1-0.2 is a real, usable')
    print('but modest signal for financial data — this is normal, not a failure. Anything above')
    print('~0.3 in absolute value on n>500 is a comparatively strong result for this kind of macro')
    print('signal. Compare correlation strength ACROSS windows for the same metric to find where')
    print('the signal peaks, more than treating any single number as definitively "good" or "bad."')


if __name__ == '__main__':
    main()#!/usr/bin/env python3
"""
Sensitivity calibration for the percentile-based stress scores.

Question this answers: refresh_model.py's percentile_score() currently
uses a fixed 500-trading-day (~2yr) lookback window for every metric. Is
that actually the window length that produces the strongest relationship
between the score and what happens next in the mapped asset's price — or
would a shorter/longer window do better?

Method
------
For each (FRED series, mapped asset) pair:
  1. Pull full daily history for the FRED series and the asset's price.
  2. For a grid of window lengths (60/120/250/500/750/1000 trading days)
     and forward-return horizons (21/63/126 trading days ~ 1/3/6 months):
       - Compute the percentile score at every historical date using ONLY
         that many trailing observations (same no-lookahead logic as
         refresh_model.py's percentile_score).
       - Compute the asset's actual forward return from that date.
       - Correlate the two.
  3. Report which (window, horizon) combination produced the strongest
     |correlation| for each metric, with the actual coefficient and
     sample size — so this is an empirical finding, not a guess.

This does NOT run in this sandboxed environment — it needs outbound
internet access to FRED and Yahoo Finance. Run it locally or via the
included one-off GitHub Actions workflow.

Requires: pandas, numpy, yfinance
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("This script needs pandas and numpy: pip install pandas numpy yfinance")

FRED_API_KEY = os.environ.get('FRED_API_KEY', '').strip()

# The pairs to calibrate — the two metrics we just moved to percentile
# scoring, tested against their most directly-related mapped asset (not
# every asset in the category, to keep the signal clean).
PAIRS = [
    {'metric': 'HY Credit Spread', 'series': 'BAMLH0A0HYM2', 'ticker': 'HYG',
     'note': 'High-yield spread vs. HY bond ETF — higher spread should precede weaker HYG returns'},
    {'metric': 'IG Credit Spread', 'series': 'BAMLC0A0CM', 'ticker': 'LQD',
     'note': 'Investment-grade spread vs. IG bond ETF'},
    {'metric': '10Y Treasury Yield', 'series': 'DGS10', 'ticker': 'TLT',
     'note': 'Long yield vs. long Treasury ETF — yields up should precede TLT down (inverse by construction)'},
    {'metric': '2Y Treasury Yield', 'series': 'DGS2', 'ticker': 'BIL',
     'note': 'Short yield vs. T-Bill ETF — expect a weak relationship, BIL barely moves either way'},
]

WINDOWS = [60, 120, 250, 500, 750, 1000]      # trading days to test as the percentile lookback
HORIZONS = [21, 63, 126]                       # forward-return horizons: ~1mo, ~3mo, ~6mo


def fetch_fred_full(series_id, cosd='2012-01-01'):
    url = f'https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&observation_start={cosd}' if FRED_API_KEY else \
          f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cosd}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode('utf-8', errors='replace')

    rows = []
    if FRED_API_KEY:
        data = json.loads(text)
        for obs in data.get('observations', []):
            v = obs.get('value')
            if v in (None, '.', ''):
                continue
            try:
                rows.append((pd.Timestamp(obs['date']), float(v)))
            except (ValueError, KeyError):
                continue
    else:
        for line in text.strip().splitlines()[1:]:
            parts = line.split(',')
            if len(parts) < 2:
                continue
            try:
                rows.append((pd.Timestamp(parts[0].strip()), float(parts[1].strip())))
            except ValueError:
                continue
    if not rows:
        return pd.Series(dtype=float)
    dates, vals = zip(*rows)
    return pd.Series(vals, index=pd.DatetimeIndex(dates)).sort_index()


def rolling_percentile_score(series, window):
    """Percentile rank of each value within its own trailing `window`
    observations (inclusive of itself) — same definition as
    refresh_model.py's percentile_score(), vectorized via pandas.rolling.
    No lookahead: each point only ever looks backward."""
    def pct_of_last(x):
        if len(x) < 30:
            return np.nan
        current = x[-1]
        pool_sorted = np.sort(x)
        idx = np.searchsorted(pool_sorted, current, side='left')
        return idx / len(pool_sorted) * 100
    return series.rolling(window, min_periods=30).apply(pct_of_last, raw=True)


def calibrate_pair(pair):
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("This script needs yfinance: pip install pandas numpy yfinance")

    print(f"\n=== {pair['metric']} vs {pair['ticker']} ===", file=sys.stderr)
    fred = fetch_fred_full(pair['series'])
    if fred.empty:
        print(f"  WARN: no FRED data for {pair['series']}", file=sys.stderr)
        return None
    print(f"  FRED series: {len(fred)} obs, {fred.index.min().date()} to {fred.index.max().date()}", file=sys.stderr)

    px = yf.download(pair['ticker'], start='2012-01-01', progress=False, auto_adjust=True)['Close']
    if isinstance(px, pd.DataFrame):
        px = px.iloc[:, 0]
    px = px.dropna()
    if px.empty:
        print(f"  WARN: no price data for {pair['ticker']}", file=sys.stderr)
        return None
    print(f"  Price series: {len(px)} obs, {px.index.min().date()} to {px.index.max().date()}", file=sys.stderr)

    results = []
    for window in WINDOWS:
        score = rolling_percentile_score(fred, window)
        # align FRED's (possibly irregular / non-trading-day) dates onto the
        # asset's actual trading dates via forward-fill, same logic the
        # dashboard chart uses, so scores and prices line up on real dates
        score_df = score.dropna().rename('score').to_frame()
        score_df.index.name = 'date'
        score_df.index = score_df.index.astype('datetime64[ns]')  # FRED's API and yfinance
        px_df = px.rename('price').to_frame()                      # sometimes return indices with
        px_df.index.name = 'date'                                  # different internal datetime64
        px_df.index = px_df.index.astype('datetime64[ns]')         # precisions — normalize both
        merged = pd.merge_asof(px_df.sort_index(), score_df.sort_index(),
                                left_index=True, right_index=True, direction='backward')
        merged = merged.dropna()
        if len(merged) < 100:
            continue

        for horizon in HORIZONS:
            fwd_return = merged['price'].shift(-horizon) / merged['price'] - 1
            valid = pd.DataFrame({'score': merged['score'], 'fwd_return': fwd_return}).dropna()
            if len(valid) < 100:
                continue
            corr = valid['score'].corr(valid['fwd_return'])
            results.append({'window': window, 'horizon': horizon, 'correlation': round(corr, 4), 'n': len(valid)})

    if not results:
        print("  WARN: not enough overlapping data to calibrate", file=sys.stderr)
        return None

    best = max(results, key=lambda r: abs(r['correlation']))
    print(f"  Best: window={best['window']}d, horizon={best['horizon']}d, "
          f"r={best['correlation']}, n={best['n']}", file=sys.stderr)

    return {
        'metric': pair['metric'], 'ticker': pair['ticker'], 'note': pair['note'],
        'all_results': results, 'best': best,
    }


def main():
    print('Calibrating percentile-score sensitivity against real historical data...', file=sys.stderr)
    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'windows_tested': WINDOWS, 'horizons_tested_days': HORIZONS,
        'pairs': [],
    }
    for pair in PAIRS:
        result = calibrate_pair(pair)
        if result:
            output['pairs'].append(result)

    with open('sensitivity_calibration.json', 'w') as f:
        json.dump(output, f, indent=2)

    print('\n=== SUMMARY ===')
    for p in output['pairs']:
        b = p['best']
        print(f"{p['metric']:<22} best window={b['window']:>4}d  horizon={b['horizon']:>3}d  "
              f"r={b['correlation']:+.3f}  (n={b['n']})")
    print('\nFull grid saved to sensitivity_calibration.json')
    print('\nNote on interpreting these numbers: a correlation around ±0.1-0.2 is a real, usable')
    print('but modest signal for financial data — this is normal, not a failure. Anything above')
    print('~0.3 in absolute value on n>500 is a comparatively strong result for this kind of macro')
    print('signal. Compare correlation strength ACROSS windows for the same metric to find where')
    print('the signal peaks, more than treating any single number as definitively "good" or "bad."')


if __name__ == '__main__':
    main()
