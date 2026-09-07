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

# Dropped from the original list: PAYEMS, CPIAUCSL and PCEPI were fetched
# every run and never referenced by any formula; T10Y2Y is superseded by
# T10Y3M, which has the better recession record. Added: DFII10 (the real
# 10-year yield, the most connected variable in macro and previously absent),
# RRPONTSYD (reverse repo — without it the net-liquidity figure was missing
# a facility that held over $2trn at its peak), and T10Y3M.
FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'DFII10', 'T10Y3M', 'DTWEXBGS', 'WALCL', 'WRESBAL', 'WTREGEN',
    'RRPONTSYD', 'NFCI', 'EFFR', 'SOFR25', 'SOFR75', 'DEXJPUS', 'DEXUSEU',
    'DEXCHUS', 'DEXSZUS', 'DEXUSAL',
]

# --- Real yield differentials -------------------------------------------
# Market-priced real yields (like the US TIPS series DFII10) barely exist in
# free daily form outside the US: the Bank of England publishes a daily real
# gilt curve but only as zipped spreadsheets, and Germany and Japan each use a
# different portal and format again.
#
# So these are EX-POST real yields: nominal long-term government yield minus
# year-over-year CPI. That is inflation that already happened, not the market's
# expectation, which is what a linker prices. It is the standard fallback when
# index-linked data isn't available, and it is fine for comparing levels and
# direction across countries — but it is not the same quantity as DFII10 and
# should not be read as though it were.
#
# Monthly, with roughly a month's publication lag. Deliberately kept OUT of the
# risk score: different question, different cadence, and the model was just
# simplified to remove redundancy.
#
# 'fx' quote conventions differ on FRED. 'fx_inverted' means the series is
# foreign-currency-per-dollar (rising = weaker foreign currency); otherwise it
# is dollars-per-foreign-unit (rising = stronger foreign currency).
REAL_YIELD_MARKETS = {
    'US': {'name': 'United States', 'ccy': 'USD',
           'yield': 'IRLTLT01USM156N', 'cpi': 'CPALTT01USM659N', 'fx': None, 'fx_inverted': False},
    'DE': {'name': 'Germany', 'ccy': 'EUR',
           'yield': 'IRLTLT01DEM156N', 'cpi': 'CPALTT01DEM659N', 'fx': 'DEXUSEU', 'fx_inverted': False},
    'GB': {'name': 'United Kingdom', 'ccy': 'GBP',
           'yield': 'IRLTLT01GBM156N', 'cpi': 'CPALTT01GBM659N', 'fx': 'DEXUSUK', 'fx_inverted': False},
    'JP': {'name': 'Japan', 'ccy': 'JPY',
           'yield': 'IRLTLT01JPM156N', 'cpi': 'CPALTT01JPM659N', 'fx': 'DEXJPUS', 'fx_inverted': True},
    'CA': {'name': 'Canada', 'ccy': 'CAD',
           'yield': 'IRLTLT01CAM156N', 'cpi': 'CPALTT01CAM659N', 'fx': 'DEXCAUS', 'fx_inverted': True},
    'AU': {'name': 'Australia', 'ccy': 'AUD',
           'yield': 'IRLTLT01AUM156N', 'cpi': 'CPALTT01AUQ659N', 'fx': 'DEXUSAL', 'fx_inverted': False},
    'CH': {'name': 'Switzerland', 'ccy': 'CHF',
           'yield': 'IRLTLT01CHM156N', 'cpi': 'CPALTT01CHM659N', 'fx': 'DEXSZUS', 'fx_inverted': True},
    'NO': {'name': 'Norway', 'ccy': 'NOK',
           'yield': 'IRLTLT01NOM156N', 'cpi': 'CPALTT01NOM659N', 'fx': 'DEXNOUS', 'fx_inverted': True},
    'SE': {'name': 'Sweden', 'ccy': 'SEK',
           'yield': 'IRLTLT01SEM156N', 'cpi': 'CPALTT01SEM659N', 'fx': 'DEXSDUS', 'fx_inverted': True},
}


def build_real_yields(R):
    """R = dict of the optional series fetched for this panel. Any country
    whose yield or CPI series failed is dropped rather than shown as blank,
    and the caller reports which ones survived."""
    def latest(sid):
        arr = R.get(sid) or []
        return (arr[-1]['value'], arr[-1]['date']) if arr else (None, None)

    rows = {}
    for code, cfg in REAL_YIELD_MARKETS.items():
        nom, nom_d = latest(cfg['yield'])
        cpi, cpi_d = latest(cfg['cpi'])
        if nom is None or cpi is None:
            continue
        rows[code] = {
            'code': code, 'name': cfg['name'], 'ccy': cfg['ccy'],
            'nominal': round(nom, 3), 'nominal_date': nom_d,
            'cpi': round(cpi, 3), 'cpi_date': cpi_d,
            'real': round(nom - cpi, 3),
            'yield_series': cfg['yield'], 'cpi_series': cfg['cpi'],
        }

    us = rows.get('US')
    for code, r in rows.items():
        r['real_diff_vs_us'] = round(r['real'] - us['real'], 3) if us else None

        cfg = REAL_YIELD_MARKETS[code]
        r['fx_60d'] = None
        if cfg['fx']:
            arr = R.get(cfg['fx']) or []
            if len(arr) > 60:
                a, b = arr[-1]['value'], arr[-61]['value']
                if a and b:
                    chg = (a / b - 1) * 100
                    # express as the foreign currency's gain against the dollar
                    r['fx_60d'] = round(-chg if cfg['fx_inverted'] else chg, 2)
            r['fx_series'] = cfg['fx']
    return list(rows.values())


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


# ---------------------------------------------------------------------------
# SIMPLIFIED INDICATOR SET
#
# The previous model carried 18 weighted indicators, but several were reading
# the same series through different transforms and so were counted twice:
#
#   * WALCL / WRESBAL / WTREGEN carried 19% across four indicators — a levels
#     read (Fed Balance Sheet, Bank Reserves, TGA) and a flows read (Liquidity
#     Flow Stress) of one balance sheet. Worse, that flow formula ADDED
#     reserves to assets, when reserves are a liability of the same balance
#     sheet. These collapse into one Fed Net Liquidity indicator using the
#     conventional definition: assets minus TGA minus reverse repo.
#   * VIXCLS carried 10% across VIX and VIX Momentum — one series, two rows.
#     Merged into a single Market Volatility indicator blending level and
#     momentum.
#   * SPY/RSP carried 8% across S&P 500 Breadth and Market Participation
#     Momentum. Those two are the same two spreads with different linear
#     weights and correlate at 0.989 — one signal, billed twice. Merged.
#   * Nominal 2Y and 10Y were each weighted standalone AND inside the Treasury
#     Vol Proxy. Replaced by the real 10-year yield (the variable that actually
#     drives the dollar, gold and long duration) and the 10Y-3M curve.
#
# Result: 13 weighted indicators from 18, with no series feeding two weighted
# rows. Category totals are unchanged, so the headline score stays comparable.
# Two indicators are kept at zero weight for visibility only.
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Credit — 20%
    'HY Credit Spreads':            ('Credit', .13),
    'Investment-Grade Spreads':     ('Credit', .07),
    # Liquidity — 36%
    'Repo-Market Stress':           ('Liquidity', .12),
    'Fed Net Liquidity':            ('Liquidity', .19),
    'DXY / Broad Dollar':           ('Liquidity', .05),
    # Rates — 16%
    'Treasury Vol Proxy (MOVE-style)': ('Rates', .07),
    'Real 10-Year Yield':           ('Rates', .05),
    'Yield Curve (10Y \u2212 3M)':      ('Rates', .04),
    # Market / Macro — 28%
    'Market Volatility':            ('Market / Macro', .10),
    'Equity Breadth':               ('Market / Macro', .08),
    'Financial Conditions (NFCI)':  ('Market / Macro', .04),
    'Jobless Claims Momentum':      ('Market / Macro', .03),
    'Inflation Momentum':           ('Market / Macro', .03),
    # shown but not scored
    'SOFR\u2013IORB Spread':            ('Liquidity', 0),
    'Bank Reserves':                ('Liquidity', 0),
}


def regime_from(liquidity, credit, rates, inflationary_pressure, fx_stress):
    """The regime rules in ONE place. compute_model() and the backtest both
    call this, so a rule change can never leave the backtest scoring a model
    the dashboard no longer runs."""
    vals = (liquidity, credit, rates, inflationary_pressure, fx_stress)
    if any(v is None for v in vals):
        return 'Insufficient data'
    B5, B6, B7, K8, K10 = vals
    if B5 >= 60 and B6 >= 60 and (B7 < 60 or K10 >= FX_CRISIS):
        return 'Deflationary / Funding Crisis'
    if B5 >= 55 and B7 >= 60 and K8 >= 60:
        return 'Inflationary Tightening'
    if (B5 >= 55 or B6 >= 55) and K10 >= FX_STRESS_GATE:
        return 'Funding / Credit Stress'
    if B5 <= 30 and K10 < FX_CALM:
        return 'Liquidity Expansion'
    if B5 < 50 and K10 < FX_CONTAINED:
        return 'Neutral / Balanced'
    return 'General Tightening'


