#!/usr/bin/env python3
"""
Backtest: does the Asset-Class Outlook table's regime -> direction mapping
actually predict anything?

Method
------
1. Pull full daily history (back to 2015, for lookback buffer) for the ~26
   FRED series the live model uses.
2. Pull full daily price history for the 22 tickers in the outlook table via
   yfinance.
3. For each month-end date from 2018-01 to the most recent month with at
   least 3 months of forward data: reconstruct what the live model's
   Detected Regime would have been AS OF that date (using only data
   available up to that date — no lookahead), using the same formulas as
   refresh_model.py.
4. For each asset, compute the actual forward 3-month return from that date.
5. Compare: did the outlook table's predicted direction for that
   (regime, asset) pair match the sign of the actual forward return?
   "MIXED" carries no directional claim and is excluded from scoring.
6. Aggregate hit rate per asset, per regime, and overall — with sample
   sizes shown, because a hit rate on n=3 means nothing.

This does NOT run in a sandboxed/offline environment — it needs outbound
internet access to Stooq^H^H FRED and Yahoo Finance. Run it locally or as a
one-off GitHub Actions job (see README).

Requires: pandas, yfinance  (pip install pandas yfinance)
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

FRED_API_KEY = os.environ.get('FRED_API_KEY', '').strip()

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas: pip install pandas yfinance")


FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'PAYEMS', 'CPIAUCSL', 'PCEPI', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'T10Y2Y', 'DTWEXBGS', 'WALCL', 'WRESBAL', 'WTREGEN',
    'NFCI', 'EFFR', 'SOFR25', 'SOFR75', 'DEXJPUS', 'DEXUSEU',
    'DEXCHUS', 'DEXSZUS', 'DEXUSAL',
]

# (outlook-table name, yfinance ticker, [expansion, neutral, inflationary_tightening, funding_credit_stress, deflationary_crisis])
ASSET_TABLE = [
    ('S&P 500', 'SPY', ['UP', 'MIXED', 'DOWN', 'DOWN', 'DOWN']),
    ('Nasdaq / Growth', 'QQQ', ['UP', 'MIXED', 'DOWN STRONG', 'DOWN', 'DOWN']),
    ('Small Caps', 'IWM', ['UP STRONG', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN STRONG']),
    ('Value Stocks', 'VTV', ['UP', 'MIXED', 'MIXED / DOWN', 'DOWN', 'DOWN']),
    ('High Dividend Stocks', 'VYM', ['UP', 'MIXED', 'MIXED', 'DOWN', 'DOWN']),
    ('High-Yield Bonds', 'HYG', ['UP', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN STRONG']),
    ('Investment-Grade Bonds', 'LQD', ['UP', 'MIXED', 'DOWN', 'DOWN', 'UP / MIXED']),
    ('Short Treasuries / T-Bills', 'BIL', ['MIXED / UP', 'UP', 'UP', 'UP', 'UP']),
    ('Long Treasuries', 'TLT', ['UP', 'MIXED', 'DOWN STRONG', 'MIXED / UP', 'UP STRONG']),
    ('U.S. Dollar', 'UUP', ['DOWN / MIXED', 'MIXED', 'UP', 'UP', 'UP initially']),
    ('Gold', 'GLD', ['UP', 'MIXED', 'MIXED', 'MIXED / UP', 'UP after liquidation']),
    ('Silver', 'SLV', ['UP', 'MIXED', 'MIXED / DOWN', 'DOWN / MIXED', 'MIXED']),
    ('Broad Commodities', 'DBC', ['UP', 'MIXED', 'UP / MIXED', 'DOWN', 'DOWN']),
    ('Oil', 'USO', ['UP', 'MIXED', 'UP / MIXED', 'DOWN', 'DOWN']),
    ('REITs', 'VNQ', ['UP', 'MIXED', 'DOWN', 'DOWN', 'MIXED / UP']),
    ('Utilities', 'XLU', ['UP', 'MIXED', 'DOWN / MIXED', 'MIXED', 'UP']),
    ('Consumer Staples', 'XLP', ['UP', 'MIXED', 'MIXED', 'RELATIVE UP', 'RELATIVE UP']),
    ('Financials', 'XLF', ['UP', 'MIXED', 'MIXED', 'DOWN STRONG', 'DOWN']),
    ('Bitcoin', 'BTC-USD', ['UP STRONG', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN initially']),
    ('Crypto ex-BTC', 'ETH-USD', ['UP STRONG', 'MIXED', 'DOWN STRONG', 'DOWN STRONG', 'DOWN STRONG']),
    ('Emerging-Market Stocks', 'EEM', ['UP', 'MIXED', 'DOWN', 'DOWN', 'DOWN']),
    ('Emerging-Market Bonds', 'EMB', ['UP', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN']),
]

DIRECTION_RANK = {
    'UP STRONG': 2, 'UP': 1, 'UP / MIXED': 0.5, 'MIXED / UP': 0.5,
    'UP after liquidation': 0.5, 'UP initially': 0.5, 'RELATIVE UP': 0.5,
    'MIXED': 0, 'MIXED / DOWN': -0.5, 'DOWN / MIXED': -0.5, 'DOWN initially': -0.5,
    'DOWN': -1, 'DOWN STRONG': -2,
}
REGIME_COLS = ['Liquidity Expansion', 'Neutral / Balanced', 'Inflationary Tightening',
               'Funding / Credit Stress', 'Deflationary / Funding Crisis']
# "General Tightening" shares a column with "Funding / Credit Stress" per the
# workbook's own IF() logic.
REGIME_COL_INDEX = {
    'Liquidity Expansion': 0, 'Neutral / Balanced': 1, 'Inflationary Tightening': 2,
    'Funding / Credit Stress': 3, 'General Tightening': 3, 'Deflationary / Funding Crisis': 4,
}


def clamp(x, lo, hi):
    if x is None or pd.isna(x):
        return None
    return min(hi, max(lo, x))


def fetch_fred_full(series_id, cosd='2015-01-01'):
    """Uses FRED's official JSON API when FRED_API_KEY is set (same
    reliability rationale as refresh_model.py); falls back to the public
    CSV export endpoint otherwise."""
    if FRED_API_KEY:
        url = (f'https://api.stlouisfed.org/fred/series/observations'
               f'?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json'
               f'&observation_start={cosd}')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        rows = []
        for obs in data.get('observations', []):
            v = obs.get('value')
            if v in (None, '.', ''):
                continue
            try:
                rows.append((pd.Timestamp(obs['date']), float(v)))
            except (ValueError, KeyError):
                continue
        if not rows:
            return pd.Series(dtype=float)
        dates, vals = zip(*rows)
        return pd.Series(vals, index=pd.DatetimeIndex(dates)).sort_index()

    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cosd}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode('utf-8', errors='replace')
    rows = []
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


def asof(series, date, back=0):
    """Value `back` valid observations before (and including, if back=0) `date`."""
    if series is None or series.empty:
        return None
    window = series.loc[:date]
    if len(window) <= back:
        return None
    return window.iloc[-1 - back]


def hy_score(d):
    if d is None:
        return None
    if d <= 250: return 10
    if d <= 300: return 10 + (d - 250) * 0.2
    if d <= 350: return 20 + (d - 300) * 0.3
    if d <= 400: return 35 + (d - 350) * 0.3
    if d <= 500: return 50 + (d - 400) * 0.15
    if d <= 700: return 65 + (d - 500) * 0.075
    if d <= 1000: return 80 + (d - 700) * 0.0666667
    return 100


def classify_regime_asof(S, date):
    """Reconstruct the Detected Regime as of `date` using only data up to
    that date. Mirrors refresh_model.py's compute_model() math (post-fix
    weighting), condensed to just what's needed for the regime label."""
    L = lambda k: asof(S.get(k), date, 0)
    P1 = lambda k: asof(S.get(k), date, 1)
    P5 = lambda k: asof(S.get(k), date, 5)
    P20 = lambda k: asof(S.get(k), date, 20)

    hy, ig = L('BAMLH0A0HYM2'), L('BAMLC0A0CM')
    hyScore, igScore = hy_score(hy), (clamp((ig - 60) / 2.4, 0, 100) if ig is not None else None)

    sofr, iorb, effr, s25, s75 = L('SOFR'), L('IORB'), L('EFFR'), L('SOFR25'), L('SOFR75')
    sofrIorbBps = (sofr - iorb) * 100 if None not in (sofr, iorb) else None
    sofrEffrBps = (sofr - effr) * 100 if None not in (sofr, effr) else None
    sofrIqrBps = (s75 - s25) * 100 if None not in (s75, s25) else None
    repoScore = None
    if None not in (sofrIorbBps, sofrEffrBps, sofrIqrBps):
        repoScore = clamp(0.45*clamp(20+sofrIorbBps*3,0,100)+0.3*clamp(20+sofrEffrBps*4,0,100)+0.25*clamp(sofrIqrBps*4,0,100), 0, 100)

    dgs2, dgs10 = L('DGS2'), L('DGS10')
    dgs2p5, dgs10p5 = P5('DGS2'), P5('DGS10')
    move2 = abs((dgs2 - dgs2p5) * 100) if None not in (dgs2, dgs2p5) else None
    move10 = abs((dgs10 - dgs10p5) * 100) if None not in (dgs10, dgs10p5) else None
    treasuryVolStress = clamp((move2*0.6 + move10*0.4)*2, 0, 100) if None not in (move2, move10) else None

    vix = L('VIXCLS')
    vixScore = clamp((vix - 12) * 3.2, 0, 100) if vix is not None else None

    y2Score = clamp((dgs2 - 3.5) * 25, 0, 100) if dgs2 is not None else None
    y10Score = clamp((dgs10 - 4) * 20, 0, 100) if dgs10 is not None else None

    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None

    walcl, wresbal, wtregen = L('WALCL'), L('WRESBAL'), L('WTREGEN')
    fedBsScore = clamp(50 - (walcl - 7000000) / 40000, 0, 100) if walcl is not None else None
    reservesScore = clamp(50 - (wresbal/1000 - 3000) / 20, 0, 100) if wresbal is not None else None
    tgaScore = clamp(20 + (wtregen/1000 - 500) / 10, 0, 100) if wtregen is not None else None

    nfci = L('NFCI')
    nfciScore = clamp(50 + nfci * 35, 0, 100) if nfci is not None else None

    icsa, icsaP5 = L('ICSA'), P5('ICSA')
    econSurpriseScore = None
    if None not in (icsa, icsaP5) and icsaP5:
        econSurpriseScore = clamp(50 + (icsa/icsaP5 - 1)*100*5, 0, 100)

    cpiCore, cpiCoreP1 = L('CPILFESL'), P1('CPILFESL')
    pceCore, pceCoreP1 = L('PCEPILFE'), P1('PCEPILFE')
    inflationLaborScore = None
    if None not in (cpiCore, cpiCoreP1, pceCore, pceCoreP1) and cpiCoreP1 and pceCoreP1:
        coreCpiMo = (cpiCore/cpiCoreP1 - 1) * 100
        corePceMo = (pceCore/pceCoreP1 - 1) * 100
        inflationLaborScore = clamp(50 + ((coreCpiMo*0.5 + corePceMo*0.5) - 0.2)*200, 0, 100)

    fedExpScore = 0.6*y2Score + 0.4*clamp(50+sofrIorbBps*4,0,100) if None not in (y2Score, sofrIorbBps) else None

    walclP5, walclP20 = P5('WALCL'), P20('WALCL')
    wresbalP5, wresbalP20 = P5('WRESBAL'), P20('WRESBAL')
    wtregenP5, wtregenP20 = P5('WTREGEN'), P20('WTREGEN')
    liqFlowComposite = None
    if None not in (walcl, walclP5, walclP20, wresbal, wresbalP5, wresbalP20, wtregen, wtregenP5, wtregenP20):
        netImp5 = (walcl-walclP5) + (wresbal-wresbalP5) - (wtregen-wtregenP5)
        netImp20 = (walcl-walclP20) + (wresbal-wresbalP20) - (wtregen-wtregenP20)
        liqFlow5 = clamp(50 - netImp5/10000, 0, 100)
        liqFlow20 = clamp(50 - netImp20/20000, 0, 100)
        liqFlowComposite = liqFlow5*0.65 + liqFlow20*0.35

    def fx_leg(sid, invert):
        c, d, e, f = L(sid), P1(sid), P5(sid), P20(sid)
        if None in (c, d, e, f) or not d or not e or not f:
            return None
        g, h, i = c/d-1, c/e-1, c/f-1
        raw = (50-250*g-120*h-60*i) if invert else (50+250*g+120*h+60*i)
        return clamp(raw, 0, 100)

    fx_legs = [fx_leg('DEXJPUS', True), fx_leg('DEXUSEU', True), fx_leg('DEXCHUS', False),
               fx_leg('DEXSZUS', True), fx_leg('DEXUSAL', True), fx_leg('DTWEXBGS', False)]
    fxStress = None if None in fx_legs else (0.3*fx_legs[0]+0.15*fx_legs[1]+0.2*fx_legs[2]+0.1*fx_legs[3]+0.1*fx_legs[4]+0.15*fx_legs[5])

    # category scores using post-fix weights (2s10s / Fed Expectations / standalone
    # SOFR-IORB zeroed out; Liquidity Flow Stress included)
    credit_rows = [(hyScore, .13), (igScore, .07)]
    liquidity_rows = [(repoScore, .12), (dxyScore, .05), (fedBsScore, .03), (reservesScore, .04), (tgaScore, .04), (liqFlowComposite, .08)]
    rates_rows = [(treasuryVolStress, .07), (y2Score, .05), (y10Score, .04)]

    def cat_avg(rows):
        valid = [(s, w) for s, w in rows if s is not None]
        if not valid:
            return None
        wsum = sum(w for _, w in valid)
        return sum(s*w for s, w in valid) / wsum if wsum > 0 else None

    B5, B6, B7 = cat_avg(liquidity_rows), cat_avg(credit_rows), cat_avg(rates_rows)
    K8 = clamp(0.45*B7 + 0.3*fedExpScore + 0.25*dxyScore, 0, 100) if None not in (B7, fedExpScore, dxyScore) else None
    K10 = fxStress

    if None in (B5, B6, B7, K8, K10):
        return None
    if B5 >= 60 and B6 >= 60 and (B7 < 60 or K10 >= 70):
        return 'Deflationary / Funding Crisis'
    if B5 >= 55 and B7 >= 60 and K8 >= 60:
        return 'Inflationary Tightening'
    if (B5 >= 55 or B6 >= 55) and K10 >= 65:
        return 'Funding / Credit Stress'
    if B5 <= 30 and K10 < 45:
        return 'Liquidity Expansion'
    if B5 < 50 and K10 < 55:
        return 'Neutral / Balanced'
    return 'General Tightening'


