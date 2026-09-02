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


def fetch_fred_series(series_id, days_back=4000):
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


# --- FX stress scale -------------------------------------------------------
# Each FX leg below is built as `50 + momentum`, so a market where nothing
# moved scores exactly 50, and only readings ABOVE 50 mean movement in the
# stress direction. The original composite averaged the raw legs, which
# parked the whole measure at ~50 whenever FX was calm. Two consequences:
# the dashboard's shared 0-100 risk colour ramp painted a dead-quiet FX
# market orange as "Elevated", and the regime gates (65/70) needed roughly
# 2.7% per day sustained across all six currencies to trigger — i.e. never.
#
# Fix: take each leg's EXCESS over 50, so calm scores 0 and legs moving the
# benign way contribute nothing instead of masking a leg that is genuinely
# stressed, then scale onto the same 0-100 axis every other indicator uses.
# Raise FX_GAIN to make the reading more sensitive.
#
# NOTE: this must stay in step with the same constants in the dashboard's
# inline script. The dashboard prefers this file's model_output.json and
# only computes in-browser as a fallback, so a mismatch shows up as the
# tile silently reverting to the old ~50 reading.
FX_WEIGHTS = {'jpy': .30, 'eur': .15, 'cny': .20, 'chf': .10, 'aud': .10, 'dxy': .15}
FX_GAIN = 5              # ~1.4%/day across all six sustained -> ~50

# Regime gates, rebased for the scale above. The old values (45/55/65/70)
# were written for a scale centred on 50; on a scale where calm is 0 they
# would mean "never trigger" and "always calm" respectively.
FX_CRISIS, FX_STRESS_GATE, FX_CONTAINED, FX_CALM = 55, 50, 35, 20


def fx_composite(jpy, eur, cny, chf, aud, dxy):
    """Weighted blend of each leg's stress-direction excess over 50."""
    legs = (jpy, eur, cny, chf, aud, dxy)
    if None in legs:
        return None
    ex = lambda v: max(0.0, v - 50.0)
    return clamp(FX_GAIN * (
        FX_WEIGHTS['jpy'] * ex(jpy) + FX_WEIGHTS['eur'] * ex(eur)
        + FX_WEIGHTS['cny'] * ex(cny) + FX_WEIGHTS['chf'] * ex(chf)
        + FX_WEIGHTS['aud'] * ex(aud) + FX_WEIGHTS['dxy'] * ex(dxy)), 0, 100)


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


def momentum_percentile_score(series, asof_date=None, window=500, roc_period=20, invert=False):
    """Percentile rank of the metric's recent `roc_period`-observation
    CHANGE within its own trailing `window` of such changes — a different
    question from percentile_score() above. That function asks "is this
    unusually ELEVATED right now?" (found, via calibration against real
    HYG/LQD/TLT/BIL/QQQ/SPY forward returns, to behave mostly like a
    mean-reversion signal). This one asks "is this moving unusually FAST
    right now?" — tested and found to be a genuine continuation-style
    signal for Bank Reserves and NFCI specifically (see
    sensitivity_calibration.json): rapid recent moves in those two
    predicted the SAME-DIRECTION follow-through in the mapped asset,
    not a bounce-back.

    `invert` follows the same convention as percentile_score(): pass
    invert=True when a bigger recent INCREASE means LESS stress in this
    scoring system's convention (as with Bank Reserves — rising reserves
    is calmer, not more stressed), so the ranking flips accordingly."""
    if not series:
        return None
    if asof_date is not None:
        pool_raw = [p['value'] for p in series if p['date'] <= asof_date]
    else:
        pool_raw = [p['value'] for p in series]
    if len(pool_raw) < roc_period + 30:
        return None
    roc_series = [pool_raw[i] - pool_raw[i - roc_period] for i in range(roc_period, len(pool_raw))]
    if len(roc_series) < 30:
        return None
    current_roc = roc_series[-1]
    pool = roc_series[-window:]
    if len(pool) < 30:
        return None
    pool_sorted = sorted(pool)
    idx = bisect.bisect_left(pool_sorted, current_roc)
    pct = idx / len(pool_sorted) * 100
    return round((100 - pct) if invert else pct, 2)