def net_liquidity_series(S):
    """Fed net liquidity = total assets \u2212 Treasury general account \u2212 reverse
    repo, the conventional measure of how many dollars are actually loose in
    the system. Built on WALCL's weekly dates, with the other two taken as of
    each of those dates, because the three publish on different schedules.

    RRPONTSYD is reported in $bn while WALCL and WTREGEN are in $mm, hence the
    \u00d71000. It also only begins in 2013; treated as zero before that, which is
    correct \u2014 the facility did not exist."""
    walcl = S.get('WALCL') or []
    tga_s = S.get('WTREGEN') or []
    rrp_s = S.get('RRPONTSYD') or []
    out = []
    for p in walcl:
        d = p['date']
        tga = asof_at(tga_s, d)
        if tga is None:
            continue
        rrp = asof_at(rrp_s, d)
        rrp = (rrp * 1000.0) if rrp is not None else 0.0
        out.append({'date': d, 'value': p['value'] - tga - rrp})
    return out


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
    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None


    wresbal = L('WRESBAL')
    walcl, wtregen = L('WALCL'), L('WTREGEN')
    rrp = L('RRPONTSYD')

    # One measure where there were four. Net liquidity = assets − TGA − RRP,
    # scored on how fast it is moving relative to its own recent history and
    # inverted, so rapid expansion reads calm. The old set scored the levels of
    # three components separately AND their combined flow, putting 19% of the
    # model on one balance sheet read two ways — and it added reserves to
    # assets, double-counting a liability against its own asset side.
    netLiqSeries = net_liquidity_series(S)
    netLiq = netLiqSeries[-1]['value'] if netLiqSeries else None
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    # Still computed, still charted, no longer weighted: this is the one
    # metric-asset pairing with a validated out-of-sample relationship
    # (Short Treasuries vs. Fed balance-sheet momentum), so risk_history keeps
    # carrying it even though the balance sheet now enters the score through
    # net liquidity instead.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), window=120, invert=True)
    reservesScore = momentum_percentile_score(S.get('WRESBAL', []), window=60, invert=True)

    nfci = L('NFCI')
    # calibrated: rapid NFCI TIGHTENING preceded SPY weakness (r=-0.27, n=3317)
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

    # The Liquidity Flow Stress composite that used to live here is gone: its
    # three inputs are now read once, through Fed Net Liquidity.

    # Real 10-year yield: the single most connected variable in macro, and
    # absent from the original model. Drives the dollar through real-rate
    # differentials, gold inversely, and every long-duration valuation.
    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), window=1000)

    # 10Y minus 3M rather than 10Y minus 2Y: the better recession record, and
    # unlike 2s10s it is not simply the difference of two things already scored.
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    # One volatility indicator instead of two rows reading the same series.
    volScore = None
    if None not in (vixScore, vixTermProxy):
        volScore = 0.6 * vixScore + 0.4 * vixTermProxy

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
    # The old pair of breadth indicators used these same two spreads with
    # weights of (10, 5) and (15, 10) respectively — linear combinations so
    # similar that the two scores correlate at 0.989. One indicator, weights
    # midway between the two originals.
    breadthScore = clamp(50 - (breadth5D*12 + breadth20D*7), 0, 100) if None not in (breadth5D, breadth20D) else None

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
        {'name': 'HY Credit Spreads', 'category': 'Credit', 'weight': WEIGHTS['HY Credit Spreads'][1], 'reading': hy, 'units': 'bps', 'score': hyScore,
         'formula': 'Percentile rank of today\u2019s spread within its own trailing 750 observations. 100 = widest in that window.',
         'inputs': [raw('BAMLH0A0HYM2', 'ICE BofA US High Yield option-adjusted spread')]},
        {'name': 'Investment-Grade Spreads', 'category': 'Credit', 'weight': WEIGHTS['Investment-Grade Spreads'][1], 'reading': ig, 'units': 'bps', 'score': igScore,
         'formula': 'Percentile rank within its own trailing 750 observations.',
         'inputs': [raw('BAMLC0A0CM', 'ICE BofA US Corporate option-adjusted spread')]},
        {'name': 'Repo-Market Stress', 'category': 'Liquidity', 'weight': WEIGHTS['Repo-Market Stress'][1], 'reading': repoScore, 'units': '0\u2013100', 'score': repoScore,
         'formula': '0.45 \u00d7 clamp(20 + (SOFR\u2212IORB)\u00d73) + 0.30 \u00d7 clamp(20 + (SOFR\u2212EFFR)\u00d74) + 0.25 \u00d7 clamp((SOFR 75th \u2212 25th)\u00d74), each clamped 0\u2013100',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    raw('EFFR', 'Effective Fed Funds Rate'), raw('SOFR25', 'SOFR 25th percentile'), raw('SOFR75', 'SOFR 75th percentile'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps'), calc('SOFR \u2212 EFFR', sofrEffrBps, 'bps'),
                    calc('SOFR interquartile range', sofrIqrBps, 'bps')]},
        {'name': 'Fed Net Liquidity', 'category': 'Liquidity', 'weight': WEIGHTS['Fed Net Liquidity'][1], 'reading': netLiq, 'units': '$mm', 'score': netLiqScore,
         'formula': 'net liquidity = Fed total assets \u2212 Treasury general account \u2212 reverse repo. Scored on the percentile rank of its 20-week change within its own trailing 120, inverted so rapid expansion reads calm.',
         'inputs': [raw('WALCL', 'Fed total assets'), raw('WTREGEN', 'Treasury general account'),
                    raw('RRPONTSYD', 'Overnight reverse repo ($bn)'),
                    calc('net liquidity', netLiq, '$mm')],
         'flag': 'Replaces four indicators (Fed Balance Sheet, Bank Reserves, TGA, Liquidity Flow Stress) that read the same balance sheet twice over.'},
        {'name': 'DXY / Broad Dollar', 'category': 'Liquidity', 'weight': WEIGHTS['DXY / Broad Dollar'][1], 'reading': dxy, 'units': 'index', 'score': dxyScore,
         'formula': 'clamp((index \u2212 100) \u00d7 2, 0, 100). Reads 0 at or below 100.',
         'inputs': [raw('DTWEXBGS', 'Nominal Broad US Dollar Index')]},
        {'name': 'Treasury Vol Proxy (MOVE-style)', 'category': 'Rates', 'weight': WEIGHTS['Treasury Vol Proxy (MOVE-style)'][1], 'reading': treasuryVolStress, 'units': '0\u2013100', 'score': treasuryVolStress, 'source_note': 'synthetic proxy from 2Y/10Y 5-day moves \u2014 the real MOVE index isn\u2019t freely available via FRED',
         'formula': 'clamp((|2Y 5-day move in bps| \u00d7 0.6 + |10Y 5-day move in bps| \u00d7 0.4) \u00d7 2, 0, 100)',
         'inputs': [raw('DGS2', '2-year Treasury yield'), lag('DGS2', 5, '2-year, 5 sessions ago'),
                    raw('DGS10', '10-year Treasury yield'), lag('DGS10', 5, '10-year, 5 sessions ago'),
                    calc('|2Y 5-day move|', move2, 'bps'), calc('|10Y 5-day move|', move10, 'bps')]},
        {'name': 'Real 10-Year Yield', 'category': 'Rates', 'weight': WEIGHTS['Real 10-Year Yield'][1], 'reading': dfii10, 'units': '%', 'score': realYieldScore,
         'formula': 'Percentile rank of the 10-year TIPS yield within its own trailing 1000 observations.',
         'inputs': [raw('DFII10', '10-year Treasury inflation-indexed yield')],
         'flag': 'New. Replaces the separately-weighted nominal 2Y and 10Y, which were each also counted inside the Treasury Vol Proxy.'},
        {'name': 'Yield Curve (10Y \u2212 3M)', 'category': 'Rates', 'weight': WEIGHTS['Yield Curve (10Y \u2212 3M)'][1], 'reading': t10y3m, 'units': 'pct pts', 'score': curveScore,
         'formula': 'clamp(50 \u2212 25 \u00d7 (10Y \u2212 3M), 0, 100). Reads 50 at a flat curve and rises as it inverts.',
         'inputs': [raw('T10Y3M', '10-year minus 3-month spread')],
         'flag': 'Replaces 2s10s, which carried 0% weight because it was the difference of two already-scored yields. 10Y\u22123M is a distinct series with the stronger recession record.'},
        {'name': 'Market Volatility', 'category': 'Market / Macro', 'weight': WEIGHTS['Market Volatility'][1], 'reading': vix, 'units': 'VIX index', 'score': volScore,
         'formula': '0.6 \u00d7 clamp((VIX \u2212 12) \u00d7 3.2) + 0.4 \u00d7 clamp(50 + (VIX 5-day % change) \u00d7 4), each clamped 0\u2013100.',
         'inputs': [raw('VIXCLS', 'CBOE Volatility Index, close'), lag('VIXCLS', 5, 'VIX, 5 sessions ago'),
                    calc('5-day change', vixChgPct, '%'), calc('level component', vixScore, '0\u2013100'),
                    calc('momentum component', vixTermProxy, '0\u2013100')],
         'flag': 'Merges the old VIX and VIX Momentum rows, which read the same series and together carried 10%.'},
        {'name': 'Equity Breadth', 'category': 'Market / Macro', 'weight': WEIGHTS['Equity Breadth'][1], 'reading': breadthScore, 'units': '0\u2013100', 'score': breadthScore, 'source_note': 'live via SPY/RSP \u2014 unavailable in the original workbook',
         'formula': 'clamp(50 \u2212 (5-day RSP-vs-SPY spread \u00d7 12 + 20-day spread \u00d7 7), 0, 100). Equal-weight lagging cap-weight means a narrow market.',
         'inputs': [eq('SPY', spyL, 'S&P 500 ETF, last close'), eq('RSP', rspL, 'Equal-weight S&P ETF, last close'),
                    calc('5-day breadth spread', breadth5D, '%'), calc('20-day breadth spread', breadth20D, '%')],
         'flag': 'Merges S&P 500 Breadth and Market Participation Momentum, which used these same two spreads and correlated at 0.989.'},
        {'name': 'Financial Conditions (NFCI)', 'category': 'Market / Macro', 'weight': WEIGHTS['Financial Conditions (NFCI)'][1], 'reading': nfci, 'units': 'index', 'score': nfciScore,
         'formula': 'Percentile rank of the 20-observation change within its own trailing 500 \u2014 fast tightening scores high.',
         'inputs': [raw('NFCI', 'Chicago Fed National Financial Conditions Index')]},
        {'name': 'Jobless Claims Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Jobless Claims Momentum'][1], 'reading': econSurpriseScore, 'units': '0\u2013100', 'score': econSurpriseScore,
         'formula': 'clamp(50 + (initial claims 5-week % change) \u00d7 5, 0, 100). Reads 50 when claims are flat.',
         'inputs': [raw('ICSA', 'Initial unemployment claims'), lag('ICSA', 5, '5 weeks ago'),
                    calc('5-week change', claimsChgPct, '%')],
         'flag': 'Renamed from \u201cEconomic Surprise\u201d. A surprise index measures data against consensus forecasts; this measures claims against their own recent level, so the old name overstated it.'},
        {'name': 'Inflation Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Inflation Momentum'][1], 'reading': inflationLaborScore, 'units': '0\u2013100', 'score': inflationLaborScore,
         'formula': 'clamp(50 + ((core CPI m/m \u00d7 0.5 + core PCE m/m \u00d7 0.5) \u2212 0.2) \u00d7 200, 0, 100). Reads 50 at 0.2% monthly, roughly the 2% annual target.',
         'inputs': [raw('CPILFESL', 'Core CPI index'), lag('CPILFESL', 1, 'Core CPI, prior month'),
                    raw('PCEPILFE', 'Core PCE index'), lag('PCEPILFE', 1, 'Core PCE, prior month'),
                    calc('core CPI m/m', coreCpiMo, '%'), calc('core PCE m/m', corePceMo, '%')],
         'flag': 'Renamed from \u201cInflation & Labor Momentum\u201d. No labour series ever fed it.'},
        {'name': 'SOFR\u2013IORB Spread', 'category': 'Liquidity', 'weight': 0, 'reading': sofrIorbBps, 'units': 'bps', 'score': sofrIorbScore, 'redundant': 'folded into Repo-Market Stress (45% of that composite)',
         'formula': 'clamp(50 + (SOFR \u2212 IORB in bps) \u00d7 4, 0, 100)',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps')]},
        {'name': 'Bank Reserves', 'category': 'Liquidity', 'weight': 0, 'reading': wresbal, 'units': '$mm', 'score': reservesScore, 'redundant': 'reserves are a liability of the same balance sheet Fed Net Liquidity already measures \u2014 shown for reference, not scored',
         'formula': 'Percentile rank of the 20-week change within its own trailing 60, inverted.',
         'inputs': [raw('WRESBAL', 'Reserve balances held at Federal Reserve banks')]},
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

    def roc_steps_series(series, window, score, roc_period=20, invert=False, label='net liquidity'):
        """roc_steps() for a series built in code rather than fetched by id."""
        vals = [q['value'] for q in series]
        if score is None or len(vals) < roc_period + 30:
            return []
        rocs = [vals[i] - vals[i - roc_period] for i in range(roc_period, len(vals))]
        pool = rocs[-window:]
        cur = rocs[-1]
        below = sum(1 for v in pool if v < cur)
        out = [f'{label} now {g(vals[-1])}, {roc_period} observations ago {g(vals[-1-roc_period])}',
               f'change = {g(cur)}',
               f'pool = the last {len(pool)} such changes, ranging {g(min(pool))} to {g(max(pool))}',
               f'{below} of {len(pool)} were smaller \u2192 {below/len(pool)*100:.2f}']
        out.append(f'inverted (faster expansion = calmer): 100 \u2212 {below/len(pool)*100:.2f} = {score:.2f}'
                   if invert else f'score = {score:.2f}')
        return out

    step_map = {
        'HY Credit Spreads': pct_steps(hy, 'BAMLH0A0HYM2', 750, hyScore),
        'Investment-Grade Spreads': pct_steps(ig, 'BAMLC0A0CM', 750, igScore),
        'Real 10-Year Yield': pct_steps(dfii10, 'DFII10', 1000, realYieldScore),
        'Bank Reserves': roc_steps('WRESBAL', 60, reservesScore, invert=True),
        'Financial Conditions (NFCI)': roc_steps('NFCI', 500, nfciScore),
    }
    if netLiqSeries and netLiq is not None:
        step_map['Fed Net Liquidity'] = [
            f'net liquidity = assets {g(walcl)} \u2212 TGA {g(wtregen)} \u2212 RRP {g((rrp or 0)*1000)} = {g(netLiq)} $mm',
        ] + roc_steps_series(netLiqSeries, 120, netLiqScore, invert=True)
    if t10y3m is not None:
        step_map['Yield Curve (10Y \u2212 3M)'] = [
            f'10Y \u2212 3M = {f(t10y3m,3)} percentage points',
            f'clamp(50 \u2212 25 \u00d7 {f(t10y3m,3)}) = {f(curveScore)}',
        ]
    if volScore is not None:
        step_map['Market Volatility'] = [
            f'level: clamp(({f(vix)} \u2212 12) \u00d7 3.2) = {f(vixScore)}',
            f'momentum: VIX {f(vix)} vs {f(vixp5)} five sessions ago = {f(vixChgPct)}%',
            f'          clamp(50 + {f(vixChgPct)} \u00d7 4) = {f(vixTermProxy)}',
            f'0.6 \u00d7 {f(vixScore)} + 0.4 \u00d7 {f(vixTermProxy)} = {f(volScore)}',
        ]
    if breadthScore is not None:
        step_map['Equity Breadth'] = [
            f'RSP vs SPY over 5 sessions = {f(breadth5D,3)}%  (equal-weight minus cap-weight)',
            f'RSP vs SPY over 20 sessions = {f(breadth20D,3)}%',
            f'clamp(50 \u2212 ({f(breadth5D,3)} \u00d7 12 + {f(breadth20D,3)} \u00d7 7)) = {f(breadthScore)}',
        ]

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
    if dxy is not None:
        step_map['DXY / Broad Dollar'] = [f'clamp(({f(dxy,4)} \u2212 100) \u00d7 2) = {f(dxyScore)}']
    if claimsChgPct is not None:
        step_map['Jobless Claims Momentum'] = [
            f'claims {f(icsa,0)} vs {f(icsaP5,0)} five weeks ago = {f(claimsChgPct)}%',
            f'clamp(50 + {f(claimsChgPct)} \u00d7 5) = {f(econSurpriseScore)}',
        ]
    if None not in (coreCpiMo, corePceMo):
        blend = coreCpiMo * 0.5 + corePceMo * 0.5
        step_map['Inflation Momentum'] = [
            f'core CPI {f(cpiCore,3)} vs {f(cpiCoreP1,3)} last month = {f(coreCpiMo,3)}% m/m',
            f'core PCE {f(pceCore,3)} vs {f(pceCoreP1,3)} last month = {f(corePceMo,3)}% m/m',
            f'blend = ({f(coreCpiMo,3)} + {f(corePceMo,3)}) \u00f7 2 = {f(blend,3)}%',
            f'clamp(50 + ({f(blend,3)} \u2212 0.2) \u00d7 200) = {f(inflationLaborScore)}',
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

    regime = regime_from(category_scores['Liquidity'], category_scores['Credit'],
                         category_scores['Rates'], inflationary_pressure, fxStress)

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
    # Kept because Short Treasuries vs. Fed balance-sheet momentum is the one
    # validated out-of-sample pairing; no longer part of the weighted score.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), asof_date=date, window=120, invert=True)

    netLiqSeries = [q for q in net_liquidity_series(S) if q['date'] <= date]
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    nfci = L('NFCI')
    nfciScore = momentum_percentile_score(S.get('NFCI', []), asof_date=date, window=500)

    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), asof_date=date, window=1000)
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    volScore = 0.6 * vixScore + 0.4 * vixTermProxy if None not in (vixScore, vixTermProxy) else None

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

    spyL, spyP5, spyP20 = EL('SPY'), EP5('SPY'), EP20('SPY')
    rspL, rspP5, rspP20 = EL('RSP'), EP5('RSP'), EP20('RSP')
    breadth5D = breadth20D = None
    if None not in (spyL, spyP5, rspL, rspP5) and spyP5 and rspP5:
        breadth5D = ((rspL/rspP5)/(spyL/spyP5)-1)*100
    if None not in (spyL, spyP20, rspL, rspP20) and spyP20 and rspP20:
        breadth20D = ((rspL/rspP20)/(spyL/spyP20)-1)*100
    breadthScore = clamp(50-(breadth5D*12+breadth20D*7),0,100) if None not in (breadth5D,breadth20D) else None

    # Single source of truth: same WEIGHTS table compute_model() uses, so the
    # reconstructed history can never drift from the live score.
    scored = {
        'HY Credit Spreads': hyScore,
        'Investment-Grade Spreads': igScore,
        'Repo-Market Stress': repoScore,
        'Fed Net Liquidity': netLiqScore,
        'DXY / Broad Dollar': dxyScore,
        'Treasury Vol Proxy (MOVE-style)': treasuryVolStress,
        'Real 10-Year Yield': realYieldScore,
        'Yield Curve (10Y \u2212 3M)': curveScore,
        'Market Volatility': volScore,
        'Equity Breadth': breadthScore,
        'Financial Conditions (NFCI)': nfciScore,
        'Jobless Claims Momentum': econSurpriseScore,
        'Inflation Momentum': inflationLaborScore,
    }

    def cat_avg(category):
        rows = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                if sc is not None and WEIGHTS[n][0] == category and WEIGHTS[n][1] > 0]
        if not rows:
            return None
        wsum = sum(w for _, w in rows)
        return sum(sc*w for sc, w in rows) / wsum if wsum > 0 else None

    liquidity_score = cat_avg('Liquidity')
    credit_score = cat_avg('Credit')
    rates_score = cat_avg('Rates')
    market_score = cat_avg('Market / Macro')

    sofrIorbScore = clamp(50 + sofrIorbBps*4, 0, 100) if sofrIorbBps is not None else None
    y2Score = percentile_score(dgs2, S.get('DGS2', []), asof_date=date, window=1000)
    fedExpScore = 0.6*y2Score + 0.4*sofrIorbScore if None not in (y2Score, sofrIorbScore) else None
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
    fx_stress = fx_composite(*fx_legs)

    contributing = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                    if sc is not None and WEIGHTS[n][1] > 0]
    overall_risk = sum(sc*w for sc, w in contributing) if contributing else None

    return {
        'overall_risk': round(overall_risk, 2) if overall_risk is not None else None,
        'Liquidity': round(liquidity_score, 2) if liquidity_score is not None else None,
        'Credit': round(credit_score, 2) if credit_score is not None else None,
        'Rates': round(rates_score, 2) if rates_score is not None else None,
        'Market / Macro': round(market_score, 2) if market_score is not None else None,
        'Inflationary Pressure': round(inflationary_pressure, 2) if inflationary_pressure is not None else None,
        'FX Stress': round(fx_stress, 2) if fx_stress is not None else None,
        'Real Yield': round(realYieldScore, 2) if realYieldScore is not None else None,
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
    # Gold's dominant driver is the real yield, not a rates/Fed/dollar blend.
    # Now that DFII10 is fetched, chart them against the thing itself.
    'U.S. Dollar': 'FX Stress', 'Gold': 'Real Yield', 'Silver': 'Real Yield',
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

    # Optional panel data, fetched AFTER the success guard above so a failure
    # here can never block the risk model from publishing. Each series is
    # independent: whatever returns gets used, whatever doesn't is skipped.
    print('Fetching real-yield panel series (optional \u2014 failures are tolerated)...')
    optional_ids = []
    for cfg in REAL_YIELD_MARKETS.values():
        optional_ids += [cfg['yield'], cfg['cpi']] + ([cfg['fx']] if cfg['fx'] else [])
    optional_ids = sorted(set(optional_ids))
    R = {}
    for sid in optional_ids:
        arr = fetch_fred_series(sid, days_back=2500)
        R[sid] = arr
        if not arr:
            print(f'  {sid}: unavailable (skipped)')
        time.sleep(0.4)
    ok = sum(1 for a in R.values() if a)
    print(f'  real-yield panel: {ok}/{len(optional_ids)} series returned data')

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
        ry = next((i for i in model['indicators'] if i['name'] == 'Real 10-Year Yield'), None)
        live_point['Real Yield'] = round(ry['score'], 2) if ry and ry['score'] is not None else None
        fedbs_indicator = next((i for i in model['indicators'] if i['name'] == 'Fed Balance Sheet'), None)
        live_point['Fed Balance Sheet'] = round(fedbs_indicator['score'], 2) if fedbs_indicator and fedbs_indicator['score'] is not None else None
        risk_history.append(live_point)
    print(f'  {len(risk_history)} risk-history points reconstructed (overall + 6 sub-metrics each)')

    model['asset_price_history'] = asset_price_history
    model['risk_history'] = risk_history
    model['asset_risk_map'] = ASSET_RISK_MAP

    real_rows = build_real_yields(R)
    model['real_yields'] = real_rows
    model['real_yields_note'] = (
        'Ex-post real yield = long-term government bond yield minus year-over-year CPI. '
        'This is inflation that has already happened, not the market-priced expectation a '
        'US TIPS yield (DFII10) represents, so it is not the same quantity as the Real '
        '10-Year Yield indicator above. Sources are OECD series via FRED, published monthly '
        'with roughly a month\u2019s lag \u2014 useful for the structural picture across countries, '
        'not for timing. Currency moves are 60 business days, shown as the foreign '
        'currency\u2019s gain against the dollar.')
    print(f'  real-yield panel: {len(real_rows)} of {len(REAL_YIELD_MARKETS)} countries built')
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
    main()#!/usr/bin/env python3
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

# Dropped from the original list: PAYEMS, CPIAUCSL and PCEPI were fetched
# every run and never referenced by any formula; T10Y2Y is superseded by
# T10Y3M, which has the better recession record. Added: DFII10 (the real
# 10-year yield, the most connected variable in macro and previously absent),
# RRPONTSYD (reverse repo — without it the net-liquidity figure was missing
# a facility that held over $2trn at its peak), and T10Y3M.
FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'DFII10', 'T10Y3M', 'DTWEXBGS', 'WALCL', 'WRESBAL', 'WTREGEN',
    'RRPONTSYD', 'NFCI', 'EFFR', 'SOFR25', 'SOFR75', 'DEXJPUS', 'DEXUSEU',
    'DEXCHUS', 'DEXSZUS', 'DEXUSAL',
]