def classify_regime_asof_ORIGINAL(S, date):
    """Reconstructs the regime using the workbook's ORIGINAL formulas —
    bugs included — for a head-to-head accuracy comparison against the
    corrected version above. Specifically, unlike classify_regime_asof():
      - Bank Reserves / TGA use the original (un-corrected) thresholds,
        which are off by ~1000x against FRED's units.
      - Liquidity Flow Stress is entirely excluded from both the Liquidity
        category average and its weight sum (the original SUMIF range bug).
      - SOFR-IORB, 2s10s Curve, and Fed Expectations are all separately
        weighted despite the redundancy with Repo-Market Stress / 2Y+10Y —
        i.e. no de-duplication.
      - Credit stays at its original, lower weight (14%, not 20%).
      - FX Stress is computed from the ORIGINAL workbook's row-misaligned
        lookup — each "currency pair" actually reads an unrelated economic
        series (documented in the chat this was built in). Replicated
        exactly here, not fixed, because the point is to test what the
        shipped file would have actually produced.
      - S&P 500 Breadth / Market Participation Momentum are forced to
        unavailable (None), matching the original file's broken
        STOCKHISTORY-based formulas.
    """
    L = lambda k: asof(S.get(k), date, 0)
    P1 = lambda k: asof(S.get(k), date, 1)
    P5 = lambda k: asof(S.get(k), date, 5)
    P20 = lambda k: asof(S.get(k), date, 20)

    hy, ig = L('BAMLH0A0HYM2'), L('BAMLC0A0CM')
    hyScore, igScore = hy_score(hy), (clamp((ig - 60) / 2.4, 0, 100) if ig is not None else None)

    sofr, iorb, effr, s25, s75 = L('SOFR'), L('IORB'), L('EFFR'), L('SOFR25'), L('SOFR75')
    sofrIorbBps = (sofr - iorb) * 100 if None not in (sofr, iorb) else None
    sofrEffrBps = (sofr - effr) * 100 if None not in (sofr, effr) else None
    sofrIqrBps = (s75 - s25) * 100 if None not in (s75, s25) else None
    sofrIorbScore = clamp(50 + sofrIorbBps * 4, 0, 100) if sofrIorbBps is not None else None
    repoScore = None
    if None not in (sofrIorbBps, sofrEffrBps, sofrIqrBps):
        repoScore = clamp(0.45*clamp(20+sofrIorbBps*3,0,100)+0.3*clamp(20+sofrEffrBps*4,0,100)+0.25*clamp(sofrIqrBps*4,0,100), 0, 100)

    dgs2, dgs10 = L('DGS2'), L('DGS10')
    dgs2p5, dgs10p5 = P5('DGS2'), P5('DGS10')
    move2 = abs((dgs2 - dgs2p5) * 100) if None not in (dgs2, dgs2p5) else None
    move10 = abs((dgs10 - dgs10p5) * 100) if None not in (dgs10, dgs10p5) else None
    treasuryVolStress = clamp((move2*0.6 + move10*0.4)*2, 0, 100) if None not in (move2, move10) else None

    y2Score = clamp((dgs2 - 3.5) * 25, 0, 100) if dgs2 is not None else None
    y10Score = clamp((dgs10 - 4) * 20, 0, 100) if dgs10 is not None else None
    t2s10 = L('T10Y2Y')
    curveScore = clamp(50 - 20 * t2s10, 0, 100) if t2s10 is not None else None

    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None

    walcl, wresbal, wtregen = L('WALCL'), L('WRESBAL'), L('WTREGEN')
    fedBsScore = clamp(50 - (walcl - 7000000) / 40000, 0, 100) if walcl is not None else None
    # BUG (replicated): thresholds assume billions, FRED returns millions
    reservesScore = clamp(50 - (wresbal - 3000) / 20, 0, 100) if wresbal is not None else None
    tgaScore = clamp(20 + (wtregen - 500) / 10, 0, 100) if wtregen is not None else None

    nfci = L('NFCI')
    nfciScore = clamp(50 + nfci * 35, 0, 100) if nfci is not None else None

    fedExpScore = 0.6*y2Score + 0.4*sofrIorbScore if None not in (y2Score, sofrIorbScore) else None

    def fx_leg(sid, invert):
        c, d, e, f = L(sid), P1(sid), P5(sid), P20(sid)
        if None in (c, d, e, f) or not d or not e or not f:
            return None
        g, h, i = c/d-1, c/e-1, c/f-1
        raw = (50-250*g-120*h-60*i) if invert else (50+250*g+120*h+60*i)
        return clamp(raw, 0, 100)

    # BUG (replicated): each "FX pair" formula actually reads the wrong row
    # of the master lookup table. USD/JPY -> CPILFESL, EUR/USD -> PCEPILFE,
    # USD/CNY -> PAYEMS, USD/CHF -> CPIAUCSL, AUD/USD -> PCEPI,
    # "Broad USD Index" -> DEXUSAL (AUD/USD's real data, mislabeled).
    k4 = fx_leg('CPILFESL', True)
    k5 = fx_leg('PCEPILFE', True)
    k6 = fx_leg('PAYEMS', False)
    k7 = fx_leg('CPIAUCSL', True)
    k8_fx = fx_leg('PCEPI', True)
    k9 = fx_leg('DEXUSAL', False)
    fx_legs = [k4, k5, k6, k7, k8_fx, k9]
    fxStress = None if None in fx_legs else (0.3*k4+0.15*k5+0.2*k6+0.1*k7+0.1*k8_fx+0.15*k9)

    # No de-duplication in the original: SOFR-IORB, 2s10s, and Fed
    # Expectations are all separately weighted alongside the composites
    # that already contain the same information.
    credit_rows = [(hyScore, .10), (igScore, .04)]
    liquidity_rows = [(sofrIorbScore, .06), (repoScore, .06), (dxyScore, .05), (fedBsScore, .03), (reservesScore, .04), (tgaScore, .04)]
    rates_rows = [(treasuryVolStress, .07), (y2Score, .05), (y10Score, .04), (curveScore, .03), (fedExpScore, .03)]

    def cat_avg(rows):
        valid = [(s, w) for s, w in rows if s is not None]
        if not valid:
            return None
        wsum = sum(w for _, w in valid)
        return sum(s*w for s, w in valid) / wsum if wsum > 0 else None

    B5, B6, B7 = cat_avg(liquidity_rows), cat_avg(credit_rows), cat_avg(rates_rows)
    K8 = clamp(0.45*B7 + 0.3*fedExpScore + 0.25*dxyScore, 0, 100) if None not in (B7, fedExpScore, dxyScore) else None
    K10 = fxStress

    if None in (B5, B6, B7, K8, K10):
        return None
    if B5 >= 60 and B6 >= 60 and (B7 < 60 or K10 >= 70):
        return 'Deflationary / Funding Crisis'
    if B5 >= 55 and B7 >= 60 and K8 >= 60:
        return 'Inflationary Tightening'
    if (B5 >= 55 or B6 >= 55) and K10 >= 65:
        return 'Funding / Credit Stress'
    if B5 <= 30 and K10 < 45:
        return 'Liquidity Expansion'
    if B5 < 50 and K10 < 55:
        return 'Neutral / Balanced'
    return 'General Tightening'