def series_snapshot(S):
    """Every raw FRED series with the exact observations the formulas read:
    the latest value, plus the 1 / 5 / 20-observation lags the momentum and
    change calculations use, each with its own date. This is what makes the
    dashboard auditable — you can check any score by hand against the same
    numbers the model saw, and spot a stale or short series immediately."""
    out = {}
    for sid, arr in S.items():
        if not arr:
            out[sid] = {'latest': None, 'date': None, 'obs': 0}
            continue

        def at(b):
            i = len(arr) - 1 - b
            return arr[i] if i >= 0 else None

        latest, p1, p5, p20 = at(0), at(1), at(5), at(20)
        vals = [p['value'] for p in arr]
        out[sid] = {
            'latest': latest['value'], 'date': latest['date'], 'obs': len(arr),
            'prev_1': p1['value'] if p1 else None, 'prev_1_date': p1['date'] if p1 else None,
            'prev_5': p5['value'] if p5 else None, 'prev_5_date': p5['date'] if p5 else None,
            'prev_20': p20['value'] if p20 else None, 'prev_20_date': p20['date'] if p20 else None,
            'min': min(vals), 'max': max(vals),
            'first_date': arr[0]['date'],
        }
    return out


def compute_model(S, E):
    """S = dict of FRED series arrays, E = dict of equity series arrays (SPY, RSP)."""
    L = lambda k: obs(S.get(k), 0)
    P1 = lambda k: obs(S.get(k), 1)
    P5 = lambda k: obs(S.get(k), 5)
    P20 = lambda k: obs(S.get(k), 20)

    hy, ig = L('BAMLH0A0HYM2'), L('BAMLC0A0CM')
    # window=750 calibrated against real HYG/LQD forward returns
    # (see calibrate_sensitivity.py / sensitivity_calibration.json)
    hyScore = percentile_score(hy, S.get('BAMLH0A0HYM2', []), window=750)
    igScore = percentile_score(ig, S.get('BAMLC0A0CM', []), window=750)

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

    # window=1000 calibrated against real TLT/BIL forward returns
    y2Score = percentile_score(dgs2, S.get('DGS2', []), window=1000)
    y10Score = percentile_score(dgs10, S.get('DGS10', []), window=1000)
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
    # calibrated: rapid Fed balance-sheet EXPANSION preceded QQQ gains (r=+0.23 on raw
    # momentum, n=3319) — inverted so "balance sheet growing fast" scores LOW (calm),
    # matching this metric's existing "bigger balance sheet = less stress" convention
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), window=120, invert=True)
    # calibrated: rapid reserve BUILD-UPs preceded QQQ gains (r=+0.41 on raw momentum,
    # n=3319) — inverted so "reserves growing fast" scores LOW (calm), matching this
    # metric's existing "more reserves = less stress" convention
    reservesScore = momentum_percentile_score(S.get('WRESBAL', []), window=60, invert=True)
    tgaScore = clamp(20 + (wtregen/1000 - 500) / 10, 0, 100) if wtregen is not None else None

    nfci = L('NFCI')
    # calibrated: rapid NFCI TIGHTENING preceded SPY weakness (r=-0.27, n=3317) —
    # already the right direction for this metric's "higher NFCI = more stress"
    # convention, no inversion needed
    nfciScore = momentum_percentile_score(S.get('NFCI', []), window=500)

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
    # RESCALED — see fx_composite() and the FX scale notes near the top.
    # A market with no FX movement now scores 0 here, not 50.
    fxStress = fx_composite(jpyScore, eurScore, cnyScore, chfScore, audScore, dxyFxScore)

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

    # Helpers that attach the actual observations behind each indicator, so
    # every score on the dashboard can be checked by hand against the same
    # numbers the model read.
    def raw(sid, label):
        arr = S.get(sid) or []
        if not arr:
            return {'label': label, 'series': sid, 'value': None, 'date': None}
        return {'label': label, 'series': sid, 'value': arr[-1]['value'], 'date': arr[-1]['date']}

    def lag(sid, back, label):
        arr = S.get(sid) or []
        i = len(arr) - 1 - back
        if i < 0:
            return {'label': label, 'series': sid, 'value': None, 'date': None}
        return {'label': label, 'series': sid, 'value': arr[i]['value'], 'date': arr[i]['date']}

    def eq(ticker, value, label):
        return {'label': label, 'series': ticker, 'value': value, 'date': None}

    def calc(label, value, units):
        return {'label': label, 'value': value, 'units': units, 'derived': True}

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
        {'name': 'HY Credit Spreads', 'category': 'Credit', 'weight': .13, 'reading': hy, 'units': 'bps', 'score': hyScore,
         'formula': 'Percentile rank of today\u2019s spread within its own trailing 750 observations. 100 = widest in that window.',
         'inputs': [raw('BAMLH0A0HYM2', 'ICE BofA US High Yield option-adjusted spread')]},
        {'name': 'Investment-Grade Spreads', 'category': 'Credit', 'weight': .07, 'reading': ig, 'units': 'bps', 'score': igScore,
         'formula': 'Percentile rank within its own trailing 750 observations.',
         'inputs': [raw('BAMLC0A0CM', 'ICE BofA US Corporate option-adjusted spread')]},
        {'name': 'SOFR\u2013IORB Spread', 'category': 'Liquidity', 'weight': 0, 'reading': sofrIorbBps, 'units': 'bps', 'score': sofrIorbScore, 'redundant': 'folded into Repo-Market Stress (45% of that composite) \u2014 weight moved there to avoid double-counting',
         'formula': 'clamp(50 + (SOFR \u2212 IORB in bps) \u00d7 4, 0, 100)',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps')]},
        {'name': 'Repo-Market Stress', 'category': 'Liquidity', 'weight': .12, 'reading': repoScore, 'units': '0\u2013100', 'score': repoScore,
         'formula': '0.45 \u00d7 clamp(20 + (SOFR\u2212IORB)\u00d73) + 0.30 \u00d7 clamp(20 + (SOFR\u2212EFFR)\u00d74) + 0.25 \u00d7 clamp((SOFR 75th \u2212 25th pctile)\u00d74), each clamped 0\u2013100',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    raw('EFFR', 'Effective Fed Funds Rate'), raw('SOFR25', 'SOFR 25th percentile'), raw('SOFR75', 'SOFR 75th percentile'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps'), calc('SOFR \u2212 EFFR', sofrEffrBps, 'bps'),
                    calc('SOFR interquartile range', sofrIqrBps, 'bps')]},
        {'name': 'Treasury Vol Proxy (MOVE-style)', 'category': 'Rates', 'weight': .07, 'reading': treasuryVolStress, 'units': '0\u2013100', 'score': treasuryVolStress, 'source_note': 'synthetic proxy from 2Y/10Y 5-day moves \u2014 the real MOVE index isn\u2019t freely available via FRED',
         'formula': 'clamp((|2Y 5-day move in bps| \u00d7 0.6 + |10Y 5-day move in bps| \u00d7 0.4) \u00d7 2, 0, 100)',
         'inputs': [raw('DGS2', '2-year Treasury yield'), lag('DGS2', 5, '2-year, 5 sessions ago'),
                    raw('DGS10', '10-year Treasury yield'), lag('DGS10', 5, '10-year, 5 sessions ago'),
                    calc('|2Y 5-day move|', move2, 'bps'), calc('|10Y 5-day move|', move10, 'bps')]},
        {'name': 'VIX', 'category': 'Market / Macro', 'weight': .05, 'reading': vix, 'units': 'index', 'score': vixScore,
         'formula': 'clamp((VIX \u2212 12) \u00d7 3.2, 0, 100). Reads 0 at or below 12, saturates at about 43.',
         'inputs': [raw('VIXCLS', 'CBOE Volatility Index, close')]},
        {'name': 'VIX Momentum Proxy (5D)', 'category': 'Market / Macro', 'weight': .05, 'reading': vixTermProxy, 'units': '0\u2013100', 'score': vixTermProxy, 'source_note': 'transform of VIX\u2019s own 5-day change \u2014 not real futures term-structure data',
         'formula': 'clamp(50 + (VIX 5-day % change) \u00d7 4, 0, 100). Reads 50 when VIX is unchanged.',
         'inputs': [raw('VIXCLS', 'VIX today'), lag('VIXCLS', 5, 'VIX, 5 sessions ago'),
                    calc('5-day change', vixChgPct, '%')]},
        {'name': '2Y Treasury Yield', 'category': 'Rates', 'weight': .05, 'reading': dgs2, 'units': '%', 'score': y2Score,
         'formula': 'Percentile rank within its own trailing 1000 observations.',
         'inputs': [raw('DGS2', '2-year Treasury constant maturity')]},
        {'name': '10Y Treasury Yield', 'category': 'Rates', 'weight': .04, 'reading': dgs10, 'units': '%', 'score': y10Score,
         'formula': 'Percentile rank within its own trailing 1000 observations.',
         'inputs': [raw('DGS10', '10-year Treasury constant maturity')]},
        {'name': '2s10s Curve', 'category': 'Rates', 'weight': 0, 'reading': t2s10, 'units': 'pct pts', 'score': curveScore, 'redundant': 'derived from 2Y and 10Y, both already counted separately \u2014 weight moved to Credit',
         'formula': 'clamp(50 \u2212 20 \u00d7 (10Y \u2212 2Y), 0, 100). Reads 50 at a flat curve, rises as it inverts.',
         'inputs': [raw('T10Y2Y', '10-year minus 2-year spread')]},
        {'name': 'DXY / Broad Dollar', 'category': 'Liquidity', 'weight': .05, 'reading': dxy, 'units': 'index', 'score': dxyScore,
         'formula': 'clamp((index \u2212 100) \u00d7 2, 0, 100). Reads 0 at or below 100.',
         'inputs': [raw('DTWEXBGS', 'Nominal Broad US Dollar Index')]},
        {'name': 'Fed Balance Sheet', 'category': 'Liquidity', 'weight': .03, 'reading': walcl, 'units': '$mm', 'score': fedBsScore,
         'formula': 'Percentile rank of the 20-observation change within its own trailing 120, inverted \u2014 so fast expansion scores low (calm).',
         'inputs': [raw('WALCL', 'Total assets, all Federal Reserve banks'), lag('WALCL', 20, '20 weeks ago'),
                    calc('20-observation change', (walcl - walclP20) if None not in (walcl, walclP20) else None, '$mm')]},
        {'name': 'Bank Reserves', 'category': 'Liquidity', 'weight': .04, 'reading': wresbal, 'units': '$mm', 'score': reservesScore, 'flag': 'unit-corrected (\u00f71000 to billions) vs. original workbook \u2014 see notes',
         'formula': 'Percentile rank of the 20-observation change within its own trailing 60, inverted \u2014 so fast reserve build-up scores low (calm).',
         'inputs': [raw('WRESBAL', 'Reserve balances held at Federal Reserve banks'), lag('WRESBAL', 20, '20 weeks ago'),
                    calc('20-observation change', (wresbal - wresbalP20) if None not in (wresbal, wresbalP20) else None, '$mm')]},
        {'name': 'Treasury General Account', 'category': 'Liquidity', 'weight': .04, 'reading': wtregen, 'units': '$mm', 'score': tgaScore, 'flag': 'unit-corrected (\u00f71000 to billions) vs. original workbook \u2014 see notes',
         'formula': 'clamp(20 + (TGA in $bn \u2212 500) \u00f7 10, 0, 100). The \u00f71000 converts FRED\u2019s $mm to the $bn the threshold assumes.',
         'inputs': [raw('WTREGEN', 'US Treasury general account balance'),
                    calc('converted to $bn', (wtregen/1000) if wtregen is not None else None, '$bn')]},
        {'name': 'Financial Conditions (NFCI)', 'category': 'Market / Macro', 'weight': .04, 'reading': nfci, 'units': 'index', 'score': nfciScore,
         'formula': 'Percentile rank of the 20-observation change within its own trailing 500 \u2014 fast tightening scores high.',
         'inputs': [raw('NFCI', 'Chicago Fed National Financial Conditions Index')]},
        {'name': 'S&P 500 Breadth', 'category': 'Market / Macro', 'weight': .05, 'reading': breadthStress, 'units': '0\u2013100', 'score': breadthStress, 'source_note': 'now live via Stooq SPY/RSP \u2014 unavailable in the original workbook',
         'formula': 'clamp(50 \u2212 (5-day RSP-vs-SPY spread \u00d7 10 + 20-day spread \u00d7 5), 0, 100). RSP is the equal-weight S&P, so RSP lagging SPY means a narrow market.',
         'inputs': [eq('SPY', spyL, 'S&P 500 ETF, last close'), eq('RSP', rspL, 'Equal-weight S&P ETF, last close'),
                    calc('5-day breadth spread', breadth5D, '%'), calc('20-day breadth spread', breadth20D, '%')]},
        {'name': 'Market Participation Momentum', 'category': 'Market / Macro', 'weight': .03, 'reading': participationMomentum, 'units': '0\u2013100', 'score': participationMomentum, 'source_note': 'now live via Stooq SPY/RSP \u2014 unavailable in the original workbook',
         'formula': 'clamp(50 \u2212 (5-day spread \u00d7 15 + 20-day spread \u00d7 10), 0, 100). Same inputs as Breadth, weighted harder toward the recent move.',
         'inputs': [eq('SPY', spyL, 'S&P 500 ETF, last close'), eq('RSP', rspL, 'Equal-weight S&P ETF, last close'),
                    calc('5-day breadth spread', breadth5D, '%'), calc('20-day breadth spread', breadth20D, '%')]},
        {'name': 'Economic Surprise', 'category': 'Market / Macro', 'weight': .03, 'reading': econSurpriseScore, 'units': '0\u2013100', 'score': econSurpriseScore,
         'formula': 'clamp(50 + (initial claims 5-week % change) \u00d7 5, 0, 100). Reads 50 when claims are flat.',
         'inputs': [raw('ICSA', 'Initial unemployment claims'), lag('ICSA', 5, '5 weeks ago'),
                    calc('5-week change', claimsChgPct, '%')]},
        {'name': 'Inflation & Labor Momentum', 'category': 'Market / Macro', 'weight': .03, 'reading': inflationLaborScore, 'units': '0\u2013100', 'score': inflationLaborScore,
         'formula': 'clamp(50 + ((core CPI m/m \u00d7 0.5 + core PCE m/m \u00d7 0.5) \u2212 0.2) \u00d7 200, 0, 100). Reads 50 at 0.2% monthly, roughly the 2% annual target.',
         'inputs': [raw('CPILFESL', 'Core CPI index'), lag('CPILFESL', 1, 'Core CPI, prior month'),
                    raw('PCEPILFE', 'Core PCE index'), lag('PCEPILFE', 1, 'Core PCE, prior month'),
                    calc('core CPI m/m', coreCpiMo, '%'), calc('core PCE m/m', corePceMo, '%')]},
        {'name': 'Fed Expectations', 'category': 'Rates', 'weight': 0, 'reading': fedExpScore, 'units': '0\u2013100', 'score': fedExpScore, 'redundant': 'derived entirely from the 2Y Yield and SOFR-IORB scores, both already counted separately \u2014 weight moved to Credit',
         'formula': '0.6 \u00d7 (2Y yield score) + 0.4 \u00d7 (SOFR\u2013IORB score)',
         'inputs': [calc('2Y yield score', y2Score, '0\u2013100'), calc('SOFR\u2013IORB score', sofrIorbScore, '0\u2013100')]},
        {'name': 'Liquidity Flow Stress', 'category': 'Liquidity', 'weight': .08, 'reading': liqFlowComposite, 'units': '0\u2013100', 'score': liqFlowComposite,
         'formula': 'Net injection = \u0394Fed assets + \u0394reserves \u2212 \u0394TGA, over 5 and 20 observations. Each maps to clamp(50 \u2212 net \u00f7 scale), blended 65% short / 35% long. A drain scores high.',
         'inputs': [raw('WALCL', 'Fed total assets'), raw('WRESBAL', 'Bank reserves'), raw('WTREGEN', 'Treasury general account'),
                    calc('net injection, 5 obs', netImp5, '$mm'), calc('net injection, 20 obs', netImp20, '$mm')]},
    ]

    # ---- worked arithmetic ------------------------------------------------
    # The same numbers listed in each indicator's 'inputs', substituted into
    # its formula and carried through to the score. This is what makes a
    # reading checkable rather than merely sourced: you can follow every line
    # with a calculator and land on the number the dashboard shows.
    def f(v, dp=2):
        return '\u2014' if v is None else f'{v:,.{dp}f}'

    def g(v):
        """Readable at any magnitude: reserves are in the millions, spreads in
        single digits, and '3.686e+06' helps nobody check arithmetic."""
        if v is None:
            return '\u2014'
        a = abs(v)
        if a >= 1000:
            return f'{v:,.0f}'
        if a >= 1:
            return f'{v:,.3f}'.rstrip('0').rstrip('.')
        return f'{v:,.5f}'.rstrip('0').rstrip('.')

    def pct_steps(current, sid, window, score, invert=False):
        """Explains a percentile_score() result against its actual pool."""
        arr = S.get(sid) or []
        pool = [q['value'] for q in arr][-window:]
        if current is None or score is None or len(pool) < 30:
            return []
        below = sum(1 for v in pool if v < current)
        out = [f'pool = last {len(pool)} observations of {sid}, ranging {g(min(pool))} to {g(max(pool))}',
               f'{below} of those {len(pool)} sit below the current {g(current)}',
               f'{below} \u00f7 {len(pool)} = {below/len(pool)*100:.2f} \u2192 score {score:.2f}']
        if invert:
            out[-1] = (f'{below} \u00f7 {len(pool)} = {below/len(pool)*100:.2f}, inverted '
                       f'(lower value = more stress): 100 \u2212 {below/len(pool)*100:.2f} = {score:.2f}')
        return out

    def roc_steps(sid, window, score, roc_period=20, invert=False):
        """Explains a momentum_percentile_score() result: rank of the recent
        change within the distribution of past changes over the same span."""
        arr = S.get(sid) or []
        vals = [q['value'] for q in arr]
        if score is None or len(vals) < roc_period + 30:
            return []
        rocs = [vals[i] - vals[i - roc_period] for i in range(roc_period, len(vals))]
        pool = rocs[-window:]
        cur = rocs[-1]
        below = sum(1 for v in pool if v < cur)
        out = [f'{roc_period}-observation change = {g(vals[-1])} \u2212 {g(vals[-1-roc_period])} = {g(cur)}',
               f'pool = the last {len(pool)} such changes, ranging {g(min(pool))} to {g(max(pool))}',
               f'{below} of {len(pool)} changes were smaller \u2192 {below/len(pool)*100:.2f}']
        out.append(f'inverted (faster growth = calmer): 100 \u2212 {below/len(pool)*100:.2f} = {score:.2f}'
                   if invert else f'score = {score:.2f}')
        return out

    step_map = {
        'HY Credit Spreads': pct_steps(hy, 'BAMLH0A0HYM2', 750, hyScore),
        'Investment-Grade Spreads': pct_steps(ig, 'BAMLC0A0CM', 750, igScore),
        '2Y Treasury Yield': pct_steps(dgs2, 'DGS2', 1000, y2Score),
        '10Y Treasury Yield': pct_steps(dgs10, 'DGS10', 1000, y10Score),
        'Fed Balance Sheet': roc_steps('WALCL', 120, fedBsScore, invert=True),
        'Bank Reserves': roc_steps('WRESBAL', 60, reservesScore, invert=True),
        'Financial Conditions (NFCI)': roc_steps('NFCI', 500, nfciScore),
    }

    if None not in (sofrIorbBps, sofrEffrBps, sofrIqrBps):
        l1 = clamp(20 + sofrIorbBps * 3, 0, 100)
        l2 = clamp(20 + sofrEffrBps * 4, 0, 100)
        l3 = clamp(sofrIqrBps * 4, 0, 100)
        step_map['Repo-Market Stress'] = [
            f'SOFR \u2212 IORB = {f(sofr,4)} \u2212 {f(iorb,4)} = {f(sofrIorbBps)}bp',
            f'SOFR \u2212 EFFR = {f(sofr,4)} \u2212 {f(effr,4)} = {f(sofrEffrBps)}bp',
            f'SOFR 75th \u2212 25th = {f(s75,4)} \u2212 {f(s25,4)} = {f(sofrIqrBps)}bp',
            f'leg 1: clamp(20 + {f(sofrIorbBps)} \u00d7 3) = {f(l1)}',
            f'leg 2: clamp(20 + {f(sofrEffrBps)} \u00d7 4) = {f(l2)}',
            f'leg 3: clamp({f(sofrIqrBps)} \u00d7 4) = {f(l3)}',
            f'0.45 \u00d7 {f(l1)} + 0.30 \u00d7 {f(l2)} + 0.25 \u00d7 {f(l3)} = {f(repoScore)}',
        ]
    if sofrIorbBps is not None:
        step_map['SOFR\u2013IORB Spread'] = [
            f'SOFR \u2212 IORB = {f(sofr,4)} \u2212 {f(iorb,4)} = {f(sofrIorbBps)}bp',
            f'clamp(50 + {f(sofrIorbBps)} \u00d7 4) = {f(sofrIorbScore)}',
        ]
    if None not in (move2, move10):
        step_map['Treasury Vol Proxy (MOVE-style)'] = [
            f'2Y moved {f(dgs2,4)} \u2212 {f(dgs2p5,4)} \u2192 |{f(move2)}|bp over 5 sessions',
            f'10Y moved {f(dgs10,4)} \u2212 {f(dgs10p5,4)} \u2192 |{f(move10)}|bp over 5 sessions',
            f'weighted: {f(move2)} \u00d7 0.6 + {f(move10)} \u00d7 0.4 = {f(move2*0.6 + move10*0.4)}',
            f'clamp({f(move2*0.6 + move10*0.4)} \u00d7 2) = {f(treasuryVolStress)}',
        ]
    if vix is not None:
        step_map['VIX'] = [f'clamp(({f(vix)} \u2212 12) \u00d7 3.2) = {f(vixScore)}']
    if vixChgPct is not None:
        step_map['VIX Momentum Proxy (5D)'] = [
            f'VIX {f(vix)} vs {f(vixp5)} five sessions ago = {f(vixChgPct)}%',
            f'clamp(50 + {f(vixChgPct)} \u00d7 4) = {f(vixTermProxy)}',
        ]
    if t2s10 is not None:
        step_map['2s10s Curve'] = [f'clamp(50 \u2212 20 \u00d7 {f(t2s10,4)}) = {f(curveScore)}']
    if dxy is not None:
        step_map['DXY / Broad Dollar'] = [f'clamp(({f(dxy,4)} \u2212 100) \u00d7 2) = {f(dxyScore)}']
    if wtregen is not None:
        step_map['Treasury General Account'] = [
            f'{f(wtregen,0)} $mm \u00f7 1000 = {f(wtregen/1000)} $bn',
            f'clamp(20 + ({f(wtregen/1000)} \u2212 500) \u00f7 10) = {f(tgaScore)}',
        ]
    if claimsChgPct is not None:
        step_map['Economic Surprise'] = [
            f'claims {f(icsa,0)} vs {f(icsaP5,0)} five weeks ago = {f(claimsChgPct)}%',
            f'clamp(50 + {f(claimsChgPct)} \u00d7 5) = {f(econSurpriseScore)}',
        ]
    if None not in (coreCpiMo, corePceMo):
        blend = coreCpiMo * 0.5 + corePceMo * 0.5
        step_map['Inflation & Labor Momentum'] = [
            f'core CPI {f(cpiCore,3)} vs {f(cpiCoreP1,3)} last month = {f(coreCpiMo,3)}% m/m',
            f'core PCE {f(pceCore,3)} vs {f(pceCoreP1,3)} last month = {f(corePceMo,3)}% m/m',
            f'blend = ({f(coreCpiMo,3)} + {f(corePceMo,3)}) \u00f7 2 = {f(blend,3)}%',
            f'clamp(50 + ({f(blend,3)} \u2212 0.2) \u00d7 200) = {f(inflationLaborScore)}',
        ]
    if fedExpScore is not None:
        step_map['Fed Expectations'] = [
            f'0.6 \u00d7 {f(y2Score)} (2Y score) + 0.4 \u00d7 {f(sofrIorbScore)} (SOFR\u2013IORB score) = {f(fedExpScore)}']
    if liqFlowComposite is not None:
        step_map['Liquidity Flow Stress'] = [
            f'5-obs: \u0394assets {f(dFed5,0)} + \u0394reserves {f(dRes5,0)} \u2212 \u0394TGA {f(dTga5,0)} = {f(netImp5,0)} $mm',
            f'20-obs: \u0394assets {f(dFed20,0)} + \u0394reserves {f(dRes20,0)} \u2212 \u0394TGA {f(dTga20,0)} = {f(netImp20,0)} $mm',
            f'short leg: clamp(50 \u2212 {f(netImp5,0)} \u00f7 10,000) = {f(liqFlow5)}',
            f'long leg: clamp(50 \u2212 {f(netImp20,0)} \u00f7 20,000) = {f(liqFlow20)}',
            f'0.65 \u00d7 {f(liqFlow5)} + 0.35 \u00d7 {f(liqFlow20)} = {f(liqFlowComposite)}',
        ]
    if None not in (breadth5D, breadth20D):
        step_map['S&P 500 Breadth'] = [
            f'RSP vs SPY over 5 sessions = {f(breadth5D,3)}%  (equal-weight minus cap-weight)',
            f'RSP vs SPY over 20 sessions = {f(breadth20D,3)}%',
            f'clamp(50 \u2212 ({f(breadth5D,3)} \u00d7 10 + {f(breadth20D,3)} \u00d7 5)) = {f(breadthStress)}',
        ]
        step_map['Market Participation Momentum'] = [
            f'same two spreads: {f(breadth5D,3)}% over 5, {f(breadth20D,3)}% over 20',
            f'clamp(50 \u2212 ({f(breadth5D,3)} \u00d7 15 + {f(breadth20D,3)} \u00d7 10)) = {f(participationMomentum)}',
        ]

    for ind in indicators:
        ind['steps'] = step_map.get(ind['name'], [])
        if ind['score'] is not None and ind['weight'] > 0:
            ind['steps'] = list(ind['steps']) + [
                f"contribution to overall risk: {ind['score']:.2f} \u00d7 {ind['weight']*100:.0f}% "
                f"= {ind['weight']*ind['score']:.2f}"]

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
        # FX gates use the rebased constants defined near the top of this file.
        if B5 >= 60 and B6 >= 60 and (B7 < 60 or K10 >= FX_CRISIS):
            regime = 'Deflationary / Funding Crisis'
        elif B5 >= 55 and B7 >= 60 and K8 >= 60:
            regime = 'Inflationary Tightening'
        elif (B5 >= 55 or B6 >= 55) and K10 >= FX_STRESS_GATE:
            regime = 'Funding / Credit Stress'
        elif B5 <= 30 and K10 < FX_CALM:
            regime = 'Liquidity Expansion'
        elif B5 < 50 and K10 < FX_CONTAINED:
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
        'raw_series': series_snapshot(S),
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
    hyScore = percentile_score(hy, S.get('BAMLH0A0HYM2', []), asof_date=date, window=750)
    igScore = percentile_score(ig, S.get('BAMLC0A0CM', []), asof_date=date, window=750)

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

    y2Score = percentile_score(dgs2, S.get('DGS2', []), asof_date=date, window=1000)
    y10Score = percentile_score(dgs10, S.get('DGS10', []), asof_date=date, window=1000)

    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None

    walcl, wresbal, wtregen = L('WALCL'), L('WRESBAL'), L('WTREGEN')
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), asof_date=date, window=120, invert=True)
    reservesScore = momentum_percentile_score(S.get('WRESBAL', []), asof_date=date, window=60, invert=True)
    tgaScore = clamp(20 + (wtregen/1000 - 500) / 10, 0, 100) if wtregen is not None else None

    nfci = L('NFCI')
    nfciScore = momentum_percentile_score(S.get('NFCI', []), asof_date=date, window=500)

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
    # RESCALED — same fx_composite() the live path uses, so the historical
    # 'FX Stress' line on the per-asset charts stays on the same 0-100 axis
    # as the live tile. Leaving this on the old formula would have made the
    # chart and the tile silently disagree by ~50 points.
    fx_stress = fx_composite(*fx_legs)

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
        # tracked as its own field (not folded into the Liquidity blend) because
        # it's the one metric-asset pairing with a genuinely validated, real
        # out-of-sample relationship (see asset_signals.json) — BIL vs. this
        # exact score, not vs. the diluted 6-component Liquidity category
        'Fed Balance Sheet': round(fedBsScore, 2) if fedBsScore is not None else None,
    }