# --- Real yield differentials -------------------------------------------
# Market-priced real yields (like the US TIPS series DFII10) barely exist in
# free daily form outside the US: the Bank of England publishes a daily real
# gilt curve but only as zipped spreadsheets, and Germany and Japan each use a
# different portal and format again.
#
# So these are EX-POST real yields: nominal long-term government yield minus
# year-over-year CPI. That is inflation that already happened, not the market's
# expectation, which is what a linker prices. It is the standard fallback when
# index-linked data isn't available, and it is fine for comparing levels and
# direction across countries — but it is not the same quantity as DFII10 and
# should not be read as though it were.
#
# Monthly, with roughly a month's publication lag. Deliberately kept OUT of the
# risk score: different question, different cadence, and the model was just
# simplified to remove redundancy.
#
# 'fx' quote conventions differ on FRED. 'fx_inverted' means the series is
# foreign-currency-per-dollar (rising = weaker foreign currency); otherwise it
# is dollars-per-foreign-unit (rising = stronger foreign currency).
REAL_YIELD_MARKETS = {
    'US': {'name': 'United States', 'ccy': 'USD',
           'yield': 'IRLTLT01USM156N', 'cpi': 'CPALTT01USM659N', 'fx': None, 'fx_inverted': False},
    'DE': {'name': 'Germany', 'ccy': 'EUR',
           'yield': 'IRLTLT01DEM156N', 'cpi': 'CPALTT01DEM659N', 'fx': 'DEXUSEU', 'fx_inverted': False},
    'GB': {'name': 'United Kingdom', 'ccy': 'GBP',
           'yield': 'IRLTLT01GBM156N', 'cpi': 'CPALTT01GBM659N', 'fx': 'DEXUSUK', 'fx_inverted': False},
    'JP': {'name': 'Japan', 'ccy': 'JPY',
           'yield': 'IRLTLT01JPM156N', 'cpi': 'CPALTT01JPM659N', 'fx': 'DEXJPUS', 'fx_inverted': True},
    'CA': {'name': 'Canada', 'ccy': 'CAD',
           'yield': 'IRLTLT01CAM156N', 'cpi': 'CPALTT01CAM659N', 'fx': 'DEXCAUS', 'fx_inverted': True},
    'AU': {'name': 'Australia', 'ccy': 'AUD',
           'yield': 'IRLTLT01AUM156N', 'cpi': 'CPALTT01AUQ659N', 'fx': 'DEXUSAL', 'fx_inverted': False},
    'CH': {'name': 'Switzerland', 'ccy': 'CHF',
           'yield': 'IRLTLT01CHM156N', 'cpi': 'CPALTT01CHM659N', 'fx': 'DEXSZUS', 'fx_inverted': True},
    'NO': {'name': 'Norway', 'ccy': 'NOK',
           'yield': 'IRLTLT01NOM156N', 'cpi': 'CPALTT01NOM659N', 'fx': 'DEXNOUS', 'fx_inverted': True},
    'SE': {'name': 'Sweden', 'ccy': 'SEK',
           'yield': 'IRLTLT01SEM156N', 'cpi': 'CPALTT01SEM659N', 'fx': 'DEXSDUS', 'fx_inverted': True},
}


