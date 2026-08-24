# Macro Liquidity Dashboard — durable, corrected, backtested

Four files, one system:

| File | What it does | Runs |
|---|---|---|
| `refresh_model.py` | Fetches ~27 FRED series + SPY/RSP (Stooq), computes the corrected model, writes `model_output.json` | every 15 min |
| `backtest_asset_outlook.py` | Reconstructs regime history back to 2018, checks it against real forward returns, writes `asset_outlook_backtest.json` | weekly (or on demand) |
| `dashboard.html` | Reads both JSON files. No proxy dependency once these are running. | opened in a browser |
| `.github/workflows/*.yml` | Runs the two scripts on schedule, free, on GitHub's infrastructure | — |

Nothing in the main path depends on a browser-side CORS proxy anymore. FRED
and Yahoo Finance both get hit server-side, where cross-origin restrictions
don't apply — the proxy-based in-browser fetch only remains as a fallback
for before you've deployed this, or if you want to try the dashboard
standalone without any setup.

## Setup (free, ~10 minutes, no server to maintain)

1. Create a **public** GitHub repo, push all four files (keep the
   `.github/workflows/` folder structure).
2. Repo Settings → **Actions → General** → Workflow permissions → **Read
   and write permissions**. (Both workflows commit their JSON output back
   to the repo.)
3. Repo Settings → **Pages** → source = `main` branch, root folder. You'll
   get a URL like `https://<you>.github.io/<repo>/`.
4. Actions tab → run **Refresh macro liquidity model** once manually.
   Confirm `model_output.json` appears in the repo.
5. Actions tab → run **Backtest asset outlook** once manually (takes a few
   minutes — it's pulling 8 years of data). Confirm
   `asset_outlook_backtest.json` appears.
6. Open `https://<you>.github.io/<repo>/dashboard.html`. It picks up both
   files automatically — no config needed.

From here: the model refreshes every 15 minutes, the backtest re-runs every
Monday, and the dashboard always shows current data plus current accuracy
stats, indefinitely, for free.

## What the dashboard shows once both jobs have run at least once

- **Live gauge, category scores, regime label** — recomputed every 15 min
  from real FRED data, using the corrected/de-duplicated model (see below).
- **Asset-Class Outlook table** — 22 assets, direction for the current
  regime, *and a "Backtested Hit Rate" column* showing how often that
  regime's prediction has actually been right since 2018, with sample size.
  Before the backtest has run once, this column is hidden and a banner
  says so plainly — it never shows a fabricated number.
- **Old vs. new model comparison** is in `asset_outlook_backtest.json`
  (`old_model` vs `new_model` keys) if you want to see exactly how much the
  fixes below moved the needle — not currently surfaced on the dashboard
  itself, easy to add if you want it there too.

## What "corrected" means — every change vs. the original workbook

**Bugs fixed (not just design opinions — these were broken regardless of
intent):**
- Bank Reserves / Treasury General Account scoring assumed billions; FRED
  returns millions. Was pinning Bank Reserves at 0 and TGA at 100 (both
  saturated, functionally dead indicators). Fixed by dividing by 1000
  before scoring.
- The `Overall Risk` sum's original range silently excluded the 8%-weighted
  Liquidity Flow Stress row. Now included — it actually counts.
- FX Stress used a row-misaligned lookup table: each "currency pair" was
  actually reading an unrelated economic series (AUD/USD's formula read
  PCEPI inflation data, etc.). Rewired to the correct FRED series per pair
  (DEXJPUS, DEXUSEU, DEXCHUS, DEXSZUS, DEXUSAL, DTWEXBGS).

**Redundancy removed (methodology decision, made explicit and reversible):**
- SOFR–IORB Spread was separately weighted (6%) *and* already 45% of the
  Repo-Market Stress composite (also 6%) — folded together into
  Repo-Market Stress at 12%, standalone SOFR–IORB zeroed.
- Fed Expectations (`0.6×2Y score + 0.4×SOFR-IORB score`) and 2s10s Curve
  (`10Y − 2Y`) are both pure derivatives of indicators counted elsewhere —
  zeroed out, their combined 6% moved to Credit.
- Net effect: **Credit 14%→20%, Rates 22%→16%**, Liquidity and Market/Macro
  unchanged. All zeroed indicators still display (reading + score), just
  don't double-count.

**Coverage gap closed:**
- S&P 500 Breadth and Market Participation Momentum are now live via
  Stooq SPY/RSP daily data. The original workbook's STOCKHISTORY-based
  formulas for these were broken (`#VALUE!`) — that Excel function needs a
  live Microsoft 365 connection the file clearly didn't have when saved.

**Mislabeled proxies renamed, not just relabeled:** "MOVE Index" is
actually a synthetic Treasury-volatility proxy from 2Y/10Y 5-day moves (the
real MOVE index isn't freely available via FRED) — renamed "Treasury Vol
Proxy (MOVE-style)". "VIX Term Structure" is actually a transform of VIX's
own 5-day change, not real futures curve data — renamed "VIX Momentum
Proxy (5D)".

Every one of these is documented on the dashboard itself in the "Model
integrity notes" panel, not just here — so it stays visible to anyone
using the tool without reading this file.

## Backtest methodology, briefly

For every month-end since Jan 2018, `backtest_asset_outlook.py`
reconstructs what the Detected Regime would have been using only data
available as of that date (no lookahead), for both the corrected model and
a faithful replica of the original workbook's formulas — bugs included,
even the FX row-misalignment. It then checks each version's predicted
direction for all 22 assets against real forward 3-month returns.

Caveats that matter before trusting any number it produces:
- "MIXED" predictions are excluded from scoring — no directional claim to
  grade.
- Sample sizes vary a lot by regime (Deflationary/Funding Crisis periods
  are rare) — check `n` before trusting any single regime or asset row.
- It's testing prediction accuracy, not the composite risk score's general
  usefulness as a monitoring tool — those are different claims.

## Running any of this yourself instead of GitHub Actions

Both scripts are plain Python, no dependencies beyond what's noted:

```
python3 refresh_model.py                      # stdlib only
pip install pandas yfinance
python3 backtest_asset_outlook.py              # needs pandas + yfinance
```

Cron examples:

```
*/15 * * * * cd /path/to/repo && python3 refresh_model.py
0 6 * * 1    cd /path/to/repo && python3 backtest_asset_outlook.py
```

Serve `dashboard.html` alongside the two JSON files with any static file
server, or edit `DATA_URL` near the top of the `<script>` block in
`dashboard.html` if you're hosting the JSON somewhere else.