# Which risk sub-score is most relevant to each asset, derived from each
# asset's own "why it moves" column in ASSET_TABLE above. This is a
# judgment call, not a precise science — the point is "more relevant than
# always showing Overall Risk for everything," not a claim of precision.
# Exception: 'Short Treasuries / T-Bills' -> 'Fed Balance Sheet' is NOT a
# judgment call — it's the one pairing in this whole table with a real,
# validated out-of-sample relationship (train r=-0.84 n=98, test r=-0.74
# n=69; see derive_asset_signals.py / asset_signals.json). Every other
# entry here is illustrative grouping, not a backtested claim.
ASSET_RISK_MAP = {
    'S&P 500': 'Market / Macro', 'Nasdaq / Growth': 'Liquidity', 'Small Caps': 'Liquidity',
    'Value Stocks': 'Market / Macro', 'High Dividend Stocks': 'Market / Macro',
    'High-Yield Bonds': 'Credit', 'Investment-Grade Bonds': 'Credit',
    'Short Treasuries / T-Bills': 'Fed Balance Sheet', 'Long Treasuries': 'Rates',
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
        fedbs_indicator = next((i for i in model['indicators'] if i['name'] == 'Fed Balance Sheet'), None)
        live_point['Fed Balance Sheet'] = round(fedbs_indicator['score'], 2) if fedbs_indicator and fedbs_indicator['score'] is not None else None
        risk_history.append(live_point)
    print(f'  {len(risk_history)} risk-history points reconstructed (overall + 6 sub-metrics each)')

    model['asset_price_history'] = asset_price_history
    model['risk_history'] = risk_history
    model['asset_risk_map'] = ASSET_RISK_MAP
    model['price_history_note'] = ('Daily closing prices and a daily-resolution reconstruction of the risk '
                                    'scores, both refreshed on this 15-minute schedule. Each asset is charted '
                                    'against a risk sub-metric grouping — but a rigorous out-of-sample test '
                                    '(train/test split, no regime bucket, one metric tested per asset '
                                    'independently) found a real, holding-up relationship for only 1 of 22 '
                                    'assets: Short Treasuries / T-Bills vs. Fed Balance Sheet (marked with a '
                                    '\u2713 badge below). Every other pairing here is an illustrative grouping, '
                                    'not a validated predictor. "Real-time" here means "as of the latest '
                                    '15-minute refresh, using the latest available daily close" — not intraday tick data.')

    with open('model_output.json', 'w') as f:
        json.dump(model, f, indent=2)

    print(f"Done. Overall risk: {model['overall_risk']}, regime: {model['regime']}, FX stress: "
          f"{None if model['fx_stress'] is None else round(model['fx_stress'], 1)} (0 = no FX movement)")


if __name__ == '__main__':
    main()