def build_real_yields(R):
    """R = dict of the optional series fetched for this panel. Any country
    whose yield or CPI series failed is dropped rather than shown as blank,
    and the caller reports which ones survived."""
    def latest(sid):
        arr = R.get(sid) or []
        return (arr[-1]['value'], arr[-1]['date']) if arr else (None, None)

    rows = {}
    for code, cfg in REAL_YIELD_MARKETS.items():
        nom, nom_d = latest(cfg['yield'])
        cpi, cpi_d = latest(cfg['cpi'])
        if nom is None or cpi is None:
            continue
        rows[code] = {
            'code': code, 'name': cfg['name'], 'ccy': cfg['ccy'],
            'nominal': round(nom, 3), 'nominal_date': nom_d,
            'cpi': round(cpi, 3), 'cpi_date': cpi_d,
            'real': round(nom - cpi, 3),
            'yield_series': cfg['yield'], 'cpi_series': cfg['cpi'],
        }

    us = rows.get('US')
    for code, r in rows.items():
        r['real_diff_vs_us'] = round(r['real'] - us['real'], 3) if us else None

        cfg = REAL_YIELD_MARKETS[code]
        r['fx_60d'] = None
        if cfg['fx']:
            arr = R.get(cfg['fx']) or []
            if len(arr) > 60:
                a, b = arr[-1]['value'], arr[-61]['value']
                if a and b:
                    chg = (a / b - 1) * 100
                    # express as the foreign currency's gain against the dollar
                    r['fx_60d'] = round(-chg if cfg['fx_inverted'] else chg, 2)
            r['fx_series'] = cfg['fx']
    return list(rows.values())


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


# ---------------------------------------------------------------------------
# SIMPLIFIED INDICATOR SET
#
# The previous model carried 18 weighted indicators, but several were reading
# the same series through different transforms and so were counted twice:
#
#   * WALCL / WRESBAL / WTREGEN carried 19% across four indicators — a levels
#     read (Fed Balance Sheet, Bank Reserves, TGA) and a flows read (Liquidity
#     Flow Stress) of one balance sheet. Worse, that flow formula ADDED
#     reserves to assets, when reserves are a liability of the same balance
#     sheet. These collapse into one Fed Net Liquidity indicator using the
#     conventional definition: assets minus TGA minus reverse repo.
#   * VIXCLS carried 10% across VIX and VIX Momentum — one series, two rows.
#     Merged into a single Market Volatility indicator blending level and
#     momentum.
#   * SPY/RSP carried 8% across S&P 500 Breadth and Market Participation
#     Momentum. Those two are the same two spreads with different linear
#     weights and correlate at 0.989 — one signal, billed twice. Merged.
#   * Nominal 2Y and 10Y were each weighted standalone AND inside the Treasury
#     Vol Proxy. Replaced by the real 10-year yield (the variable that actually
#     drives the dollar, gold and long duration) and the 10Y-3M curve.
#
# Result: 13 weighted indicators from 18, with no series feeding two weighted
# rows. Category totals are unchanged, so the headline score stays comparable.
# Two indicators are kept at zero weight for visibility only.
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Credit — 20%
    'HY Credit Spreads':            ('Credit', .13),
    'Investment-Grade Spreads':     ('Credit', .07),
    # Liquidity — 36%
    'Repo-Market Stress':           ('Liquidity', .12),
    'Fed Net Liquidity':            ('Liquidity', .19),
    'DXY / Broad Dollar':           ('Liquidity', .05),
    # Rates — 16%
    'Treasury Vol Proxy (MOVE-style)': ('Rates', .07),
    'Real 10-Year Yield':           ('Rates', .05),
    'Yield Curve (10Y \u2212 3M)':      ('Rates', .04),
    # Market / Macro — 28%
    'Market Volatility':            ('Market / Macro', .10),
    'Equity Breadth':               ('Market / Macro', .08),
    'Financial Conditions (NFCI)':  ('Market / Macro', .04),
    'Jobless Claims Momentum':      ('Market / Macro', .03),
    'Inflation Momentum':           ('Market / Macro', .03),
    # shown but not scored
    'SOFR\u2013IORB Spread':            ('Liquidity', 0),
    'Bank Reserves':                ('Liquidity', 0),
}


