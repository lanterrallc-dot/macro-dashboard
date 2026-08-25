#!/usr/bin/env python3
"""
Macro / Liquidity Risk Model — server-side refresh.

Fetches every input series directly from FRED and Stooq (no CORS
restriction applies to server-side requests) and recomputes the full
model using the same formulas extracted from the source workbook.
Writes model_output.json, which the dashboard reads.

Run manually:      python3 refresh_model.py
Run on a schedule:  see .github/workflows/refresh.yml
"""

import bisect
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

FRED_API_KEY = os.environ.get('FRED_API_KEY', '').strip()

FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'PAYEMS', 'CPIAUCSL', 'PCEPI', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'T10Y2Y', 'DTWEXBGS', 'WALCL', 'WRESBAL', 'WTREGEN',
    'NFCI', 'EFFR', 'SOFR25', 'SOFR75', 'DEXJPUS', 'DEXUSEU',
    'DEXCHUS', 'DEXSZUS', 'DEXUSAL',
]

STOOQ_TICKERS = {'SPY': 'SPY', 'RSP': 'RSP'}  # kept name for minimal downstream diff; now yfinance symbols

UA = {'User-Agent': 'Mozilla/5.0 (macro-liquidity-model-refresh)'}


def http_get(url, timeout=25, retries=2):
    """Fetches a URL with a couple of retries — GitHub's shared runners
    occasionally hit a bad network window where every request times out at
    once (not a FRED/Stooq problem, a runner problem). A short retry with
    backoff clears most of these transient blips without masking a real
    persistent failure."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))  # 3s, then 6s
    raise last_err


def fetch_fred_series(series_id, days_back=800):
    """Fetches via FRED's official JSON API when FRED_API_KEY is set (far
    more reliable than the public CSV export endpoint, which appears to be
    getting blocked/throttled for GitHub Actions' shared runner IPs — every
    request to it timing out, while general internet access on the same
    runner works fine, is the signature of an endpoint-specific block).
    Falls back to the old CSV scrape if no key is configured, so this still
    works if run somewhere without the FRED_API_KEY environment variable
    set (e.g. testing locally without it)."""
    cosd = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')

    if FRED_API_KEY:
        url = (f'https://api.stlouisfed.org/fred/series/observations'
               f'?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json'
               f'&observation_start={cosd}')
        try:
            text = http_get(url)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f'  WARN: {series_id} fetch failed (official API): {e}', file=sys.stderr)
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f'  WARN: {series_id} bad response from official API: {e}', file=sys.stderr)
            return []
        out = []
        for obs in data.get('observations', []):
            v = obs.get('value')
            if v in (None, '.', ''):
                continue
            try:
                out.append({'date': obs['date'], 'value': float(v)})
            except (ValueError, KeyError):
                continue
        return out

    # fallback: old CSV export endpoint (used only if no API key configured)
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cosd}'
    try:
        text = http_get(url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f'  WARN: {series_id} fetch failed: {e}', file=sys.stderr)
        return []
    out = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(',')
        if len(parts) < 2:
            continue
        try:
            out.append({'date': parts[0].strip(), 'value': float(parts[1].strip())})
        except ValueError:
            continue
    return out


def fetch_yfinance_bulk(tickers, days_back=220):
    """Fetches all requested tickers' price history in a single yfinance
    call (same library already proven working in backtest_asset_outlook.py
    today, on this same infrastructure). Returns a dict keyed by ticker,
    each value a list of {'date','value'} dicts in the same shape the rest
    of this script already expects from the old Stooq fetcher, so nothing
    downstream needs to change."""
    try:
        import yfinance as yf
    except ImportError:
        print('  WARN: yfinance not installed — equity/breadth data unavailable this run', file=sys.stderr)
        return {t: [] for t in tickers}

    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')
    try:
        df = yf.download(tickers, start=start, progress=False, auto_adjust=True,
                          group_by='ticker', threads=True)
    except Exception as e:
        print(f'  WARN: yfinance bulk download failed: {e}', file=sys.stderr)
        return {t: [] for t in tickers}

    result = {}
    for t in tickers:
        try:
            series = df['Close'] if len(tickers) == 1 else df[t]['Close']
            series = series.dropna()
            result[t] = [{'date': idx.strftime('%Y-%m-%d'), 'value': float(v)} for idx, v in series.items()]
        except Exception as e:
            print(f'  WARN: {t} yfinance parse failed: {e}', file=sys.stderr)
            result[t] = []
    return result


def obs(arr, back):
    if not arr:
        return None
    idx = len(arr) - 1 - back
    return arr[idx]['value'] if idx >= 0 else None


def asof_at(series, cutoff_date, back=0):
    """Like obs(), but relative to a specific date cutoff rather than the
    end of the series — used to reconstruct historical scores. `series`
    must be sorted ascending by date (fetch_fred_series/fetch_stooq_series
    already return it that way)."""
    if not series:
        return None
    # series is sorted ascending; find valid entries up to cutoff
    valid = [p['value'] for p in series if p['date'] <= cutoff_date]
    idx = len(valid) - 1 - back
    return valid[idx] if idx >= 0 else None


def clamp(x, lo, hi):
    if x is None:
        return None
    return min(hi, max(lo, x))


def percentile_score(current, series, asof_date=None, window=500, invert=False):
    """0-100 score for where `current` sits within its OWN trailing
    distribution, instead of a fixed absolute threshold.

    Why this exists: fixed thresholds (e.g. "HY spreads under 250bps score
    ~10") pin a metric near its floor for months whenever the market sits
    in a calm range within that threshold — the score simply has no room
    left to move, which looks like "no relationship to anything" on a
    chart even though the underlying data is moving normally. Scoring
    relative to the metric's own recent history keeps it responsive in any
    regime: a move that's unusual FOR THIS METRIC RIGHT NOW registers,
    even if it would have been unremarkable during a different multi-year
    period.

    `asof_date`, if given, restricts the comparison pool to observations
    up to and including that date — required for the historical
    reconstruction (score_all_asof) to avoid lookahead bias; omit it for
    live scoring, where "up to now" is just the whole fetched series.
    `invert=True` for metrics where a HIGHER raw value means LESS stress
    (e.g. Fed Balance Sheet expansion), so the percentile ranking flips."""
    if current is None or not series:
        return None
    if asof_date is not None:
        pool = [p['value'] for p in series if p['date'] <= asof_date]
    else:
        pool = [p['value'] for p in series]
    pool = pool[-window:]
    if len(pool) < 30:
        return None
    pool_sorted = sorted(pool)
    idx = bisect.bisect_left(pool_sorted, current)
    pct = idx / len(pool_sorted) * 100
    return round((100 - pct) if invert else pct, 2)


def compute_model(S, E):
    """S = dict of FRED series arrays, E = dict of equity series arrays (SPY, RSP)."""
    L = lambda k: obs(S.get(k), 0)
    P1 = lambda k: obs(S.get(k), 1)
    P5 = lambda k: obs(S.get(k), 5)
    P20 = lambda k: obs(S.get(k), 20)

    hy, ig = L('BAMLH0A0HYM2'), L('BAMLC0A0CM')
    hyScore = percentile_score(hy, S.get('BAMLH0A0HYM2', []))
    igScore = percentile_score(ig, S.get('BAMLC0A0CM', []))

    sofr, iorb, effr, s25, s75 = L('SOFR'), L('IORB'), L('EFFR'), L('SOFR25'), L('SOFR75')
    sofrIorbBps = (sofr - iorb) * 100 if None not in (sofr, iorb) else None
    sofrEffrBps = (sofr - effr) * 100 if None not in (sofr, effr) else None
    sofrIqrBps = (s75 - s25) * 100 if None not in (s75, s25) else None
    sofrIorbScore = clamp(50 + sofrIorbBps * 4, 0, 100) if sofrIorbBps is not None else None
    repoScore = None
    if None not in (sofrIorbBps, sofrEffrBps, sofrIqrBps):
        repoScore = clamp(
            0.45 * clamp(20 + sofrIorbBps * 3, 0, 100)
            + 0.3 * clamp(20 + sofrEffrBps * 4, 0, 100)
            + 0.25 * clamp(sofrIqrBps * 4, 0, 100), 0, 100)

    dgs2, dgs2p5 = L('DGS2'), P5('DGS2')
    dgs10, dgs10p5 = L('DGS10'), P5('DGS10')
    move2 = abs((dgs2 - dgs2p5) * 100) if None not in (dgs2, dgs2p5) else None
    move10 = abs((dgs10 - dgs10p5) * 100) if None not in (dgs10, dgs10p5) else None
    treasuryVolStress = clamp((move2 * 0.6 + move10 * 0.4) * 2, 0, 100) if None not in (move2, move10) else None

    vix, vixp5 = L('VIXCLS'), P5('VIXCLS')
    vixScore = clamp((vix - 12) * 3.2, 0, 100) if vix is not None else None
    vixChgPct = (vix / vixp5 - 1) * 100 if None not in (vix, vixp5) and vixp5 else None
    vixTermProxy = clamp(50 + vixChgPct * 4, 0, 100) if vixChgPct is not None else None

    y2Score = percentile_score(dgs2, S.get('DGS2', []))
    y10Score = percentile_score(dgs10, S.get('DGS10', []))
    t2s10 = L('T10Y2Y')
    curveScore = clamp(50 - 20 * t2s10, 0, 100) if t2s10 is not None else None

    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None

    walcl, walclP5, walclP20 = L('WALCL'), P5('WALCL'), P20('WALCL')
    wresbal, wresbalP5, wresbalP20 = L('WRESBAL'), P5('WRESBAL'), P20('WRESBAL')
    wtregen, wtregenP5, wtregenP20 = L('WTREGEN'), P5('WTREGEN'), P20('WTREGEN')
    # FIX (unit mismatch): WALCL/WRESBAL/WTREGEN are reported by FRED in $ millions.
    # The Fed Balance Sheet threshold (7,000,000) is already written on that same
    # millions scale, so it needs no change. Bank Reserves (threshold 3,000) and
    # Treasury General Account (threshold 500) were written as if the input were
    # in $ billions -- three orders of magnitude off, which is why Bank Reserves
    # was pinned at 0 and TGA was pinned at 100 in the original workbook. Dividing
    # by 1000 to convert millions -> billions before scoring restores both to a
    # normal, non-saturated range.
    fedBsScore = clamp(50 - (walcl - 7000000) / 40000, 0, 100) if walcl is not None else None
    reservesScore = clamp(50 - (wresbal/1000 - 3000) / 20, 0, 100) if wresbal is not None else None
    tgaScore = clamp(20 + (wtregen/1000 - 500) / 10, 0, 100) if wtregen is not None else None

    nfci = L('NFCI')
    nfciScore = clamp(50 + nfci * 35, 0, 100) if nfci is not None else None

    icsa, icsaP5 = L('ICSA'), P5('ICSA')
    claimsChgPct = (icsa / icsaP5 - 1) * 100 if None not in (icsa, icsaP5) and icsaP5 else None
    econSurpriseScore = clamp(50 + claimsChgPct * 5, 0, 100) if claimsChgPct is not None else None

    cpiCore, cpiCoreP1 = L('CPILFESL'), P1('CPILFESL')
    pceCore, pceCoreP1 = L('PCEPILFE'), P1('PCEPILFE')
    coreCpiMo = (cpiCore / cpiCoreP1 - 1) * 100 if None not in (cpiCore, cpiCoreP1) and cpiCoreP1 else None
    corePceMo = (pceCore / pceCoreP1 - 1) * 100 if None not in (pceCore, pceCoreP1) and pceCoreP1 else None
    inflationLaborScore = None
    if None not in (coreCpiMo, corePceMo):
        inflationLaborScore = clamp(50 + ((coreCpiMo * 0.5 + corePceMo * 0.5) - 0.2) * 200, 0, 100)

    fedExpScore = 0.6 * y2Score + 0.4 * sofrIorbScore if None not in (y2Score, sofrIorbScore) else None

    dRes5 = (wresbal - wresbalP5) if None not in (wresbal, wresbalP5) else None
    dRes20 = (wresbal - wresbalP20) if None not in (wresbal, wresbalP20) else None
    dTga5 = (wtregen - wtregenP5) if None not in (wtregen, wtregenP5) else None
    dTga20 = (wtregen - wtregenP20) if None not in (wtregen, wtregenP20) else None
    dFed5 = (walcl - walclP5) if None not in (walcl, walclP5) else None
    dFed20 = (walcl - walclP20) if None not in (walcl, walclP20) else None
    netImp5 = (dFed5 + dRes5 - dTga5) if None not in (dFed5, dRes5, dTga5) else None
    netImp20 = (dFed20 + dRes20 - dTga20) if None not in (dFed20, dRes20, dTga20) else None
    liqFlow5 = clamp(50 - netImp5 / 10000, 0, 100) if netImp5 is not None else None
    liqFlow20 = clamp(50 - netImp20 / 20000, 0, 100) if netImp20 is not None else None
    liqFlowComposite = (liqFlow5 * 0.65 + liqFlow20 * 0.35) if None not in (liqFlow5, liqFlow20) else None

    def fx_leg(series_id, invert):
        c, d, e, f = L(series_id), P1(series_id), P5(series_id), P20(series_id)
        if None in (c, d, e, f) or not d or not e or not f:
            return None
        g, h, i = c / d - 1, c / e - 1, c / f - 1
        raw = (50 - 250 * g - 120 * h - 60 * i) if invert else (50 + 250 * g + 120 * h + 60 * i)
        return clamp(raw, 0, 100)

    jpyScore = fx_leg('DEXJPUS', True)
    eurScore = fx_leg('DEXUSEU', True)
    cnyScore = fx_leg('DEXCHUS', False)
    chfScore = fx_leg('DEXSZUS', True)
    audScore = fx_leg('DEXUSAL', True)
    dxyFxScore = fx_leg('DTWEXBGS', False)
    fx_legs = [jpyScore, eurScore, cnyScore, chfScore, audScore, dxyFxScore]
    fxStress = None
    if None not in fx_legs:
        fxStress = 0.3*jpyScore + 0.15*eurScore + 0.2*cnyScore + 0.1*chfScore + 0.1*audScore + 0.15*dxyFxScore

    # --- equity breadth (now live via Stooq, unlike the original workbook) ---
    spy, rsp = E.get('SPY', []), E.get('RSP', [])
    spyL, spyP5, spyP20 = obs(spy, 0), obs(spy, 5), obs(spy, 20)
    rspL, rspP5, rspP20 = obs(rsp, 0), obs(rsp, 5), obs(rsp, 20)
    breadth5D = breadth20D = None
    if None not in (spyL, spyP5, rspL, rspP5) and spyP5 and rspP5:
        breadth5D = ((rspL / rspP5) / (spyL / spyP5) - 1) * 100
    if None not in (spyL, spyP20, rspL, rspP20) and spyP20 and rspP20:
        breadth20D = ((rspL / rspP20) / (spyL / spyP20) - 1) * 100
    breadthStress = clamp(50 - (breadth5D*10 + breadth20D*5), 0, 100) if None not in (breadth5D, breadth20D) else None
    participationMomentum = clamp(50 - (breadth5D*15 + breadth20D*10), 0, 100) if None not in (breadth5D, breadth20D) else None

    # WEIGHTING CHANGES vs. the original workbook (agreed in chat before this
    # script was written — see REVISIONS.md for the full rationale):
    #
    #   1. Liquidity Flow Stress (8%) now actually counts toward Overall Risk.
    #      The original SUM() range stopped one row short and silently
    #      dropped it despite the weight-check table assuming it was included.
    #
    #   2. SOFR–IORB Spread and Fed Expectations and 2s10s Curve are no longer
    #      separately weighted. Each was double-counting information already
    #      priced into another weighted indicator:
    #        - SOFR–IORB is 45% of the Repo-Market Stress composite already;
    #          its 6% standalone weight is folded into Repo-Market Stress
    #          (6% -> 12%), so total Liquidity weight is unchanged.
    #        - Fed Expectations = 0.6x(2Y score) + 0.4x(SOFR-IORB score) --
    #          entirely derived from two indicators already counted elsewhere.
    #        - 2s10s Curve = 10Y minus 2Y, both already counted separately.
    #      Their combined 6% (Fed Expectations 3% + 2s10s 3%) moves to Credit,
    #      which was underweighted (14%) relative to its historical value as
    #      a leading stress indicator: HY spreads 10%->13%, IG spreads 4%->7%.
    #      All three stay in the table for visibility (reading + score still
    #      shown) but are flagged `redundant` and carry 0 weight.
    #
    #   Net category weights: Credit 14%->20%, Rates 22%->16%, Liquidity and
    #   Market/Macro unchanged at 36% (with Liquidity Flow Stress now live)
    #   and 28% respectively. Total stays 100%.
    indicators = [
        {'name': 'HY Credit Spreads', 'category': 'Credit', 'weight': .13, 'reading': hy, 'units': 'bps', 'score': hyScore},
        {'name': 'Investment-Grade Spreads', 'category': 'Credit', 'weight': .07, 'reading': ig, 'units': 'bps', 'score': igScore},
        {'name': 'SOFR–IORB Spread', 'category': 'Liquidity', 'weight': 0, 'reading': sofrIorbBps, 'units': 'bps', 'score': sofrIorbScore, 'redundant': 'folded into Repo-Market Stress (45% of that composite) — weight moved there to avoid double-counting'},
        {'name': 'Repo-Market Stress', 'category': 'Liquidity', 'weight': .12, 'reading': repoScore, 'units': '0–100', 'score': repoScore},
        {'name': 'Treasury Vol Proxy (MOVE-style)', 'category': 'Rates', 'weight': .07, 'reading': treasuryVolStress, 'units': '0–100', 'score': treasuryVolStress, 'source_note': 'synthetic proxy from 2Y/10Y 5-day moves — the real MOVE index isn\u2019t freely available via FRED'},
        {'name': 'VIX', 'category': 'Market / Macro', 'weight': .05, 'reading': vix, 'units': 'index', 'score': vixScore},
        {'name': 'VIX Momentum Proxy (5D)', 'category': 'Market / Macro', 'weight': .05, 'reading': vixTermProxy, 'units': '0–100', 'score': vixTermProxy, 'source_note': 'transform of VIX\u2019s own 5-day change — not real futures term-structure data'},
        {'name': '2Y Treasury Yield', 'category': 'Rates', 'weight': .05, 'reading': dgs2, 'units': '%', 'score': y2Score},
        {'name': '10Y Treasury Yield', 'category': 'Rates', 'weight': .04, 'reading': dgs10, 'units': '%', 'score': y10Score},
        {'name': '2s10s Curve', 'category': 'Rates', 'weight': 0, 'reading': t2s10, 'units': 'pct pts', 'score': curveScore, 'redundant': 'derived from 2Y and 10Y, both already counted separately — weight moved to Credit'},
        {'name': 'DXY / Broad Dollar', 'category': 'Liquidity', 'weight': .05, 'reading': dxy, 'units': 'index', 'score': dxyScore},
        {'name': 'Fed Balance Sheet', 'category': 'Liquidity', 'weight': .03, 'reading': walcl, 'units': '$mm', 'score': fedBsScore},
        {'name': 'Bank Reserves', 'category': 'Liquidity', 'weight': .04, 'reading': wresbal, 'units': '$mm', 'score': reservesScore, 'flag': 'unit-corrected (\u00f71000 to billions) vs. original workbook — see notes'},
        {'name': 'Treasury General Account', 'category': 'Liquidity', 'weight': .04, 'reading': wtregen, 'units': '$mm', 'score': tgaScore, 'flag': 'unit-corrected (\u00f71000 to billions) vs. original workbook — see notes'},
        {'name': 'Financial Conditions (NFCI)', 'category': 'Market / Macro', 'weight': .04, 'reading': nfci, 'units': 'index', 'score': nfciScore},
        {'name': 'S&P 500 Breadth', 'category': 'Market / Macro', 'weight': .05, 'reading': breadthStress, 'units': '0–100', 'score': breadthStress, 'source_note': 'now live via Stooq SPY/RSP — unavailable in the original workbook'},
        {'name': 'Market Participation Momentum', 'category': 'Market / Macro', 'weight': .03, 'reading': participationMomentum, 'units': '0–100', 'score': participationMomentum, 'source_note': 'now live via Stooq SPY/RSP — unavailable in the original workbook'},
        {'name': 'Economic Surprise', 'category': 'Market / Macro', 'weight': .03, 'reading': econSurpriseScore, 'units': '0–100', 'score': econSurpriseScore},
        {'name': 'Inflation & Labor Momentum', 'category': 'Market / Macro', 'weight': .03, 'reading': inflationLaborScore, 'units': '0–100', 'score': inflationLaborScore},
        {'name': 'Fed Expectations', 'category': 'Rates', 'weight': 0, 'reading': fedExpScore, 'units': '0–100', 'score': fedExpScore, 'redundant': 'derived entirely from the 2Y Yield and SOFR-IORB scores, both already counted separately — weight moved to Credit'},
        {'name': 'Liquidity Flow Stress', 'category': 'Liquidity', 'weight': .08, 'reading': liqFlowComposite, 'units': '0–100', 'score': liqFlowComposite},
    ]

    for ind in indicators:
        ind['weighted'] = ind['weight'] * ind['score'] if ind['score'] is not None else None

    contributing = [i for i in indicators if i['weighted'] is not None and i['weight'] > 0]
    overall_risk = sum(i['weighted'] for i in contributing) if contributing else None
    effective_weight = sum(i['weight'] for i in contributing)

    cats = ['Liquidity', 'Credit', 'Rates', 'Market / Macro']
    category_scores = {}
    for cat in cats:
        rows = [i for i in indicators if i['category'] == cat and i['score'] is not None]
        wsum = sum(i['weight'] for i in rows)
        hsum = sum(i['weighted'] for i in rows)
        category_scores[cat] = (hsum / wsum) if wsum > 0 else None

    inflationary_pressure = None
    if None not in (category_scores['Rates'], fedExpScore, dxyScore):
        inflationary_pressure = clamp(0.45*category_scores['Rates'] + 0.3*fedExpScore + 0.25*dxyScore, 0, 100)

    regime = 'Insufficient data'
    B5, B6, B7, K8, K10 = category_scores['Liquidity'], category_scores['Credit'], category_scores['Rates'], inflationary_pressure, fxStress
    if None not in (B5, B6, B7, K8, K10):
        if B5 >= 60 and B6 >= 60 and (B7 < 60 or K10 >= 70):
            regime = 'Deflationary / Funding Crisis'
        elif B5 >= 55 and B7 >= 60 and K8 >= 60:
            regime = 'Inflationary Tightening'
        elif (B5 >= 55 or B6 >= 55) and K10 >= 65:
            regime = 'Funding / Credit Stress'
        elif B5 <= 30 and K10 < 45:
            regime = 'Liquidity Expansion'
        elif B5 < 50 and K10 < 55:
            regime = 'Neutral / Balanced'
        else:
            regime = 'General Tightening'

    asset_outlook = build_asset_outlook(regime)

    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'indicators': indicators,
        'overall_risk': overall_risk,
        'effective_weight': effective_weight,
        'category_scores': category_scores,
        'inflationary_pressure': inflationary_pressure,
        'fx_stress': fxStress,
        'regime': regime,
        'asset_outlook': asset_outlook,
    }


# Asset-class direction by regime, transcribed from the workbook's "Asset
# Outlook" sheet: columns are [Expansion, Neutral, Inflationary Tightening,
# Funding/Credit Stress, Deflationary Crisis]. "General Tightening" maps to
# the same column as Funding/Credit Stress, matching the workbook's own
# IF() logic (OR($B$3="Funding / Credit Stress", $B$3="General Tightening")).
ASSET_TABLE = [
    # name, ticker, expansion, neutral, inflationary_tightening, funding_credit_stress, deflationary_crisis, fx_transmission, interpretation
    ('S&P 500', 'SPY', 'UP', 'MIXED', 'DOWN', 'DOWN', 'DOWN', 'JPY carry unwind', 'Strong yen can pressure leveraged/global risk assets'),
    ('Nasdaq / Growth', 'QQQ', 'UP', 'MIXED', 'DOWN STRONG', 'DOWN', 'DOWN', 'USD funding', 'Broad USD strength can tighten global liquidity'),
    ('Small Caps', 'IWM', 'UP STRONG', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN STRONG', 'USD funding', 'Broad USD strength often pressures high-duration growth'),
    ('Value Stocks', 'VTV', 'UP', 'MIXED', 'MIXED / DOWN', 'DOWN', 'DOWN', 'USD / growth', 'Strong USD can pressure smaller domestic/leveraged firms less directly than EM'),
    ('High Dividend Stocks', 'VYM', 'UP', 'MIXED', 'MIXED', 'DOWN', 'DOWN', 'USD', 'Strong USD can weigh on multinational earnings'),
    ('High-Yield Bonds', 'HYG', 'UP', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN STRONG', 'USD funding', 'Dollar stress can widen HY spreads'),
    ('Investment-Grade Bonds', 'LQD', 'UP', 'MIXED', 'DOWN', 'DOWN', 'UP / MIXED', 'USD / rates', 'Depends on whether FX stress is inflationary or deflationary'),
    ('Short Treasuries / T-Bills', 'BIL / SGOV', 'MIXED / UP', 'UP', 'UP', 'UP', 'UP', 'Safe collateral', 'Often resilient in FX/liquidity stress'),
    ('Long Treasuries', 'TLT', 'UP', 'MIXED', 'DOWN STRONG', 'MIXED / UP', 'UP STRONG', 'Safe haven', 'Can benefit in deflationary stress; hurt in inflationary tightening'),
    ('U.S. Dollar', 'DXY / UUP', 'DOWN / MIXED', 'MIXED', 'UP', 'UP', 'UP initially', 'Direct', 'This is itself the USD signal'),
    ('Gold', 'GLD', 'UP', 'MIXED', 'MIXED', 'MIXED / UP', 'UP after liquidation', 'Safe haven', 'Gold often benefits after acute liquidation passes'),
    ('Silver', 'SLV', 'UP', 'MIXED', 'MIXED / DOWN', 'DOWN / MIXED', 'MIXED', 'Growth / USD', 'Sensitive to USD and industrial-growth expectations'),
    ('Broad Commodities', 'DBC', 'UP', 'MIXED', 'UP / MIXED', 'DOWN', 'DOWN', 'USD inverse', 'Broad commodities often face headwind from stronger USD'),
    ('Oil', 'USO / CL', 'UP', 'MIXED', 'UP / MIXED', 'DOWN', 'DOWN', 'USD / growth', 'Strong USD and growth fear often weigh on oil'),
    ('REITs', 'VNQ', 'UP', 'MIXED', 'DOWN', 'DOWN', 'MIXED / UP', 'Rates / USD', 'Sensitive to yields and global funding'),
    ('Utilities', 'XLU', 'UP', 'MIXED', 'DOWN / MIXED', 'MIXED', 'UP', 'Defensive', 'Often relative outperformer in risk-off regimes'),
    ('Consumer Staples', 'XLP', 'UP', 'MIXED', 'MIXED', 'RELATIVE UP', 'RELATIVE UP', 'Defensive', 'Often relative outperformer'),
    ('Financials', 'XLF', 'UP', 'MIXED', 'MIXED', 'DOWN STRONG', 'DOWN', 'Funding', 'Credit/funding stress is negative'),
    ('Bitcoin', 'BTC', 'UP STRONG', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN initially', 'Carry / liquidity', 'Very sensitive to carry unwind and USD liquidity'),
    ('Crypto ex-BTC', 'ETH / Altcoins', 'UP STRONG', 'MIXED', 'DOWN STRONG', 'DOWN STRONG', 'DOWN STRONG', 'Carry / liquidity', 'Usually even more sensitive than BTC'),
    ('Emerging-Market Stocks', 'EEM', 'UP', 'MIXED', 'DOWN', 'DOWN', 'DOWN', 'USD / China', 'Strong USD/CNY weakness often negative'),
    ('Emerging-Market Bonds', 'EMB', 'UP', 'MIXED', 'DOWN', 'DOWN STRONG', 'DOWN', 'USD funding', 'Dollar tightening can pressure EM debt'),
]

DIRECTION_RANK = {
    'UP STRONG': 2, 'UP': 1, 'UP / MIXED': 0.5, 'MIXED / UP': 0.5, 'UP after liquidation': 0.5, 'UP initially': 0.5,
    'RELATIVE UP': 0.5, 'MIXED': 0, 'MIXED / DOWN': -0.5, 'DOWN / MIXED': -0.5, 'DOWN initially': -0.5,
    'DOWN': -1, 'DOWN STRONG': -2,
}


def build_asset_outlook(regime):
    col_map = {
        'Liquidity Expansion': 2, 'Neutral / Balanced': 3, 'Inflationary Tightening': 4,
        'Funding / Credit Stress': 5, 'General Tightening': 5, 'Deflationary / Funding Crisis': 6,
    }
    col = col_map.get(regime)
    rows = []
    for entry in ASSET_TABLE:
        name, ticker = entry[0], entry[1]
        directions = entry[2:7]
        fx_transmission, interpretation = entry[7], entry[8]
        direction = directions[col - 2] if col else None
        rows.append({
            'name': name, 'ticker': ticker, 'likely_direction': direction,
            'fx_transmission': fx_transmission, 'interpretation': interpretation,
            'direction_rank': DIRECTION_RANK.get(direction) if direction else None,
        })
    favored = sorted([r for r in rows if r['direction_rank'] is not None], key=lambda r: -r['direction_rank'])
    return {
        'regime': regime,
        'assets': rows,
        'most_favored': [r['name'] for r in favored[:5] if r['direction_rank'] > 0],
        'least_favored': [r['name'] for r in favored[-5:] if r['direction_rank'] < 0][::-1],
        'note': 'Regime-conditioned historical tendencies from the source workbook, not guaranteed forecasts. "Relative UP" means the asset may still decline but has often held up better than broad equities.',
    }


# yfinance symbol for each asset's price history (used for the price-vs-score
# charts). Same tickers backtest_asset_outlook.py already uses successfully.
STOOQ_ASSET_MAP = {
    'S&P 500': 'SPY', 'Nasdaq / Growth': 'QQQ', 'Small Caps': 'IWM',
    'Value Stocks': 'VTV', 'High Dividend Stocks': 'VYM', 'High-Yield Bonds': 'HYG',
    'Investment-Grade Bonds': 'LQD', 'Short Treasuries / T-Bills': 'BIL',
    'Long Treasuries': 'TLT', 'U.S. Dollar': 'UUP', 'Gold': 'GLD', 'Silver': 'SLV',
    'Broad Commodities': 'DBC', 'Oil': 'USO', 'REITs': 'VNQ', 'Utilities': 'XLU',
    'Consumer Staples': 'XLP', 'Financials': 'XLF', 'Bitcoin': 'BTC-USD',
    'Crypto ex-BTC': 'ETH-USD', 'Emerging-Market Stocks': 'EEM', 'Emerging-Market Bonds': 'EMB',
}


def score_all_asof(S, E, date):
    """Recomputes every sub-score (not just Overall Risk) as of a historical
    date, using the same corrected/de-duplicated weights as compute_model()
    but sourcing every value via asof_at() instead of the live obs(). This
    is what lets each asset's chart show the risk category actually
    relevant to it (Credit for HY bonds, Rates for Treasuries, etc.)
    instead of one generic Overall Risk line for every asset."""
    L = lambda k: asof_at(S.get(k, []), date, 0)
    P1 = lambda k: asof_at(S.get(k, []), date, 1)
    P5 = lambda k: asof_at(S.get(k, []), date, 5)
    P20 = lambda k: asof_at(S.get(k, []), date, 20)
    EL = lambda k: asof_at(E.get(k, []), date, 0)
    EP5 = lambda k: asof_at(E.get(k, []), date, 5)
    EP20 = lambda k: asof_at(E.get(k, []), date, 20)

    hy, ig = L('BAMLH0A0HYM2'), L('BAMLC0A0CM')
    hyScore = percentile_score(hy, S.get('BAMLH0A0HYM2', []), asof_date=date)
    igScore = percentile_score(ig, S.get('BAMLC0A0CM', []), asof_date=date)

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

    vix, vixp5 = L('VIXCLS'), P5('VIXCLS')
    vixScore = clamp((vix - 12) * 3.2, 0, 100) if vix is not None else None
    vixChgPct = (vix/vixp5 - 1)*100 if None not in (vix, vixp5) and vixp5 else None
    vixTermProxy = clamp(50 + vixChgPct*4, 0, 100) if vixChgPct is not None else None

    y2Score = percentile_score(dgs2, S.get('DGS2', []), asof_date=date)
    y10Score = percentile_score(dgs10, S.get('DGS10', []), asof_date=date)

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

    spyL, spyP5, spyP20 = EL('SPY'), EP5('SPY'), EP20('SPY')
    rspL, rspP5, rspP20 = EL('RSP'), EP5('RSP'), EP20('RSP')
    breadth5D = breadth20D = None
    if None not in (spyL, spyP5, rspL, rspP5) and spyP5 and rspP5:
        breadth5D = ((rspL/rspP5)/(spyL/spyP5)-1)*100
    if None not in (spyL, spyP20, rspL, rspP20) and spyP20 and rspP20:
        breadth20D = ((rspL/rspP20)/(spyL/spyP20)-1)*100
    breadthStress = clamp(50-(breadth5D*10+breadth20D*5),0,100) if None not in (breadth5D,breadth20D) else None
    participationMomentum = clamp(50-(breadth5D*15+breadth20D*10),0,100) if None not in (breadth5D,breadth20D) else None

    credit_rows = [(hyScore, .13), (igScore, .07)]
    liquidity_rows = [(repoScore, .12), (dxyScore, .05), (fedBsScore, .03), (reservesScore, .04), (tgaScore, .04), (liqFlowComposite, .08)]
    rates_rows = [(treasuryVolStress, .07), (y2Score, .05), (y10Score, .04)]
    market_rows = [(vixScore, .05), (vixTermProxy, .05), (nfciScore, .04), (breadthStress, .05), (participationMomentum, .03), (econSurpriseScore, .03), (inflationLaborScore, .03)]

    def cat_avg(rows):
        valid = [(s, w) for s, w in rows if s is not None]
        if not valid:
            return None
        wsum = sum(w for _, w in valid)
        return sum(s*w for s, w in valid) / wsum if wsum > 0 else None

    liquidity_score = cat_avg(liquidity_rows)
    credit_score = cat_avg(credit_rows)
    rates_score = cat_avg(rates_rows)
    market_score = cat_avg(market_rows)

    fedExpScore = 0.6*y2Score + 0.4*clamp(50+sofrIorbBps*4,0,100) if None not in (y2Score, sofrIorbBps) else None
    inflationary_pressure = None
    if None not in (rates_score, fedExpScore, dxyScore):
        inflationary_pressure = clamp(0.45*rates_score + 0.3*fedExpScore + 0.25*dxyScore, 0, 100)

    def fx_leg(sid, invert):
        c, d, e, f = L(sid), P1(sid), P5(sid), P20(sid)
        if None in (c, d, e, f) or not d or not e or not f:
            return None
        g, h, i = c/d-1, c/e-1, c/f-1
        raw = (50-250*g-120*h-60*i) if invert else (50+250*g+120*h+60*i)
        return clamp(raw, 0, 100)

    fx_legs = [fx_leg('DEXJPUS', True), fx_leg('DEXUSEU', True), fx_leg('DEXCHUS', False),
               fx_leg('DEXSZUS', True), fx_leg('DEXUSAL', True), fx_leg('DTWEXBGS', False)]
    fx_stress = None if None in fx_legs else (0.3*fx_legs[0]+0.15*fx_legs[1]+0.2*fx_legs[2]+0.1*fx_legs[3]+0.1*fx_legs[4]+0.15*fx_legs[5])

    weighted_scores = [
        (hyScore, .13), (igScore, .07), (repoScore, .12), (treasuryVolStress, .07),
        (vixScore, .05), (vixTermProxy, .05), (y2Score, .05), (y10Score, .04),
        (dxyScore, .05), (fedBsScore, .03), (reservesScore, .04), (tgaScore, .04),
        (nfciScore, .04), (breadthStress, .05), (participationMomentum, .03),
        (econSurpriseScore, .03), (inflationLaborScore, .03), (liqFlowComposite, .08),
    ]
    contributing = [(s, w) for s, w in weighted_scores if s is not None]
    overall_risk = sum(s*w for s, w in contributing) if contributing else None

    return {
        'overall_risk': round(overall_risk, 2) if overall_risk is not None else None,
        'Liquidity': round(liquidity_score, 2) if liquidity_score is not None else None,
        'Credit': round(credit_score, 2) if credit_score is not None else None,
        'Rates': round(rates_score, 2) if rates_score is not None else None,
        'Market / Macro': round(market_score, 2) if market_score is not None else None,
        'Inflationary Pressure': round(inflationary_pressure, 2) if inflationary_pressure is not None else None,
        'FX Stress': round(fx_stress, 2) if fx_stress is not None else None,
    }


# Which risk sub-score is most relevant to each asset, derived from each
# asset's own "why it moves" column in ASSET_TABLE above. This is a
# judgment call, not a precise science — the point is "more relevant than
# always showing Overall Risk for everything," not a claim of precision.
ASSET_RISK_MAP = {
    'S&P 500': 'Market / Macro', 'Nasdaq / Growth': 'Liquidity', 'Small Caps': 'Liquidity',
    'Value Stocks': 'Market / Macro', 'High Dividend Stocks': 'Market / Macro',
    'High-Yield Bonds': 'Credit', 'Investment-Grade Bonds': 'Credit',
    'Short Treasuries / T-Bills': 'Rates', 'Long Treasuries': 'Rates',
    'U.S. Dollar': 'FX Stress', 'Gold': 'Inflationary Pressure', 'Silver': 'Inflationary Pressure',
    'Broad Commodities': 'Inflationary Pressure', 'Oil': 'Inflationary Pressure',
    'REITs': 'Rates', 'Utilities': 'Market / Macro', 'Consumer Staples': 'Market / Macro',
    'Financials': 'Credit', 'Bitcoin': 'Liquidity', 'Crypto ex-BTC': 'Liquidity',
    'Emerging-Market Stocks': 'FX Stress', 'Emerging-Market Bonds': 'Liquidity',
}


def main():
    print('Fetching FRED series...')
    S = {}
    for sid in FRED_SERIES:
        arr = fetch_fred_series(sid)
        S[sid] = arr
        print(f'  {sid}: {len(arr)} obs' if arr else f'  {sid}: FAILED')
        time.sleep(0.5)  # small gap between requests — some providers rate-limit
                          # or briefly block bursts of rapid automated traffic,
                          # which is a likely cause of the all-requests-timeout
                          # pattern seen from shared CI runner IPs

    fred_success_count = sum(1 for arr in S.values() if arr)
    print(f'FRED fetch summary: {fred_success_count}/{len(FRED_SERIES)} series succeeded')

    # Guard: if the vast majority of requests failed, this is almost
    # certainly a transient network problem on the runner (seen in
    # practice: every single request across two unrelated domains timing
    # out at once), not real data unavailability. Refuse to overwrite the
    # last known-good model_output.json with an all-null result — better
    # to leave the dashboard showing slightly-stale-but-real data than
    # blank it out. The workflow step fails (non-zero exit), so the
    # "Commit updated output" step never runs and nothing gets pushed.
    MIN_SUCCESS_FRACTION = 0.5
    if fred_success_count < len(FRED_SERIES) * MIN_SUCCESS_FRACTION:
        print(f'ERROR: only {fred_success_count}/{len(FRED_SERIES)} FRED series succeeded '
              f'(need at least {MIN_SUCCESS_FRACTION*100:.0f}%). Likely a transient network '
              f'issue on this run. Aborting WITHOUT writing/committing model_output.json, '
              f'so the last good data stays live. Will retry on the next scheduled run.',
              file=sys.stderr)
        sys.exit(1)

    print('Fetching equity/asset price data (yfinance, single bulk call)...')
    all_yf_tickers = sorted(set(list(STOOQ_TICKERS.values()) + list(STOOQ_ASSET_MAP.values())))
    yf_data = fetch_yfinance_bulk(all_yf_tickers, days_back=220)

    E = {}
    for name, ticker in STOOQ_TICKERS.items():
        arr = yf_data.get(ticker, [])
        E[name] = arr
        print(f'  {name} ({ticker}): {len(arr)} obs' if arr else f'  {name} ({ticker}): FAILED')

    print('Computing model...')
    model = compute_model(S, E)

    print('Building per-asset price history for the price-vs-score charts...')
    asset_price_history = {}
    for name, symbol in STOOQ_ASSET_MAP.items():
        arr = yf_data.get(symbol, [])
        asset_price_history[name] = arr[-180:] if arr else []
        print(f'  {name} ({symbol}): {len(asset_price_history[name])} obs' if arr else f'  {name} ({symbol}): FAILED')

    print('Reconstructing full risk-score history (~180 days, every metric, sampled every 3 days)...')
    today = datetime.now(timezone.utc).date()
    risk_history = []
    for i in range(180, -1, -3):
        d = (today - timedelta(days=i)).isoformat()
        scores = score_all_asof(S, E, d)
        if scores['overall_risk'] is not None:
            risk_history.append({'date': d, **scores})
    # always include the live figure as the most recent point, even if the
    # sampling loop's last step landed a day or two short of today
    if model['overall_risk'] is not None:
        live_point = {'date': today.isoformat(), 'overall_risk': round(model['overall_risk'], 2)}
        for cat in ['Liquidity', 'Credit', 'Rates', 'Market / Macro']:
            v = model['category_scores'].get(cat)
            live_point[cat] = round(v, 2) if v is not None else None
        live_point['Inflationary Pressure'] = round(model['inflationary_pressure'], 2) if model['inflationary_pressure'] is not None else None
        live_point['FX Stress'] = round(model['fx_stress'], 2) if model['fx_stress'] is not None else None
        risk_history.append(live_point)
    print(f'  {len(risk_history)} risk-history points reconstructed (overall + 6 sub-metrics each)')

    model['asset_price_history'] = asset_price_history
    model['risk_history'] = risk_history
    model['asset_risk_map'] = ASSET_RISK_MAP
    model['price_history_note'] = ('Daily closing prices and a daily-resolution reconstruction of the risk '
                                    'scores, both refreshed on this 15-minute schedule. Each asset is charted '
                                    'against the risk sub-metric most relevant to it (e.g. Credit for HY bonds, '
                                    'Rates for Treasuries, FX Stress for the Dollar), not a single generic '
                                    'Overall Risk line for everything. "Real-time" here means "as of the latest '
                                    '15-minute refresh, using the latest available daily close" — not intraday tick data.')

    with open('model_output.json', 'w') as f:
        json.dump(model, f, indent=2)

    print(f"Done. Overall risk: {model['overall_risk']}, regime: {model['regime']}")


if __name__ == '__main__':
    main()