def main():
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("This script needs yfinance: pip install pandas yfinance")

    print('Fetching FRED history (2015-present)...', file=sys.stderr)
    S = {}
    for sid in FRED_SERIES:
        try:
            S[sid] = fetch_fred_full(sid)
            print(f'  {sid}: {len(S[sid])} obs', file=sys.stderr)
        except Exception as e:
            print(f'  {sid}: FAILED ({e})', file=sys.stderr)
            S[sid] = pd.Series(dtype=float)

    tickers = [t for _, t, _ in ASSET_TABLE]
    print(f'Fetching price history for {len(tickers)} tickers via yfinance...', file=sys.stderr)
    prices = yf.download(tickers, start='2017-06-01', progress=False, auto_adjust=True)['Close']
    if isinstance(prices, pd.Series):
        prices = prices.to_frame()

    month_ends = pd.date_range('2018-01-31', datetime.now(timezone.utc).date().isoformat(), freq='ME')
    # need at least ~3 months of forward price data to score a prediction
    usable_dates = [d for d in month_ends if d + pd.Timedelta(days=100) <= prices.index.max()]

    print(f'Classifying regime (both versions) for {len(usable_dates)} historical month-ends...', file=sys.stderr)
    regime_history = []
    for d in usable_dates:
        regime_history.append({
            'date': d.date().isoformat(),
            'regime_new': classify_regime_asof(S, d),
            'regime_old': classify_regime_asof_ORIGINAL(S, d),
        })

    print('Scoring predictions against actual forward 3M returns...', file=sys.stderr)

    def score_version(regime_key):
        results = {name: {'hits': 0, 'total': 0, 'by_regime': {}} for name, _, _ in ASSET_TABLE}
        for entry in regime_history:
            d = pd.Timestamp(entry['date'])
            regime = entry[regime_key]
            if regime is None:
                continue
            col = REGIME_COL_INDEX.get(regime)
            if col is None:
                continue
            for name, ticker, directions in ASSET_TABLE:
                if ticker not in prices.columns:
                    continue
                price_series = prices[ticker].dropna()
                p0 = asof(price_series, d, 0)
                future_window = price_series.loc[d + pd.Timedelta(days=80): d + pd.Timedelta(days=100)]
                if p0 is None or future_window.empty:
                    continue
                fwd_return = (future_window.iloc[0] / p0) - 1
                direction = directions[col]
                rank = DIRECTION_RANK.get(direction, 0)
                if rank == 0:
                    continue
                hit = (rank > 0 and fwd_return > 0) or (rank < 0 and fwd_return < 0)
                results[name]['total'] += 1
                results[name]['hits'] += int(hit)
                results[name]['by_regime'].setdefault(regime, {'hits': 0, 'total': 0})
                results[name]['by_regime'][regime]['total'] += 1
                results[name]['by_regime'][regime]['hits'] += int(hit)
        for name in results:
            r = results[name]
            r['hit_rate'] = round(r['hits']/r['total'], 3) if r['total'] else None
        return results

    results_new = score_version('regime_new')
    results_old = score_version('regime_old')

    def overall(results):
        hits = sum(r['hits'] for r in results.values())
        total = sum(r['total'] for r in results.values())
        return hits, total, (round(hits/total, 3) if total else None)

    new_hits, new_total, new_rate = overall(results_new)
    old_hits, old_total, old_rate = overall(results_old)

    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'regime_history': regime_history,
        'new_model': {'per_asset': results_new, 'overall_hit_rate': new_rate, 'hits': new_hits, 'total': new_total},
        'old_model': {'per_asset': results_old, 'overall_hit_rate': old_rate, 'hits': old_hits, 'total': old_total},
    }
    with open('asset_outlook_backtest.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print()
    print(f"OLD (original workbook, bugs included): {old_rate} ({old_hits}/{old_total} scored calls)" if old_total else "OLD: no scoreable predictions")
    print(f"NEW (corrected, de-duplicated):         {new_rate} ({new_hits}/{new_total} scored calls)" if new_total else "NEW: no scoreable predictions")
    print()
    print(f"{'Asset':<28} {'OLD':<10} {'NEW':<10} {'n(old)':<8} {'n(new)':<8}")
    for name, _, _ in ASSET_TABLE:
        o, n = results_old[name], results_new[name]
        o_hr = f"{o['hit_rate']*100:.0f}%" if o['hit_rate'] is not None else 'n/a'
        n_hr = f"{n['hit_rate']*100:.0f}%" if n['hit_rate'] is not None else 'n/a'
        print(f"{name:<28} {o_hr:<10} {n_hr:<10} {o['total']:<8} {n['total']:<8}")


if __name__ == '__main__':
    main()