def net_liquidity_series(S):
    """Fed net liquidity = total assets \u2212 Treasury general account \u2212 reverse
    repo, the conventional measure of how many dollars are actually loose in
    the system. Built on WALCL's weekly dates, with the other two taken as of
    each of those dates, because the three publish on different schedules.

    RRPONTSYD is reported in $bn while WALCL and WTREGEN are in $mm, hence the
    \u00d71000. It also only begins in 2013; treated as zero before that, which is
    correct \u2014 the facility did not exist."""
    walcl = S.get('WALCL') or []
    tga_s = S.get('WTREGEN') or []
    rrp_s = S.get('RRPONTSYD') or []
    out = []
    for p in walcl:
        d = p['date']
        tga = asof_at(tga_s, d)
        if tga is None:
            continue
        rrp = asof_at(rrp_s, d)
        rrp = (rrp * 1000.0) if rrp is not None else 0.0
        out.append({'date': d, 'value': p['value'] - tga - rrp})
    return out


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
    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None


    wresbal = L('WRESBAL')
    walcl, wtregen = L('WALCL'), L('WTREGEN')
    rrp = L('RRPONTSYD')

    # One measure where there were four. Net liquidity = assets − TGA − RRP,
    # scored on how fast it is moving relative to its own recent history and
    # inverted, so rapid expansion reads calm. The old set scored the levels of
    # three components separately AND their combined flow, putting 19% of the
    # model on one balance sheet read two ways — and it added reserves to
    # assets, double-counting a liability against its own asset side.
    netLiqSeries = net_liquidity_series(S)
    netLiq = netLiqSeries[-1]['value'] if netLiqSeries else None
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    # Still computed, still charted, no longer weighted: this is the one
    # metric-asset pairing with a validated out-of-sample relationship
    # (Short Treasuries vs. Fed balance-sheet momentum), so risk_history keeps
    # carrying it even though the balance sheet now enters the score through
    # net liquidity instead.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), window=120, invert=True)
    reservesScore = momentum_percentile_score(S.get('WRESBAL', []), window=60, invert=True)

    nfci = L('NFCI')
    # calibrated: rapid NFCI TIGHTENING preceded SPY weakness (r=-0.27, n=3317)
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

    # The Liquidity Flow Stress composite that used to live here is gone: its
    # three inputs are now read once, through Fed Net Liquidity.

    # Real 10-year yield: the single most connected variable in macro, and
    # absent from the original model. Drives the dollar through real-rate
    # differentials, gold inversely, and every long-duration valuation.
    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), window=1000)

    # 10Y minus 3M rather than 10Y minus 2Y: the better recession record, and
    # unlike 2s10s it is not simply the difference of two things already scored.
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    # One volatility indicator instead of two rows reading the same series.
    volScore = None
    if None not in (vixScore, vixTermProxy):
        volScore = 0.6 * vixScore + 0.4 * vixTermProxy

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
    # The old pair of breadth indicators used these same two spreads with
    # weights of (10, 5) and (15, 10) respectively — linear combinations so
    # similar that the two scores correlate at 0.989. One indicator, weights
    # midway between the two originals.
    breadthScore = clamp(50 - (breadth5D*12 + breadth20D*7), 0, 100) if None not in (breadth5D, breadth20D) else None

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
        {'name': 'HY Credit Spreads', 'category': 'Credit', 'weight': WEIGHTS['HY Credit Spreads'][1], 'reading': hy, 'units': 'bps', 'score': hyScore,
         'formula': 'Percentile rank of today\u2019s spread within its own trailing 750 observations. 100 = widest in that window.',
         'inputs': [raw('BAMLH0A0HYM2', 'ICE BofA US High Yield option-adjusted spread')]},
        {'name': 'Investment-Grade Spreads', 'category': 'Credit', 'weight': WEIGHTS['Investment-Grade Spreads'][1], 'reading': ig, 'units': 'bps', 'score': igScore,
         'formula': 'Percentile rank within its own trailing 750 observations.',
         'inputs': [raw('BAMLC0A0CM', 'ICE BofA US Corporate option-adjusted spread')]},
        {'name': 'Repo-Market Stress', 'category': 'Liquidity', 'weight': WEIGHTS['Repo-Market Stress'][1], 'reading': repoScore, 'units': '0\u2013100', 'score': repoScore,
         'formula': '0.45 \u00d7 clamp(20 + (SOFR\u2212IORB)\u00d73) + 0.30 \u00d7 clamp(20 + (SOFR\u2212EFFR)\u00d74) + 0.25 \u00d7 clamp((SOFR 75th \u2212 25th)\u00d74), each clamped 0\u2013100',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    raw('EFFR', 'Effective Fed Funds Rate'), raw('SOFR25', 'SOFR 25th percentile'), raw('SOFR75', 'SOFR 75th percentile'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps'), calc('SOFR \u2212 EFFR', sofrEffrBps, 'bps'),
                    calc('SOFR interquartile range', sofrIqrBps, 'bps')]},
        {'name': 'Fed Net Liquidity', 'category': 'Liquidity', 'weight': WEIGHTS['Fed Net Liquidity'][1], 'reading': netLiq, 'units': '$mm', 'score': netLiqScore,
         'formula': 'net liquidity = Fed total assets \u2212 Treasury general account \u2212 reverse repo. Scored on the percentile rank of its 20-week change within its own trailing 120, inverted so rapid expansion reads calm.',
         'inputs': [raw('WALCL', 'Fed total assets'), raw('WTREGEN', 'Treasury general account'),
                    raw('RRPONTSYD', 'Overnight reverse repo ($bn)'),
                    calc('net liquidity', netLiq, '$mm')],
         'flag': 'Replaces four indicators (Fed Balance Sheet, Bank Reserves, TGA, Liquidity Flow Stress) that read the same balance sheet twice over.'},
        {'name': 'DXY / Broad Dollar', 'category': 'Liquidity', 'weight': WEIGHTS['DXY / Broad Dollar'][1], 'reading': dxy, 'units': 'index', 'score': dxyScore,
         'formula': 'clamp((index \u2212 100) \u00d7 2, 0, 100). Reads 0 at or below 100.',
         'inputs': [raw('DTWEXBGS', 'Nominal Broad US Dollar Index')]},
        {'name': 'Treasury Vol Proxy (MOVE-style)', 'category': 'Rates', 'weight': WEIGHTS['Treasury Vol Proxy (MOVE-style)'][1], 'reading': treasuryVolStress, 'units': '0\u2013100', 'score': treasuryVolStress, 'source_note': 'synthetic proxy from 2Y/10Y 5-day moves \u2014 the real MOVE index isn\u2019t freely available via FRED',
         'formula': 'clamp((|2Y 5-day move in bps| \u00d7 0.6 + |10Y 5-day move in bps| \u00d7 0.4) \u00d7 2, 0, 100)',
         'inputs': [raw('DGS2', '2-year Treasury yield'), lag('DGS2', 5, '2-year, 5 sessions ago'),
                    raw('DGS10', '10-year Treasury yield'), lag('DGS10', 5, '10-year, 5 sessions ago'),
                    calc('|2Y 5-day move|', move2, 'bps'), calc('|10Y 5-day move|', move10, 'bps')]},
        {'name': 'Real 10-Year Yield', 'category': 'Rates', 'weight': WEIGHTS['Real 10-Year Yield'][1], 'reading': dfii10, 'units': '%', 'score': realYieldScore,
         'formula': 'Percentile rank of the 10-year TIPS yield within its own trailing 1000 observations.',
         'inputs': [raw('DFII10', '10-year Treasury inflation-indexed yield')],
         'flag': 'New. Replaces the separately-weighted nominal 2Y and 10Y, which were each also counted inside the Treasury Vol Proxy.'},
        {'name': 'Yield Curve (10Y \u2212 3M)', 'category': 'Rates', 'weight': WEIGHTS['Yield Curve (10Y \u2212 3M)'][1], 'reading': t10y3m, 'units': 'pct pts', 'score': curveScore,
         'formula': 'clamp(50 \u2212 25 \u00d7 (10Y \u2212 3M), 0, 100). Reads 50 at a flat curve and rises as it inverts.',
         'inputs': [raw('T10Y3M', '10-year minus 3-month spread')],
         'flag': 'Replaces 2s10s, which carried 0% weight because it was the difference of two already-scored yields. 10Y\u22123M is a distinct series with the stronger recession record.'},
        {'name': 'Market Volatility', 'category': 'Market / Macro', 'weight': WEIGHTS['Market Volatility'][1], 'reading': vix, 'units': 'VIX index', 'score': volScore,
         'formula': '0.6 \u00d7 clamp((VIX \u2212 12) \u00d7 3.2) + 0.4 \u00d7 clamp(50 + (VIX 5-day % change) \u00d7 4), each clamped 0\u2013100.',
         'inputs': [raw('VIXCLS', 'CBOE Volatility Index, close'), lag('VIXCLS', 5, 'VIX, 5 sessions ago'),
                    calc('5-day change', vixChgPct, '%'), calc('level component', vixScore, '0\u2013100'),
                    calc('momentum component', vixTermProxy, '0\u2013100')],
         'flag': 'Merges the old VIX and VIX Momentum rows, which read the same series and together carried 10%.'},
        {'name': 'Equity Breadth', 'category': 'Market / Macro', 'weight': WEIGHTS['Equity Breadth'][1], 'reading': breadthScore, 'units': '0\u2013100', 'score': breadthScore, 'source_note': 'live via SPY/RSP \u2014 unavailable in the original workbook',
         'formula': 'clamp(50 \u2212 (5-day RSP-vs-SPY spread \u00d7 12 + 20-day spread \u00d7 7), 0, 100). Equal-weight lagging cap-weight means a narrow market.',
         'inputs': [eq('SPY', spyL, 'S&P 500 ETF, last close'), eq('RSP', rspL, 'Equal-weight S&P ETF, last close'),
                    calc('5-day breadth spread', breadth5D, '%'), calc('20-day breadth spread', breadth20D, '%')],
         'flag': 'Merges S&P 500 Breadth and Market Participation Momentum, which used these same two spreads and correlated at 0.989.'},
        {'name': 'Financial Conditions (NFCI)', 'category': 'Market / Macro', 'weight': WEIGHTS['Financial Conditions (NFCI)'][1], 'reading': nfci, 'units': 'index', 'score': nfciScore,
         'formula': 'Percentile rank of the 20-observation change within its own trailing 500 \u2014 fast tightening scores high.',
         'inputs': [raw('NFCI', 'Chicago Fed National Financial Conditions Index')]},
        {'name': 'Jobless Claims Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Jobless Claims Momentum'][1], 'reading': econSurpriseScore, 'units': '0\u2013100', 'score': econSurpriseScore,
         'formula': 'clamp(50 + (initial claims 5-week % change) \u00d7 5, 0, 100). Reads 50 when claims are flat.',
         'inputs': [raw('ICSA', 'Initial unemployment claims'), lag('ICSA', 5, '5 weeks ago'),
                    calc('5-week change', claimsChgPct, '%')],
         'flag': 'Renamed from \u201cEconomic Surprise\u201d. A surprise index measures data against consensus forecasts; this measures claims against their own recent level, so the old name overstated it.'},
        {'name': 'Inflation Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Inflation Momentum'][1], 'reading': inflationLaborScore, 'units': '0\u2013100', 'score': inflationLaborScore,
         'formula': 'clamp(50 + ((core CPI m/m \u00d7 0.5 + core PCE m/m \u00d7 0.5) \u2212 0.2) \u00d7 200, 0, 100). Reads 50 at 0.2% monthly, roughly the 2% annual target.',
         'inputs': [raw('CPILFESL', 'Core CPI index'), lag('CPILFESL', 1, 'Core CPI, prior month'),
                    raw('PCEPILFE', 'Core PCE index'), lag('PCEPILFE', 1, 'Core PCE, prior month'),
                    calc('core CPI m/m', coreCpiMo, '%'), calc('core PCE m/m', corePceMo, '%')],
         'flag': 'Renamed from \u201cInflation & Labor Momentum\u201d. No labour series ever fed it.'},
        {'name': 'SOFR\u2013IORB Spread', 'category': 'Liquidity', 'weight': 0, 'reading': sofrIorbBps, 'units': 'bps', 'score': sofrIorbScore, 'redundant': 'folded into Repo-Market Stress (45% of that composite)',
         'formula': 'clamp(50 + (SOFR \u2212 IORB in bps) \u00d7 4, 0, 100)',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps')]},
        {'name': 'Bank Reserves', 'category': 'Liquidity', 'weight': 0, 'reading': wresbal, 'units': '$mm', 'score': reservesScore, 'redundant': 'reserves are a liability of the same balance sheet Fed Net Liquidity already measures \u2014 shown for reference, not scored',
         'formula': 'Percentile rank of the 20-week change within its own trailing 60, inverted.',
         'inputs': [raw('WRESBAL', 'Reserve balances held at Federal Reserve banks')]},
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

    def roc_steps_series(series, window, score, roc_period=20, invert=False, label='net liquidity'):
        """roc_steps() for a series built in code rather than fetched by id."""
        vals = [q['value'] for q in series]
        if score is None or len(vals) < roc_period + 30:
            return []
        rocs = [vals[i] - vals[i - roc_period] for i in range(roc_period, len(vals))]
        pool = rocs[-window:]
        cur = rocs[-1]
        below = sum(1 for v in pool if v < cur)
        out = [f'{label} now {g(vals[-1])}, {roc_period} observations ago {g(vals[-1-roc_period])}',
               f'change = {g(cur)}',
               f'pool = the last {len(pool)} such changes, ranging {g(min(pool))} to {g(max(pool))}',
               f'{below} of {len(pool)} were smaller \u2192 {below/len(pool)*100:.2f}']
        out.append(f'inverted (faster expansion = calmer): 100 \u2212 {below/len(pool)*100:.2f} = {score:.2f}'
                   if invert else f'score = {score:.2f}')
        return out

    step_map = {
        'HY Credit Spreads': pct_steps(hy, 'BAMLH0A0HYM2', 750, hyScore),
        'Investment-Grade Spreads': pct_steps(ig, 'BAMLC0A0CM', 750, igScore),
        'Real 10-Year Yield': pct_steps(dfii10, 'DFII10', 1000, realYieldScore),
        'Bank Reserves': roc_steps('WRESBAL', 60, reservesScore, invert=True),
        'Financial Conditions (NFCI)': roc_steps('NFCI', 500, nfciScore),
    }
    if netLiqSeries and netLiq is not None:
        step_map['Fed Net Liquidity'] = [
            f'net liquidity = assets {g(walcl)} \u2212 TGA {g(wtregen)} \u2212 RRP {g((rrp or 0)*1000)} = {g(netLiq)} $mm',
        ] + roc_steps_series(netLiqSeries, 120, netLiqScore, invert=True)
    if t10y3m is not None:
        step_map['Yield Curve (10Y \u2212 3M)'] = [
            f'10Y \u2212 3M = {f(t10y3m,3)} percentage points',
            f'clamp(50 \u2212 25 \u00d7 {f(t10y3m,3)}) = {f(curveScore)}',
        ]
    if volScore is not None:
        step_map['Market Volatility'] = [
            f'level: clamp(({f(vix)} \u2212 12) \u00d7 3.2) = {f(vixScore)}',
            f'momentum: VIX {f(vix)} vs {f(vixp5)} five sessions ago = {f(vixChgPct)}%',
            f'          clamp(50 + {f(vixChgPct)} \u00d7 4) = {f(vixTermProxy)}',
            f'0.6 \u00d7 {f(vixScore)} + 0.4 \u00d7 {f(vixTermProxy)} = {f(volScore)}',
        ]
    if breadthScore is not None:
        step_map['Equity Breadth'] = [
            f'RSP vs SPY over 5 sessions = {f(breadth5D,3)}%  (equal-weight minus cap-weight)',
            f'RSP vs SPY over 20 sessions = {f(breadth20D,3)}%',
            f'clamp(50 \u2212 ({f(breadth5D,3)} \u00d7 12 + {f(breadth20D,3)} \u00d7 7)) = {f(breadthScore)}',
        ]

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
    if dxy is not None:
        step_map['DXY / Broad Dollar'] = [f'clamp(({f(dxy,4)} \u2212 100) \u00d7 2) = {f(dxyScore)}']
    if claimsChgPct is not None:
        step_map['Jobless Claims Momentum'] = [
            f'claims {f(icsa,0)} vs {f(icsaP5,0)} five weeks ago = {f(claimsChgPct)}%',
            f'clamp(50 + {f(claimsChgPct)} \u00d7 5) = {f(econSurpriseScore)}',
        ]
    if None not in (coreCpiMo, corePceMo):
        blend = coreCpiMo * 0.5 + corePceMo * 0.5
        step_map['Inflation Momentum'] = [
            f'core CPI {f(cpiCore,3)} vs {f(cpiCoreP1,3)} last month = {f(coreCpiMo,3)}% m/m',
            f'core PCE {f(pceCore,3)} vs {f(pceCoreP1,3)} last month = {f(corePceMo,3)}% m/m',
            f'blend = ({f(coreCpiMo,3)} + {f(corePceMo,3)}) \u00f7 2 = {f(blend,3)}%',
            f'clamp(50 + ({f(blend,3)} \u2212 0.2) \u00d7 200) = {f(inflationLaborScore)}',
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
    # Kept because Short Treasuries vs. Fed balance-sheet momentum is the one
    # validated out-of-sample pairing; no longer part of the weighted score.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), asof_date=date, window=120, invert=True)

    netLiqSeries = [q for q in net_liquidity_series(S) if q['date'] <= date]
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    nfci = L('NFCI')
    nfciScore = momentum_percentile_score(S.get('NFCI', []), asof_date=date, window=500)

    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), asof_date=date, window=1000)
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    volScore = 0.6 * vixScore + 0.4 * vixTermProxy if None not in (vixScore, vixTermProxy) else None

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

    spyL, spyP5, spyP20 = EL('SPY'), EP5('SPY'), EP20('SPY')
    rspL, rspP5, rspP20 = EL('RSP'), EP5('RSP'), EP20('RSP')
    breadth5D = breadth20D = None
    if None not in (spyL, spyP5, rspL, rspP5) and spyP5 and rspP5:
        breadth5D = ((rspL/rspP5)/(spyL/spyP5)-1)*100
    if None not in (spyL, spyP20, rspL, rspP20) and spyP20 and rspP20:
        breadth20D = ((rspL/rspP20)/(spyL/spyP20)-1)*100
    breadthScore = clamp(50-(breadth5D*12+breadth20D*7),0,100) if None not in (breadth5D,breadth20D) else None

    # Single source of truth: same WEIGHTS table compute_model() uses, so the
    # reconstructed history can never drift from the live score.
    scored = {
        'HY Credit Spreads': hyScore,
        'Investment-Grade Spreads': igScore,
        'Repo-Market Stress': repoScore,
        'Fed Net Liquidity': netLiqScore,
        'DXY / Broad Dollar': dxyScore,
        'Treasury Vol Proxy (MOVE-style)': treasuryVolStress,
        'Real 10-Year Yield': realYieldScore,
        'Yield Curve (10Y \u2212 3M)': curveScore,
        'Market Volatility': volScore,
        'Equity Breadth': breadthScore,
        'Financial Conditions (NFCI)': nfciScore,
        'Jobless Claims Momentum': econSurpriseScore,
        'Inflation Momentum': inflationLaborScore,
    }

    def cat_avg(category):
        rows = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                if sc is not None and WEIGHTS[n][0] == category and WEIGHTS[n][1] > 0]
        if not rows:
            return None
        wsum = sum(w for _, w in rows)
        return sum(sc*w for sc, w in rows) / wsum if wsum > 0 else None

    liquidity_score = cat_avg('Liquidity')
    credit_score = cat_avg('Credit')
    rates_score = cat_avg('Rates')
    market_score = cat_avg('Market / Macro')

    sofrIorbScore = clamp(50 + sofrIorbBps*4, 0, 100) if sofrIorbBps is not None else None
    y2Score = percentile_score(dgs2, S.get('DGS2', []), asof_date=date, window=1000)
    fedExpScore = 0.6*y2Score + 0.4*sofrIorbScore if None not in (y2Score, sofrIorbScore) else None
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
    fx_stress = fx_composite(*fx_legs)

    contributing = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                    if sc is not None and WEIGHTS[n][1] > 0]
    overall_risk = sum(sc*w for sc, w in contributing) if contributing else None

    return {
        'overall_risk': round(overall_risk, 2) if overall_risk is not None else None,
        'Liquidity': round(liquidity_score, 2) if liquidity_score is not None else None,
        'Credit': round(credit_score, 2) if credit_score is not None else None,
        'Rates': round(rates_score, 2) if rates_score is not None else None,
        'Market / Macro': round(market_score, 2) if market_score is not None else None,
        'Inflationary Pressure': round(inflationary_pressure, 2) if inflationary_pressure is not None else None,
        'FX Stress': round(fx_stress, 2) if fx_stress is not None else None,
        'Real Yield': round(realYieldScore, 2) if realYieldScore is not None else None,
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
    # Gold's dominant driver is the real yield, not a rates/Fed/dollar blend.
    # Now that DFII10 is fetched, chart them against the thing itself.
    'U.S. Dollar': 'FX Stress', 'Gold': 'Real Yield', 'Silver': 'Real Yield',
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

    # Optional panel data, fetched AFTER the success guard above so a failure
    # here can never block the risk model from publishing. Each series is
    # independent: whatever returns gets used, whatever doesn't is skipped.
    print('Fetching real-yield panel series (optional \u2014 failures are tolerated)...')
    optional_ids = []
    for cfg in REAL_YIELD_MARKETS.values():
        optional_ids += [cfg['yield'], cfg['cpi']] + ([cfg['fx']] if cfg['fx'] else [])
    optional_ids = sorted(set(optional_ids))
    R = {}
    for sid in optional_ids:
        arr = fetch_fred_series(sid, days_back=2500)
        R[sid] = arr
        if not arr:
            print(f'  {sid}: unavailable (skipped)')
        time.sleep(0.4)
    ok = sum(1 for a in R.values() if a)
    print(f'  real-yield panel: {ok}/{len(optional_ids)} series returned data')

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
        ry = next((i for i in model['indicators'] if i['name'] == 'Real 10-Year Yield'), None)
        live_point['Real Yield'] = round(ry['score'], 2) if ry and ry['score'] is not None else None
        fedbs_indicator = next((i for i in model['indicators'] if i['name'] == 'Fed Balance Sheet'), None)
        live_point['Fed Balance Sheet'] = round(fedbs_indicator['score'], 2) if fedbs_indicator and fedbs_indicator['score'] is not None else None
        risk_history.append(live_point)
    print(f'  {len(risk_history)} risk-history points reconstructed (overall + 6 sub-metrics each)')

    model['asset_price_history'] = asset_price_history
    model['risk_history'] = risk_history
    model['asset_risk_map'] = ASSET_RISK_MAP

    real_rows = build_real_yields(R)
    model['real_yields'] = real_rows
    model['real_yields_note'] = (
        'Ex-post real yield = long-term government bond yield minus year-over-year CPI. '
        'This is inflation that has already happened, not the market-priced expectation a '
        'US TIPS yield (DFII10) represents, so it is not the same quantity as the Real '
        '10-Year Yield indicator above. Sources are OECD series via FRED, published monthly '
        'with roughly a month\u2019s lag \u2014 useful for the structural picture across countries, '
        'not for timing. Currency moves are 60 business days, shown as the foreign '
        'currency\u2019s gain against the dollar.')
    print(f'  real-yield panel: {len(real_rows)} of {len(REAL_YIELD_MARKETS)} countries built')
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
    main()#!/usr/bin/env python3
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

# Dropped from the original list: PAYEMS, CPIAUCSL and PCEPI were fetched
# every run and never referenced by any formula; T10Y2Y is superseded by
# T10Y3M, which has the better recession record. Added: DFII10 (the real
# 10-year yield, the most connected variable in macro and previously absent),
# RRPONTSYD (reverse repo — without it the net-liquidity figure was missing
# a facility that held over $2trn at its peak), and T10Y3M.
FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'DFII10', 'T10Y3M', 'DTWEXBGS', 'WALCL', 'WRESBAL', 'WTREGEN',
    'RRPONTSYD', 'NFCI', 'EFFR', 'SOFR25', 'SOFR75', 'DEXJPUS', 'DEXUSEU',
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


# ---------------------------------------------------------------------------
# SIMPLIFIED INDICATOR SET
#
# The previous model carried 18 weighted indicators, but several were reading
# the same series through different transforms and so were counted twice:
#
#   * WALCL / WRESBAL / WTREGEN carried 19% across four indicators — a levels
#     read (Fed Balance Sheet, Bank Reserves, TGA) and a flows read (Liquidity
#     Flow Stress) of one balance sheet. Worse, that flow formula ADDED
#     reserves to assets, when reserves are a liability of the same balance
#     sheet. These collapse into one Fed Net Liquidity indicator using the
#     conventional definition: assets minus TGA minus reverse repo.
#   * VIXCLS carried 10% across VIX and VIX Momentum — one series, two rows.
#     Merged into a single Market Volatility indicator blending level and
#     momentum.
#   * SPY/RSP carried 8% across S&P 500 Breadth and Market Participation
#     Momentum. Those two are the same two spreads with different linear
#     weights and correlate at 0.989 — one signal, billed twice. Merged.
#   * Nominal 2Y and 10Y were each weighted standalone AND inside the Treasury
#     Vol Proxy. Replaced by the real 10-year yield (the variable that actually
#     drives the dollar, gold and long duration) and the 10Y-3M curve.
#
# Result: 13 weighted indicators from 18, with no series feeding two weighted
# rows. Category totals are unchanged, so the headline score stays comparable.
# Two indicators are kept at zero weight for visibility only.
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Credit — 20%
    'HY Credit Spreads':            ('Credit', .13),
    'Investment-Grade Spreads':     ('Credit', .07),
    # Liquidity — 36%
    'Repo-Market Stress':           ('Liquidity', .12),
    'Fed Net Liquidity':            ('Liquidity', .19),
    'DXY / Broad Dollar':           ('Liquidity', .05),
    # Rates — 16%
    'Treasury Vol Proxy (MOVE-style)': ('Rates', .07),
    'Real 10-Year Yield':           ('Rates', .05),
    'Yield Curve (10Y \u2212 3M)':      ('Rates', .04),
    # Market / Macro — 28%
    'Market Volatility':            ('Market / Macro', .10),
    'Equity Breadth':               ('Market / Macro', .08),
    'Financial Conditions (NFCI)':  ('Market / Macro', .04),
    'Jobless Claims Momentum':      ('Market / Macro', .03),
    'Inflation Momentum':           ('Market / Macro', .03),
    # shown but not scored
    'SOFR\u2013IORB Spread':            ('Liquidity', 0),
    'Bank Reserves':                ('Liquidity', 0),
}


