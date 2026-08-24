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

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

FRED_SERIES = [
    'CPILFESL', 'PCEPILFE', 'PAYEMS', 'CPIAUCSL', 'PCEPI', 'ICSA',
    'BAMLH0A0HYM2', 'BAMLC0A0CM', 'SOFR', 'IORB', 'VIXCLS', 'DGS2',
    'DGS10', 'T10Y2Y', 'DTWEXBGS', 'WALCL',
