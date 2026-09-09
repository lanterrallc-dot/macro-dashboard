#!/usr/bin/env python3
"""
Fiscal Dominance Monitor — server-side refresh.

Pulls the series behind fiscal.html and writes fiscal_output.json, which the
page reads. Same fetch path as refresh_model.py: the official FRED JSON API
when FRED_API_KEY is set, falling back to the public CSV export otherwise.
That matters because the CSV endpoint appears to be blocked or throttled for
GitHub Actions' shared runner IPs, so on the runner the key is effectively
required.

Standard library only — no pip install step needed.

Run manually:      python refresh_fiscal.py
Run on a schedule: see .github/workflows/refresh-fiscal.yml
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

FRED_API_KEY = os.environ.get('FRED_API_KEY', '').strip()

# History start. 1990 is far enough back to show the pre-GFC period where the
# effective rate on the debt sat well above nominal growth, which is the
# comparison the whole page is built around.
START = '1990-01-01'

SERIES = [
    # --- the debt-dynamics identity ---
    ('GDP',             'Nominal GDP, $bn SAAR, quarterly'),
    ('A091RC1Q027SBEA', 'Federal interest payments, $bn SAAR, quarterly'),
    ('FGRECPT',         'Federal current receipts, $bn SAAR, quarterly'),
    ('FGEXPND',         'Federal current expenditures, $bn SAAR, quarterly'),
    ('FYGFDPUN',        'Federal debt held by the public, $mn, quarterly EOP'),
    ('GFDEBTN',         'Total public debt, $mn, quarterly EOP'),
    # --- the market side: marginal cost of new borrowing ---
    ('DGS10',           '10y Treasury yield, daily'),
    ('DGS30',           '30y Treasury yield, daily'),
    ('DFII10',          '10y TIPS yield, daily'),
    ('T10YIE',          '10y breakeven inflation, daily'),
    ('THREEFYTP10',     '10y term premium, Kim-Wright, daily'),
    ('DFF',             'Effective fed funds rate, daily'),
    # --- the Fed side: is the balance sheet absorbing duration? ---
    ('PCEPILFE',        'Core PCE price index, monthly'),
    ('TREAST',          'Fed holdings of Treasuries, $mn, weekly'),
    ('WALCL',           'Fed total assets, $mn, weekly'),
]

# Below this share of series returning data, assume a transient runner network
# problem rather than real unavailability, and leave the last good
# fiscal_output.json in place instead of overwriting it with holes. Same
# reasoning as the guard in refresh_model.py.
MIN_SUCCESS_FRACTION = 0.6

UA = {'User-Agent': 'Mozilla/5.0 (fiscal-dominance-monitor-refresh)'}


def http_get(url, timeout=30, retries=2):
    """Fetch with a couple of backed-off retries — shared CI runners hit bad
    network windows where every request times out at once."""
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


def fetch_series(series_id):
    """Returns [[date, value], ...] ascending, skipping missing observations.
    That pair shape is what fiscal.html parses, so don't change it without
    changing the page's loadFromJSON()."""
    if FRED_API_KEY:
        url = (f'https://api.stlouisfed.org/fred/series/observations'
               f'?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json'
               f'&observation_start={START}')
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
        for ob in data.get('observations', []):
            v = ob.get('value')
            if v in (None, '.', '', 'NA'):
                continue
            try:
                out.append([ob['date'], float(v)])
            except (ValueError, KeyError):
                continue
        return out

    # Fallback: public CSV export. Works locally; expect it to fail on a
    # GitHub Actions runner, which is why the key is set as a repo secret.
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={START}'
    try:
        text = http_get(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f'  WARN: {series_id} fetch failed (CSV export): {e}', file=sys.stderr)
        return []
    out = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(',')
        if len(parts) < 2:
            continue
        date, raw = parts[0].strip(), parts[1].strip()
        if not date or raw in ('.', '', 'NA'):
            continue
        try:
            out.append([date, float(raw)])
        except ValueError:
            continue
    return out


def main():
    if not FRED_API_KEY:
        print('NOTE: FRED_API_KEY not set — falling back to the public CSV export.\n'
              '      Fine locally; on a GitHub Actions runner this endpoint is '
              'usually blocked.\n')

    print(f'Fetching {len(SERIES)} FRED series from {START}...')
    data = {}
    failed = []
    for sid, label in SERIES:
        obs = fetch_series(sid)
        if obs:
            data[sid] = obs
            print(f'  ok    {sid:<18} {len(obs):5d} obs   last {obs[-1][0]} = {obs[-1][1]}   ({label})')
        else:
            failed.append(sid)
            print(f'  FAIL  {sid:<18} no data ({label})')
        time.sleep(0.5)  # small gap: bursts of rapid automated requests get throttled

    print(f'\nFetch summary: {len(data)}/{len(SERIES)} series succeeded')

    if len(data) < len(SERIES) * MIN_SUCCESS_FRACTION:
        print(f'ERROR: only {len(data)}/{len(SERIES)} series succeeded (need at least '
              f'{MIN_SUCCESS_FRACTION*100:.0f}%). Likely a transient network issue on '
              f'this run. Aborting WITHOUT writing fiscal_output.json, so the last good '
              f'data stays live. Will retry on the next scheduled run.', file=sys.stderr)
        sys.exit(1)

    # The four quarterly national-accounts series are the whole point of the
    # page: without them there is no effective rate, no primary deficit and no
    # debt-ratio drift, only a yield table. Treat their absence as a failed run.
    core = ['GDP', 'A091RC1Q027SBEA', 'FGRECPT', 'FGEXPND', 'FYGFDPUN']
    missing_core = [sid for sid in core if sid not in data]
    if missing_core:
        print(f'ERROR: core quarterly series missing ({", ".join(missing_core)}). '
              f'Aborting without writing.', file=sys.stderr)
        sys.exit(1)

    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, 'fiscal_output.json')

    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'FRED official API' if FRED_API_KEY else 'FRED CSV export',
        'start': START,
        'failed': failed,
        'series': data,
    }

    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, separators=(',', ':'))
    os.replace(tmp, target)

    latest_q = data['GDP'][-1][0]
    print(f'Wrote {target}  ({os.path.getsize(target)/1024:.0f} KB, {len(data)} series, '
          f'latest quarter {latest_q}'
          + (f', {len(failed)} failed: {", ".join(failed)}' if failed else '') + ')')


if __name__ == '__main__':
    main()