def net_liquidity_series(S):
    """Fed net liquidity = total assets \u2212 Treasury general account \u2212 reverse
    repo, the conventional measure of how many dollars are actually loose in
    the system. Built on WALCL's weekly dates, with the other two taken as of
    each of those dates, because the three publish on different schedules.

    RRPONTSYD is reported in $bn while WALCL and WTREGEN are in $mm, hence the
    \u00d71000. It also only begins in 2013; treated as zero before that, which is
    correct \u2014 the facility did not exist."""
    walcl = S.get('WALCL') or []
    tga_s = S.get('WTREGEN') or []
    rrp_s = S.get('RRPONTSYD') or []
    out = []
    for p in walcl:
        d = p['date']
        tga = asof_at(tga_s, d)
        if tga is None:
            continue
        rrp = asof_at(rrp_s, d)
        rrp = (rrp * 1000.0) if rrp is not None else 0.0
        out.append({'date': d, 'value': p['value'] - tga - rrp})
    return out


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
    dxy = L('DTWEXBGS')
    dxyScore = clamp((dxy - 100) * 2, 0, 100) if dxy is not None else None


    wresbal = L('WRESBAL')
    walcl, wtregen = L('WALCL'), L('WTREGEN')
    rrp = L('RRPONTSYD')

    # One measure where there were four. Net liquidity = assets − TGA − RRP,
    # scored on how fast it is moving relative to its own recent history and
    # inverted, so rapid expansion reads calm. The old set scored the levels of
    # three components separately AND their combined flow, putting 19% of the
    # model on one balance sheet read two ways — and it added reserves to
    # assets, double-counting a liability against its own asset side.
    netLiqSeries = net_liquidity_series(S)
    netLiq = netLiqSeries[-1]['value'] if netLiqSeries else None
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    # Still computed, still charted, no longer weighted: this is the one
    # metric-asset pairing with a validated out-of-sample relationship
    # (Short Treasuries vs. Fed balance-sheet momentum), so risk_history keeps
    # carrying it even though the balance sheet now enters the score through
    # net liquidity instead.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), window=120, invert=True)
    reservesScore = momentum_percentile_score(S.get('WRESBAL', []), window=60, invert=True)

    nfci = L('NFCI')
    # calibrated: rapid NFCI TIGHTENING preceded SPY weakness (r=-0.27, n=3317)
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

    # The Liquidity Flow Stress composite that used to live here is gone: its
    # three inputs are now read once, through Fed Net Liquidity.

    # Real 10-year yield: the single most connected variable in macro, and
    # absent from the original model. Drives the dollar through real-rate
    # differentials, gold inversely, and every long-duration valuation.
    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), window=1000)

    # 10Y minus 3M rather than 10Y minus 2Y: the better recession record, and
    # unlike 2s10s it is not simply the difference of two things already scored.
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    # One volatility indicator instead of two rows reading the same series.
    volScore = None
    if None not in (vixScore, vixTermProxy):
        volScore = 0.6 * vixScore + 0.4 * vixTermProxy

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
    # The old pair of breadth indicators used these same two spreads with
    # weights of (10, 5) and (15, 10) respectively — linear combinations so
    # similar that the two scores correlate at 0.989. One indicator, weights
    # midway between the two originals.
    breadthScore = clamp(50 - (breadth5D*12 + breadth20D*7), 0, 100) if None not in (breadth5D, breadth20D) else None

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
        {'name': 'HY Credit Spreads', 'category': 'Credit', 'weight': WEIGHTS['HY Credit Spreads'][1], 'reading': hy, 'units': 'bps', 'score': hyScore,
         'formula': 'Percentile rank of today\u2019s spread within its own trailing 750 observations. 100 = widest in that window.',
         'inputs': [raw('BAMLH0A0HYM2', 'ICE BofA US High Yield option-adjusted spread')]},
        {'name': 'Investment-Grade Spreads', 'category': 'Credit', 'weight': WEIGHTS['Investment-Grade Spreads'][1], 'reading': ig, 'units': 'bps', 'score': igScore,
         'formula': 'Percentile rank within its own trailing 750 observations.',
         'inputs': [raw('BAMLC0A0CM', 'ICE BofA US Corporate option-adjusted spread')]},
        {'name': 'Repo-Market Stress', 'category': 'Liquidity', 'weight': WEIGHTS['Repo-Market Stress'][1], 'reading': repoScore, 'units': '0\u2013100', 'score': repoScore,
         'formula': '0.45 \u00d7 clamp(20 + (SOFR\u2212IORB)\u00d73) + 0.30 \u00d7 clamp(20 + (SOFR\u2212EFFR)\u00d74) + 0.25 \u00d7 clamp((SOFR 75th \u2212 25th)\u00d74), each clamped 0\u2013100',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    raw('EFFR', 'Effective Fed Funds Rate'), raw('SOFR25', 'SOFR 25th percentile'), raw('SOFR75', 'SOFR 75th percentile'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps'), calc('SOFR \u2212 EFFR', sofrEffrBps, 'bps'),
                    calc('SOFR interquartile range', sofrIqrBps, 'bps')]},
        {'name': 'Fed Net Liquidity', 'category': 'Liquidity', 'weight': WEIGHTS['Fed Net Liquidity'][1], 'reading': netLiq, 'units': '$mm', 'score': netLiqScore,
         'formula': 'net liquidity = Fed total assets \u2212 Treasury general account \u2212 reverse repo. Scored on the percentile rank of its 20-week change within its own trailing 120, inverted so rapid expansion reads calm.',
         'inputs': [raw('WALCL', 'Fed total assets'), raw('WTREGEN', 'Treasury general account'),
                    raw('RRPONTSYD', 'Overnight reverse repo ($bn)'),
                    calc('net liquidity', netLiq, '$mm')],
         'flag': 'Replaces four indicators (Fed Balance Sheet, Bank Reserves, TGA, Liquidity Flow Stress) that read the same balance sheet twice over.'},
        {'name': 'DXY / Broad Dollar', 'category': 'Liquidity', 'weight': WEIGHTS['DXY / Broad Dollar'][1], 'reading': dxy, 'units': 'index', 'score': dxyScore,
         'formula': 'clamp((index \u2212 100) \u00d7 2, 0, 100). Reads 0 at or below 100.',
         'inputs': [raw('DTWEXBGS', 'Nominal Broad US Dollar Index')]},
        {'name': 'Treasury Vol Proxy (MOVE-style)', 'category': 'Rates', 'weight': WEIGHTS['Treasury Vol Proxy (MOVE-style)'][1], 'reading': treasuryVolStress, 'units': '0\u2013100', 'score': treasuryVolStress, 'source_note': 'synthetic proxy from 2Y/10Y 5-day moves \u2014 the real MOVE index isn\u2019t freely available via FRED',
         'formula': 'clamp((|2Y 5-day move in bps| \u00d7 0.6 + |10Y 5-day move in bps| \u00d7 0.4) \u00d7 2, 0, 100)',
         'inputs': [raw('DGS2', '2-year Treasury yield'), lag('DGS2', 5, '2-year, 5 sessions ago'),
                    raw('DGS10', '10-year Treasury yield'), lag('DGS10', 5, '10-year, 5 sessions ago'),
                    calc('|2Y 5-day move|', move2, 'bps'), calc('|10Y 5-day move|', move10, 'bps')]},
        {'name': 'Real 10-Year Yield', 'category': 'Rates', 'weight': WEIGHTS['Real 10-Year Yield'][1], 'reading': dfii10, 'units': '%', 'score': realYieldScore,
         'formula': 'Percentile rank of the 10-year TIPS yield within its own trailing 1000 observations.',
         'inputs': [raw('DFII10', '10-year Treasury inflation-indexed yield')],
         'flag': 'New. Replaces the separately-weighted nominal 2Y and 10Y, which were each also counted inside the Treasury Vol Proxy.'},
        {'name': 'Yield Curve (10Y \u2212 3M)', 'category': 'Rates', 'weight': WEIGHTS['Yield Curve (10Y \u2212 3M)'][1], 'reading': t10y3m, 'units': 'pct pts', 'score': curveScore,
         'formula': 'clamp(50 \u2212 25 \u00d7 (10Y \u2212 3M), 0, 100). Reads 50 at a flat curve and rises as it inverts.',
         'inputs': [raw('T10Y3M', '10-year minus 3-month spread')],
         'flag': 'Replaces 2s10s, which carried 0% weight because it was the difference of two already-scored yields. 10Y\u22123M is a distinct series with the stronger recession record.'},
        {'name': 'Market Volatility', 'category': 'Market / Macro', 'weight': WEIGHTS['Market Volatility'][1], 'reading': vix, 'units': 'VIX index', 'score': volScore,
         'formula': '0.6 \u00d7 clamp((VIX \u2212 12) \u00d7 3.2) + 0.4 \u00d7 clamp(50 + (VIX 5-day % change) \u00d7 4), each clamped 0\u2013100.',
         'inputs': [raw('VIXCLS', 'CBOE Volatility Index, close'), lag('VIXCLS', 5, 'VIX, 5 sessions ago'),
                    calc('5-day change', vixChgPct, '%'), calc('level component', vixScore, '0\u2013100'),
                    calc('momentum component', vixTermProxy, '0\u2013100')],
         'flag': 'Merges the old VIX and VIX Momentum rows, which read the same series and together carried 10%.'},
        {'name': 'Equity Breadth', 'category': 'Market / Macro', 'weight': WEIGHTS['Equity Breadth'][1], 'reading': breadthScore, 'units': '0\u2013100', 'score': breadthScore, 'source_note': 'live via SPY/RSP \u2014 unavailable in the original workbook',
         'formula': 'clamp(50 \u2212 (5-day RSP-vs-SPY spread \u00d7 12 + 20-day spread \u00d7 7), 0, 100). Equal-weight lagging cap-weight means a narrow market.',
         'inputs': [eq('SPY', spyL, 'S&P 500 ETF, last close'), eq('RSP', rspL, 'Equal-weight S&P ETF, last close'),
                    calc('5-day breadth spread', breadth5D, '%'), calc('20-day breadth spread', breadth20D, '%')],
         'flag': 'Merges S&P 500 Breadth and Market Participation Momentum, which used these same two spreads and correlated at 0.989.'},
        {'name': 'Financial Conditions (NFCI)', 'category': 'Market / Macro', 'weight': WEIGHTS['Financial Conditions (NFCI)'][1], 'reading': nfci, 'units': 'index', 'score': nfciScore,
         'formula': 'Percentile rank of the 20-observation change within its own trailing 500 \u2014 fast tightening scores high.',
         'inputs': [raw('NFCI', 'Chicago Fed National Financial Conditions Index')]},
        {'name': 'Jobless Claims Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Jobless Claims Momentum'][1], 'reading': econSurpriseScore, 'units': '0\u2013100', 'score': econSurpriseScore,
         'formula': 'clamp(50 + (initial claims 5-week % change) \u00d7 5, 0, 100). Reads 50 when claims are flat.',
         'inputs': [raw('ICSA', 'Initial unemployment claims'), lag('ICSA', 5, '5 weeks ago'),
                    calc('5-week change', claimsChgPct, '%')],
         'flag': 'Renamed from \u201cEconomic Surprise\u201d. A surprise index measures data against consensus forecasts; this measures claims against their own recent level, so the old name overstated it.'},
        {'name': 'Inflation Momentum', 'category': 'Market / Macro', 'weight': WEIGHTS['Inflation Momentum'][1], 'reading': inflationLaborScore, 'units': '0\u2013100', 'score': inflationLaborScore,
         'formula': 'clamp(50 + ((core CPI m/m \u00d7 0.5 + core PCE m/m \u00d7 0.5) \u2212 0.2) \u00d7 200, 0, 100). Reads 50 at 0.2% monthly, roughly the 2% annual target.',
         'inputs': [raw('CPILFESL', 'Core CPI index'), lag('CPILFESL', 1, 'Core CPI, prior month'),
                    raw('PCEPILFE', 'Core PCE index'), lag('PCEPILFE', 1, 'Core PCE, prior month'),
                    calc('core CPI m/m', coreCpiMo, '%'), calc('core PCE m/m', corePceMo, '%')],
         'flag': 'Renamed from \u201cInflation & Labor Momentum\u201d. No labour series ever fed it.'},
        {'name': 'SOFR\u2013IORB Spread', 'category': 'Liquidity', 'weight': 0, 'reading': sofrIorbBps, 'units': 'bps', 'score': sofrIorbScore, 'redundant': 'folded into Repo-Market Stress (45% of that composite)',
         'formula': 'clamp(50 + (SOFR \u2212 IORB in bps) \u00d7 4, 0, 100)',
         'inputs': [raw('SOFR', 'Secured Overnight Financing Rate'), raw('IORB', 'Interest on Reserve Balances'),
                    calc('SOFR \u2212 IORB', sofrIorbBps, 'bps')]},
        {'name': 'Bank Reserves', 'category': 'Liquidity', 'weight': 0, 'reading': wresbal, 'units': '$mm', 'score': reservesScore, 'redundant': 'reserves are a liability of the same balance sheet Fed Net Liquidity already measures \u2014 shown for reference, not scored',
         'formula': 'Percentile rank of the 20-week change within its own trailing 60, inverted.',
         'inputs': [raw('WRESBAL', 'Reserve balances held at Federal Reserve banks')]},
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

    def roc_steps_series(series, window, score, roc_period=20, invert=False, label='net liquidity'):
        """roc_steps() for a series built in code rather than fetched by id."""
        vals = [q['value'] for q in series]
        if score is None or len(vals) < roc_period + 30:
            return []
        rocs = [vals[i] - vals[i - roc_period] for i in range(roc_period, len(vals))]
        pool = rocs[-window:]
        cur = rocs[-1]
        below = sum(1 for v in pool if v < cur)
        out = [f'{label} now {g(vals[-1])}, {roc_period} observations ago {g(vals[-1-roc_period])}',
               f'change = {g(cur)}',
               f'pool = the last {len(pool)} such changes, ranging {g(min(pool))} to {g(max(pool))}',
               f'{below} of {len(pool)} were smaller \u2192 {below/len(pool)*100:.2f}']
        out.append(f'inverted (faster expansion = calmer): 100 \u2212 {below/len(pool)*100:.2f} = {score:.2f}'
                   if invert else f'score = {score:.2f}')
        return out

    step_map = {
        'HY Credit Spreads': pct_steps(hy, 'BAMLH0A0HYM2', 750, hyScore),
        'Investment-Grade Spreads': pct_steps(ig, 'BAMLC0A0CM', 750, igScore),
        'Real 10-Year Yield': pct_steps(dfii10, 'DFII10', 1000, realYieldScore),
        'Bank Reserves': roc_steps('WRESBAL', 60, reservesScore, invert=True),
        'Financial Conditions (NFCI)': roc_steps('NFCI', 500, nfciScore),
    }
    if netLiqSeries and netLiq is not None:
        step_map['Fed Net Liquidity'] = [
            f'net liquidity = assets {g(walcl)} \u2212 TGA {g(wtregen)} \u2212 RRP {g((rrp or 0)*1000)} = {g(netLiq)} $mm',
        ] + roc_steps_series(netLiqSeries, 120, netLiqScore, invert=True)
    if t10y3m is not None:
        step_map['Yield Curve (10Y \u2212 3M)'] = [
            f'10Y \u2212 3M = {f(t10y3m,3)} percentage points',
            f'clamp(50 \u2212 25 \u00d7 {f(t10y3m,3)}) = {f(curveScore)}',
        ]
    if volScore is not None:
        step_map['Market Volatility'] = [
            f'level: clamp(({f(vix)} \u2212 12) \u00d7 3.2) = {f(vixScore)}',
            f'momentum: VIX {f(vix)} vs {f(vixp5)} five sessions ago = {f(vixChgPct)}%',
            f'          clamp(50 + {f(vixChgPct)} \u00d7 4) = {f(vixTermProxy)}',
            f'0.6 \u00d7 {f(vixScore)} + 0.4 \u00d7 {f(vixTermProxy)} = {f(volScore)}',
        ]
    if breadthScore is not None:
        step_map['Equity Breadth'] = [
            f'RSP vs SPY over 5 sessions = {f(breadth5D,3)}%  (equal-weight minus cap-weight)',
            f'RSP vs SPY over 20 sessions = {f(breadth20D,3)}%',
            f'clamp(50 \u2212 ({f(breadth5D,3)} \u00d7 12 + {f(breadth20D,3)} \u00d7 7)) = {f(breadthScore)}',
        ]

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
    if dxy is not None:
        step_map['DXY / Broad Dollar'] = [f'clamp(({f(dxy,4)} \u2212 100) \u00d7 2) = {f(dxyScore)}']
    if claimsChgPct is not None:
        step_map['Jobless Claims Momentum'] = [
            f'claims {f(icsa,0)} vs {f(icsaP5,0)} five weeks ago = {f(claimsChgPct)}%',
            f'clamp(50 + {f(claimsChgPct)} \u00d7 5) = {f(econSurpriseScore)}',
        ]
    if None not in (coreCpiMo, corePceMo):
        blend = coreCpiMo * 0.5 + corePceMo * 0.5
        step_map['Inflation Momentum'] = [
            f'core CPI {f(cpiCore,3)} vs {f(cpiCoreP1,3)} last month = {f(coreCpiMo,3)}% m/m',
            f'core PCE {f(pceCore,3)} vs {f(pceCoreP1,3)} last month = {f(corePceMo,3)}% m/m',
            f'blend = ({f(coreCpiMo,3)} + {f(corePceMo,3)}) \u00f7 2 = {f(blend,3)}%',
            f'clamp(50 + ({f(blend,3)} \u2212 0.2) \u00d7 200) = {f(inflationLaborScore)}',
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
    # Kept because Short Treasuries vs. Fed balance-sheet momentum is the one
    # validated out-of-sample pairing; no longer part of the weighted score.
    fedBsScore = momentum_percentile_score(S.get('WALCL', []), asof_date=date, window=120, invert=True)

    netLiqSeries = [q for q in net_liquidity_series(S) if q['date'] <= date]
    netLiqScore = momentum_percentile_score(netLiqSeries, window=120, invert=True)

    nfci = L('NFCI')
    nfciScore = momentum_percentile_score(S.get('NFCI', []), asof_date=date, window=500)

    dfii10 = L('DFII10')
    realYieldScore = percentile_score(dfii10, S.get('DFII10', []), asof_date=date, window=1000)
    t10y3m = L('T10Y3M')
    curveScore = clamp(50 - 25 * t10y3m, 0, 100) if t10y3m is not None else None

    volScore = 0.6 * vixScore + 0.4 * vixTermProxy if None not in (vixScore, vixTermProxy) else None

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

    spyL, spyP5, spyP20 = EL('SPY'), EP5('SPY'), EP20('SPY')
    rspL, rspP5, rspP20 = EL('RSP'), EP5('RSP'), EP20('RSP')
    breadth5D = breadth20D = None
    if None not in (spyL, spyP5, rspL, rspP5) and spyP5 and rspP5:
        breadth5D = ((rspL/rspP5)/(spyL/spyP5)-1)*100
    if None not in (spyL, spyP20, rspL, rspP20) and spyP20 and rspP20:
        breadth20D = ((rspL/rspP20)/(spyL/spyP20)-1)*100
    breadthScore = clamp(50-(breadth5D*12+breadth20D*7),0,100) if None not in (breadth5D,breadth20D) else None

    # Single source of truth: same WEIGHTS table compute_model() uses, so the
    # reconstructed history can never drift from the live score.
    scored = {
        'HY Credit Spreads': hyScore,
        'Investment-Grade Spreads': igScore,
        'Repo-Market Stress': repoScore,
        'Fed Net Liquidity': netLiqScore,
        'DXY / Broad Dollar': dxyScore,
        'Treasury Vol Proxy (MOVE-style)': treasuryVolStress,
        'Real 10-Year Yield': realYieldScore,
        'Yield Curve (10Y \u2212 3M)': curveScore,
        'Market Volatility': volScore,
        'Equity Breadth': breadthScore,
        'Financial Conditions (NFCI)': nfciScore,
        'Jobless Claims Momentum': econSurpriseScore,
        'Inflation Momentum': inflationLaborScore,
    }

    def cat_avg(category):
        rows = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                if sc is not None and WEIGHTS[n][0] == category and WEIGHTS[n][1] > 0]
        if not rows:
            return None
        wsum = sum(w for _, w in rows)
        return sum(sc*w for sc, w in rows) / wsum if wsum > 0 else None

    liquidity_score = cat_avg('Liquidity')
    credit_score = cat_avg('Credit')
    rates_score = cat_avg('Rates')
    market_score = cat_avg('Market / Macro')

    sofrIorbScore = clamp(50 + sofrIorbBps*4, 0, 100) if sofrIorbBps is not None else None
    y2Score = percentile_score(dgs2, S.get('DGS2', []), asof_date=date, window=1000)
    fedExpScore = 0.6*y2Score + 0.4*sofrIorbScore if None not in (y2Score, sofrIorbScore) else None
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
    fx_stress = fx_composite(*fx_legs)

    contributing = [(sc, WEIGHTS[n][1]) for n, sc in scored.items()
                    if sc is not None and WEIGHTS[n][1] > 0]
    overall_risk = sum(sc*w for sc, w in contributing) if contributing else None

    return {
        'overall_risk': round(overall_risk, 2) if overall_risk is not None else None,
        'Liquidity': round(liquidity_score, 2) if liquidity_score is not None else None,
        'Credit': round(credit_score, 2) if credit_score is not None else None,
        'Rates': round(rates_score, 2) if rates_score is not None else None,
        'Market / Macro': round(market_score, 2) if market_score is not None else None,
        'Inflationary Pressure': round(inflationary_pressure, 2) if inflationary_pressure is not None else None,
        'FX Stress': round(fx_stress, 2) if fx_stress is not None else None,
        'Real Yield': round(realYieldScore, 2) if realYieldScore is not None else None,
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
    # Gold's dominant driver is the real yield, not a rates/Fed/dollar blend.
    # Now that DFII10 is fetched, chart them against the thing itself.
    'U.S. Dollar': 'FX Stress', 'Gold': 'Real Yield', 'Silver': 'Real Yield',
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
        ry = next((i for i in model['indicators'] if i['name'] == 'Real 10-Year Yield'), None)
        live_point['Real Yield'] = round(ry['score'], 2) if ry and ry['score'] is not None else None
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
