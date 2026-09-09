"""
refresh_fiscal.py

Pulls the FRED series behind the Fiscal Dominance Monitor and writes
fiscal_output.json next to this script. Same pattern as refresh_model.py:
the page loads the JSON if it's there, and only falls back to hitting FRED
from the browser if it isn't.

Standard library only - no pip install needed.

Run:  python refresh_fiscal.py
"""

import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

START = "1990-01-01"

SERIES = [
    # id,            what it is
    ("GDP",                "Nominal GDP, $bn SAAR, quarterly"),
    ("A091RC1Q027SBEA",    "Federal interest payments, $bn SAAR, quarterly"),
    ("FGRECPT",            "Federal current receipts, $bn SAAR, quarterly"),
    ("FGEXPND",            "Federal current expenditures, $bn SAAR, quarterly"),
    ("FYGFDPUN",           "Federal debt held by the public, $mn, quarterly"),
    ("GFDEBTN",            "Total public debt, $mn, quarterly"),
    ("DGS10",              "10y Treasury yield, daily"),
    ("DGS30",              "30y Treasury yield, daily"),
    ("DFII10",             "10y TIPS yield, daily"),
    ("T10YIE",             "10y breakeven inflation, daily"),
    ("THREEFYTP10",        "10y term premium (Kim-Wright), daily"),
    ("DFF",                "Effective fed funds rate, daily"),
    ("PCEPILFE",           "Core PCE price index, monthly"),
    ("TREAST",             "Fed holdings of Treasuries, $mn, weekly"),
    ("WALCL",              "Fed total assets, $mn, weekly"),
]

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
HEADERS = {"User-Agent": "fiscal-monitor/1.0 (personal dashboard)"}


def fetch(series_id, tries=3):
    url = URL.format(sid=series_id, start=START)
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            return parse_csv(text)
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(2 + 2 * attempt)
    raise RuntimeError("could not fetch %s: %s" % (series_id, last_err))


def parse_csv(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    out = []
    for row in rows[1:]:  # first row is the header
        if len(row) < 2:
            continue
        date, raw = row[0].strip(), row[1].strip()
        if not date or raw in (".", "", "NA"):
            continue
        try:
            out.append([date, float(raw)])
        except ValueError:
            continue
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "fiscal_output.json")

    data = {}
    failed = []
    for sid, label in SERIES:
        try:
            obs = fetch(sid)
            data[sid] = obs
            tail = obs[-1] if obs else ["-", "-"]
            print("  ok    %-18s %5d obs   last %s = %s   (%s)"
                  % (sid, len(obs), tail[0], tail[1], label))
        except Exception as err:  # noqa: BLE001
            failed.append(sid)
            print("  FAIL  %-18s %s" % (sid, err))

    if not data:
        print("\nNothing downloaded. Check the internet connection and try again.")
        sys.exit(1)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "FRED (fredgraph.csv)",
        "start": START,
        "failed": failed,
        "series": data,
    }

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, target)

    size_kb = os.path.getsize(target) / 1024.0
    print("\nWrote %s  (%.0f KB, %d series%s)"
          % (target, size_kb, len(data),
             ", %d failed" % len(failed) if failed else ""))


if __name__ == "__main__":
    main()
