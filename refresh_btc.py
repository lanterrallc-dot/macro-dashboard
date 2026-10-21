#!/usr/bin/env python3
"""
refresh_btc.py - builds btc_output.json for btc.html (Bitcoin Liquidity Monitor).

Runs on a schedule in GitHub Actions ("Refresh BTC monitor"). Every data source is
fetched independently; if one fails, that indicator is marked "unavailable" and the
rest of the page still refreshes.

Secrets (repo Settings -> Secrets and variables -> Actions):
  FRED_API_KEY          required  (already set up for the macro dashboard)
  CRYPTOQUANT_API_KEY   optional  enables Exchange Whale Ratio + Estimated Leverage Ratio
  GLASSNODE_API_KEY     optional  enables Long-Term Holder Net Position Change
"""
import bisect
import datetime as dt
import json
import math
import os
import re
import statistics
import time
import urllib.parse
import urllib.request

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
CQ_KEY = os.environ.get("CRYPTOQUANT_API_KEY", "").strip()
GN_KEY = os.environ.get("GLASSNODE_API_KEY", "").strip()
OUT = os.environ.get("BTC_OUTPUT", "btc_output.json")

UA = {"User-Agent": "Mozilla/5.0 (macro-dashboard btc refresh)", "Accept": "*/*"}
TODAY = dt.date.today()
NOW_STAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
START = (TODAY - dt.timedelta(days=3 * 365)).isoformat()


# ----------------------------------------------------------------- helpers
def http(url, headers=None, tries=3, as_json=True):
    h = dict(UA)
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body) if as_json else body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def d(s):
    return dt.date.fromisoformat(s[:10])


def from_unix(t):
    t = float(t)
    if t > 1e11:  # milliseconds
        t /= 1000
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).date()


class Series:
    """Sorted (date, value) list with as-of lookup."""

    def __init__(self, pts):
        pts = sorted((p for p in pts if p[1] is not None and math.isfinite(p[1])), key=lambda p: p[0])
        self.dates = [p[0] for p in pts]
        self.vals = [p[1] for p in pts]

    def __len__(self):
        return len(self.vals)

    def asof(self, day):
        i = bisect.bisect_right(self.dates, day) - 1
        return self.vals[i] if i >= 0 else None

    @property
    def last(self):
        return self.vals[-1] if self.vals else None

    @property
    def last_date(self):
        return self.dates[-1] if self.dates else None

    def since(self, day):
        i = bisect.bisect_left(self.dates, day)
        return Series(list(zip(self.dates[i:], self.vals[i:])))

    def out(self, n=None, digits=4):
        pts = list(zip(self.dates, self.vals))
        if n:
            pts = pts[-n:]
        return [[p[0].isoformat(), round(p[1], digits)] for p in pts]


def weekly_wed(start, end):
    day = start + dt.timedelta(days=(2 - start.weekday()) % 7)
    out = []
    while day <= end:
        out.append(day)
        day += dt.timedelta(days=7)
    return out


def month_ends(start, end):
    out = []
    y, m = start.year, start.month
    while True:
        nxt = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        last = nxt - dt.timedelta(days=1)
        if last > end:
            break
        out.append(last)
        y, m = nxt.year, nxt.month
    return out


def pearson(a, b):
    if len(a) < 8:
        return None
    try:
        return statistics.correlation(a, b)
    except statistics.StatisticsError:
        return None


def fmt_money_b(v):
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"${v / 1000:,.2f}T"
    return f"${v:,.0f}B"


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def indicator(id_, name, **kw):
    base = {"id": id_, "name": name, "status": "na", "display": "-", "detail": "", "trigger": "", "source": "", "series": []}
    base.update(kw)
    return base


def unavailable(id_, name, reason, trigger="", source=""):
    return indicator(id_, name, status="na", display="Unavailable", detail=reason, trigger=trigger, source=source)


PREV = {}  # last good indicators from the previous btc_output.json, keyed by id
PREV_CORR = None


def load_previous():
    global PREV_CORR
    try:
        with open(OUT, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:  # noqa: BLE001
        return
    for g in old.get("groups", []):
        for i in g.get("indicators", []):
            if i.get("status") != "na":
                PREV[i["id"]] = i
    PREV_CORR = old.get("correlation")


def stale_note(asof):
    try:
        then = dt.datetime.strptime(asof, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        hrs = (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 3600
        age = f"{hrs:.0f}h" if hrs < 48 else f"{hrs / 24:.0f} days"
        return f"⚠ Source failed this run; showing last good reading from {age} ago. "
    except Exception:  # noqa: BLE001
        return "⚠ Source failed this run; showing last good reading. "


def guarded(fn, id_, name, trigger, source):
    try:
        res = fn()
        if res.get("status") != "na":
            res["asof_utc"] = NOW_STAMP
            res["detail_fresh"] = res["detail"]
        return res
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {id_}: {type(e).__name__}: {e}")
        old = PREV.get(id_)
        if old:
            keep = dict(old)
            keep["detail"] = stale_note(old.get("asof_utc", "")) + old.get("detail_fresh", old["detail"])
            keep["stale"] = True
            return keep
        return unavailable(id_, name, f"Data source failed this run ({type(e).__name__}).", trigger, source)


# ----------------------------------------------------------------- FRED
_units = {}


def fred(series_id, start=START):
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY, "file_type": "json", "observation_start": start})
    data = http(f"https://api.stlouisfed.org/fred/series/observations?{q}")
    return Series([(d(o["date"]), float(o["value"])) for o in data["observations"] if o["value"] not in (".", "")])


def fred_billions(series_id, start=START):
    """Series converted to billions of its native currency, using FRED's own units label."""
    if series_id not in _units:
        q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY, "file_type": "json"})
        _units[series_id] = http(f"https://api.stlouisfed.org/fred/series?{q}")["seriess"][0]["units"].lower()
    u = _units[series_id]
    if "100 million" in u:
        k = 0.1
    elif "million" in u:
        k = 0.001
    elif "billion" in u:
        k = 1.0
    elif "trillion" in u:
        k = 1000.0
    else:
        raise RuntimeError(f"unexpected units for {series_id}: {u}")
    s = fred(series_id, start)
    return Series([(a, b * k) for a, b in zip(s.dates, s.vals)])


# ----------------------------------------------------------------- shared market data
def btc_price_daily():
    try:
        j = http("https://api.blockchain.info/charts/market-price?timespan=3years&format=json&sampled=false")
        s = Series([(from_unix(p["x"]), float(p["y"])) for p in j["values"]])
        if len(s) > 300:
            return s
    except Exception as e:  # noqa: BLE001
        print(f"[warn] blockchain.info price: {e}")
    j = http("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365&interval=daily")
    return Series([(from_unix(t), float(p)) for t, p in j["prices"]])


def btc_supply():
    try:
        j = http("https://api.blockchain.info/charts/total-bitcoins?timespan=30days&format=json")
        return float(j["values"][-1]["y"])
    except Exception:  # noqa: BLE001
        return 19_950_000.0  # close enough for ratio purposes if the endpoint is down


# ----------------------------------------------------------------- 1. macro liquidity
def build_net_liquidity(ctx):
    walcl = fred_billions("WALCL")
    tga = fred_billions("WTREGEN")
    rrp = fred_billions("RRPONTSYD")
    weeks = weekly_wed(d(START), TODAY)
    nl = Series([(w, walcl.asof(w) - tga.asof(w) - (rrp.asof(w) or 0.0)) for w in weeks if walcl.asof(w) and tga.asof(w)])
    ctx["net_liq"] = nl
    cur = nl.last
    ch4 = cur - nl.asof(nl.last_date - dt.timedelta(days=28))
    ch13 = cur - nl.asof(nl.last_date - dt.timedelta(days=91))
    if ch4 > 0 and ch13 > 0:
        status = "good"
    elif ch4 > 0 or ch13 > 0:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "net_liquidity", "Fed Net Liquidity",
        status=status, display=fmt_money_b(cur),
        detail=f"4-wk change {ch4:+,.0f}B · 13-wk change {ch13:+,.0f}B. Fed assets {fmt_money_b(walcl.last)} − TGA {fmt_money_b(tga.last)} − RRP {fmt_money_b(rrp.last)}.",
        trigger="Green when net liquidity is rising on both a 4-week and 13-week basis.",
        source="FRED: WALCL, WTREGEN, RRPONTSYD",
        series=nl.since(TODAY - dt.timedelta(days=730)).out(digits=1), unit="$B",
    )


def build_gli(ctx):
    fed = fred_billions("WALCL")
    ecb = fred_billions("ECBASSETSW")
    boj = fred_billions("JPNASSETS")
    eurusd = fred("DEXUSEU")
    usdjpy = fred("DEXJPUS")
    pts = []
    for m in month_ends(d(START), TODAY):
        parts = (fed.asof(m), ecb.asof(m), boj.asof(m), eurusd.asof(m), usdjpy.asof(m))
        if None in parts:
            continue
        f, e, b, eu, uj = parts
        pts.append((m, f + e * eu + b / uj))
    # include a live point for the current partial month
    live = (fed.last, ecb.last, boj.last, eurusd.last, usdjpy.last)
    if None not in live and (not pts or pts[-1][0] < TODAY):
        pts.append((TODAY, live[0] + live[1] * live[3] + live[2] / live[4]))
    lvl = Series(pts)
    roc = Series([(lvl.dates[i], (lvl.vals[i] / lvl.vals[i - 3] - 1) * 100) for i in range(3, len(lvl))])
    r_now, r_prev = roc.vals[-1], roc.vals[-2]
    crossed = r_now > 0 and min(roc.vals[-3:-1]) <= 0
    if r_now > 0 and r_now >= r_prev:
        status = "good"
    elif r_now > 0 or r_now > r_prev:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "gli", "Global Liquidity (G3 proxy) · 3-mo RoC",
        status=status, display=f"{r_now:+.2f}%",
        detail=("Positive crossover just happened. " if crossed else "")
        + f"Fed + ECB + BoJ balance sheets in USD: {fmt_money_b(lvl.last)}. Prior reading {r_prev:+.2f}%. "
          "A full 21-central-bank index is paywalled; the G3 make up the bulk of it and move with it.",
        trigger="Green when the 3-month rate of change is above zero and rising (a fresh cross above zero is the signal).",
        source="FRED: WALCL, ECBASSETSW, JPNASSETS, DEXUSEU, DEXJPUS",
        series=roc.since(TODAY - dt.timedelta(days=900)).out(digits=2), unit="%",
    )


# ----------------------------------------------------------------- 2. institutional flows
def parse_farside(html):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
        if len(cells) < 3:
            continue
        m = re.match(r"^(\d{1,2} \w{3} \d{4})$", cells[0])
        if not m:
            continue
        raw = cells[-1].replace(",", "").strip()
        if raw in ("", "-"):
            continue  # day not reported yet
        neg = raw.startswith("(") and raw.endswith(")")
        try:
            v = float(raw.strip("()"))
        except ValueError:
            continue
        rows.append((dt.datetime.strptime(m.group(1), "%d %b %Y").date(), -v if neg else v))
    return Series(rows)


def build_etf_flows(ctx):
    html = http("https://farside.co.uk/bitcoin-etf-flow-all-data/", as_json=False,
                headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"})
    s = parse_farside(html)
    if len(s) < 10:
        raise RuntimeError("could not parse flow table")
    last5 = s.vals[-5:]
    avg5 = sum(last5) / 5
    if all(v > 0 for v in last5) and avg5 >= 200:
        status = "good"
    elif sum(last5) > 0:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "etf_flows", "Spot BTC ETF Net Flows",
        status=status, display=f"{s.last:+,.0f}M",
        detail=f"Latest day ({s.last_date:%b %d}). 5-day avg {avg5:+,.0f}M/day; positive days {sum(v > 0 for v in last5)}/5.",
        trigger="Green on 5 straight days of net inflows averaging ≥ +$200M/day.",
        source="Farside Investors (all US spot BTC ETFs)",
        series=s.out(n=60, digits=1), unit="$M", chart="bar",
    )


def build_stablecoins(ctx):
    chart = http("https://stablecoins.llama.fi/stablecoincharts/all")
    tot = Series([(from_unix(r["date"]), float(r["totalCirculatingUSD"].get("peggedUSD", 0)) / 1e9) for r in chart])
    assets = http("https://stablecoins.llama.fi/stablecoins?includePrices=false")["peggedAssets"]

    def circ(a, key):
        v = (a.get(key) or {}).get("peggedUSD")
        return float(v) / 1e9 if v else None

    usdt_usdc = [a for a in assets if a.get("symbol") in ("USDT", "USDC")]
    now = sum(circ(a, "circulating") or 0 for a in usdt_usdc)
    m_ago = sum(circ(a, "circulatingPrevMonth") or 0 for a in usdt_usdc)
    ch30 = pct(now, m_ago)

    price = ctx["btc_price"]
    supply = ctx["btc_supply"]
    days = [x for x in tot.dates if x >= TODAY - dt.timedelta(days=730)]
    ssr = Series([(x, price.asof(x) * supply / 1e9 / tot.asof(x)) for x in days if price.asof(x) and tot.asof(x)])
    ssr_ch30 = pct(ssr.last, ssr.asof(ssr.last_date - dt.timedelta(days=30)))
    if ch30 and ch30 > 0 and ssr_ch30 is not None and ssr_ch30 <= 0:
        status = "good"
    elif ch30 and ch30 > 0:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "stablecoins", "Stablecoin Supply & SSR",
        status=status, display=f"SSR {ssr.last:.2f}",
        detail=f"USDT + USDC {fmt_money_b(now)} ({ch30:+.1f}% 30d). All USD stablecoins {fmt_money_b(tot.last)}. SSR 30d change {ssr_ch30:+.1f}% (lower = more dry powder vs BTC's size).",
        trigger="Green when USDT+USDC supply is growing and the SSR is flat or falling (dry powder building while BTC stalls).",
        source="DefiLlama stablecoins; BTC price blockchain.com",
        series=ssr.out(digits=3), unit="ratio",
    )


# ----------------------------------------------------------------- 3. on-chain
def cq(path):
    j = http(f"https://api.cryptoquant.com/v1/btc/{path}", headers={"Authorization": f"Bearer {CQ_KEY}"})
    rows = j["result"]["data"]
    pts = []
    for r in rows:
        day = r.get("date") or r.get("datetime")
        val = next((v for k, v in r.items() if k not in ("date", "datetime") and isinstance(v, (int, float))), None)
        if day and val is not None:
            pts.append((d(day), float(val)))
    return Series(pts)


def build_lth(ctx):
    if not GN_KEY:
        return unavailable("lth", "Long-Term Holder Net Position Change",
                           "Needs a Glassnode API key (add GLASSNODE_API_KEY as a repo secret). No free source publishes this.",
                           "Green when the 7-day average flips positive (accumulation).", "Glassnode")
    j = http(f"https://api.glassnode.com/v1/metrics/supply/lth_net_change?a=BTC&i=24h&api_key={GN_KEY}")
    s = Series([(from_unix(r["t"]), float(r["v"])) for r in j]).since(TODAY - dt.timedelta(days=365))
    avg7 = sum(s.vals[-7:]) / 7
    prev7 = sum(s.vals[-14:-7]) / 7
    status = "good" if avg7 > 0 else ("warn" if avg7 > prev7 else "bad")
    return indicator(
        "lth", "Long-Term Holder Net Position Change",
        status=status, display=f"{avg7:+,.0f} BTC/day",
        detail=f"7-day avg ({'accumulating' if avg7 > 0 else 'distributing'}); prior week {prev7:+,.0f}.",
        trigger="Green when the 7-day average flips positive (accumulation).",
        source="Glassnode", series=s.out(digits=0), unit="BTC",
    )


def build_whale(ctx):
    if not CQ_KEY:
        return unavailable("whale", "Exchange Whale Ratio",
                           "Needs a CryptoQuant API key (add CRYPTOQUANT_API_KEY as a repo secret).",
                           "Green below 0.85 (whales no longer dominating exchange deposits).", "CryptoQuant")
    s = cq("flow-indicator/exchange-whale-ratio?exchange=all_exchange&window=day&limit=365")
    avg7 = sum(s.vals[-7:]) / 7
    status = "good" if avg7 < 0.85 and s.last < 0.85 else ("warn" if avg7 < 0.85 or s.last < 0.85 else "bad")
    return indicator(
        "whale", "Exchange Whale Ratio",
        status=status, display=f"{s.last:.3f}", detail=f"7-day avg {avg7:.3f}.",
        trigger="Green below 0.85 (whales no longer dominating exchange deposits).",
        source="CryptoQuant", series=s.out(digits=3), unit="ratio",
    )


# ----------------------------------------------------------------- 4. leverage
def build_elr(ctx):
    if CQ_KEY:
        s = cq("market-indicator/estimated-leverage-ratio?exchange=all_exchange&window=day&limit=365")
        name, src, unit, fmt = "Estimated Leverage Ratio", "CryptoQuant", "ratio", "{:.3f}"
    else:
        j = http("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1D")
        price, supply = ctx["btc_price"], ctx["btc_supply"]
        pts = []
        for row in j["data"]:
            day = from_unix(row[0])
            p = price.asof(day)
            if p:
                pts.append((day, float(row[1]) / (p * supply) * 100))
        s = Series(pts)
        name, src, unit, fmt = "Leverage proxy · OKX open interest / BTC mkt cap", "OKX (add CRYPTOQUANT_API_KEY for true ELR)", "%", "{:.3f}%"
    win = s.since(TODAY - dt.timedelta(days=90))
    hi = max(win.vals)
    med = statistics.median(win.vals)
    off_hi = pct(s.last, hi)
    if off_hi <= -10 and s.last <= med:
        status = "good"
    elif s.last <= med or off_hi <= -10:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "elr", name, status=status, display=fmt.format(s.last),
        detail=f"{off_hi:+.1f}% vs 90-day high; 90-day median {fmt.format(med)}.",
        trigger="Green after a flush: ≥ 10% below the 90-day high and under the 90-day median (spot-led market).",
        source=src, series=s.since(TODAY - dt.timedelta(days=365)).out(digits=4), unit=unit,
    )


def build_funding(ctx):
    rates = None
    src = "OKX BTC-USDT perpetual"
    try:
        pts, after = [], ""
        for _ in range(3):  # ~ 300 funding periods = ~100 days
            j = http("https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100" + (f"&after={after}" if after else ""))
            data = j.get("data") or []
            if not data:
                break
            pts += [(int(r["fundingTime"]), float(r["realizedRate"] or r["fundingRate"]) * 100) for r in data]
            after = data[-1]["fundingTime"]
        if pts:
            rates = sorted(pts)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] okx funding: {e}")
    if not rates:
        end = int(time.time() * 1000)
        j = http(f"https://www.deribit.com/api/v2/public/get_funding_rate_history?instrument_name=BTC-PERPETUAL&start_timestamp={end - 90 * 86400000}&end_timestamp={end}")
        rates = [(r["timestamp"], r["interest_8h"] * 100) for r in j["result"]]
        src = "Deribit BTC-PERPETUAL"
    # daily average of the 8h rate
    by_day = {}
    for t, r in rates:
        by_day.setdefault(from_unix(t), []).append(r)
    s = Series([(k, sum(v) / len(v)) for k, v in by_day.items()])
    avg7 = sum(s.vals[-7:]) / min(7, len(s))
    ann = avg7 * 3 * 365
    if avg7 <= 0.005:
        status = "good"
    elif avg7 <= 0.015:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "funding", "Perp Funding Rate (per 8h)",
        status=status, display=f"{avg7:+.4f}%",
        detail=f"7-day average ≈ {ann:+.1f}% annualized. 0.01% per 8h is the exchange default 'baseline long bias'.",
        trigger="Green at ≤ 0.005% per 8h (neutral or negative: speculative longs flushed).",
        source=src, series=s.out(digits=5), unit="%",
    )


# ----------------------------------------------------------------- macro milestones
def build_core_cpi(ctx):
    s = fred("CPILFESL", (TODAY - dt.timedelta(days=5 * 365)).isoformat())
    yoy = Series([(s.dates[i], (s.vals[i] / s.vals[i - 12] - 1) * 100) for i in range(12, len(s))])
    now, then = yoy.vals[-1], yoy.vals[-4]
    if now < then and now <= 2.5:
        status = "good"
    elif now < then:
        status = "warn"
    else:
        status = "bad"
    return indicator(
        "core_cpi", "Core CPI YoY",
        status=status, display=f"{now:.2f}%",
        detail=f"{yoy.last_date:%b %Y} print; 3 months earlier {then:.2f}%. Fed target 2.0%.",
        trigger="Green when core inflation is falling and at or below 2.5%.",
        source="FRED: CPILFESL", series=yoy.since(TODAY - dt.timedelta(days=3 * 365)).out(digits=2), unit="%",
    )


def build_fed_policy(ctx):
    s = fred("DFEDTARU", (TODAY - dt.timedelta(days=3 * 365)).isoformat())
    lo = fred("DFEDTARL", (TODAY - dt.timedelta(days=60)).isoformat()).last
    ch = s.last - s.asof(s.last_date - dt.timedelta(days=120))
    status = "good" if ch < 0 else ("warn" if ch == 0 else "bad")
    word = "cutting" if ch < 0 else ("on hold" if ch == 0 else "hiking")
    return indicator(
        "fed_policy", "Fed Funds Target",
        status=status, display=f"{lo:.2f}–{s.last:.2f}%",
        detail=f"Fed is {word}: {ch:+.2f} pts over the last ~4 months.",
        trigger="Green while the Fed is cutting; amber on hold; red if hiking.",
        source="FRED: DFEDTARU, DFEDTARL", series=s.out(digits=2), unit="%", chart="step",
    )


def build_balance_sheet(ctx):
    s = fred_billions("WALCL")
    ch13 = pct(s.last, s.asof(s.last_date - dt.timedelta(days=91)))
    status = "good" if ch13 > 0.5 else ("warn" if ch13 > -0.5 else "bad")
    word = "expanding (QE / reserve rebuild)" if ch13 > 0.5 else ("flat" if ch13 > -0.5 else "shrinking (QT)")
    return indicator(
        "balance_sheet", "Fed Balance Sheet (13-wk change)",
        status=status, display=f"{ch13:+.2f}%",
        detail=f"Total assets {fmt_money_b(s.last)}, {word}.",
        trigger="Green when the balance sheet is growing > 0.5% over 13 weeks.",
        source="FRED: WALCL", series=s.since(TODAY - dt.timedelta(days=3 * 365)).out(digits=0), unit="$B",
    )


# ----------------------------------------------------------------- correlation (the headline)
def build_correlation(ctx):
    nl, price = ctx.get("net_liq"), ctx.get("btc_price")
    if not nl or not price:
        return None
    weeks = [w for w in nl.dates if price.asof(w)]
    btc_ret, nl_chg, dates = [], [], []
    for a, b in zip(weeks, weeks[1:]):
        btc_ret.append(math.log(price.asof(b) / price.asof(a)))
        nl_chg.append(nl.asof(b) - nl.asof(a))
        dates.append(b)
    win = 26
    roll = []
    for i in range(win, len(dates) + 1):
        c = pearson(btc_ret[i - win:i], nl_chg[i - win:i])
        if c is not None:
            roll.append((dates[i - 1], c))
    s = Series(roll)
    # 13-week smoothed version (less noisy than weekly changes)
    btc13 = [math.log(price.asof(weeks[i]) / price.asof(weeks[i - 13])) for i in range(13, len(weeks))]
    nl13 = [nl.asof(weeks[i]) - nl.asof(weeks[i - 13]) for i in range(13, len(weeks))]
    c13 = pearson(btc13[-52:], nl13[-52:])
    now = s.last
    status = "good" if now > 0.3 else ("warn" if now > 0 else "bad")
    return {
        "value": round(now, 3),
        "prior": round(s.asof(s.last_date - dt.timedelta(days=28)), 3),
        "smoothed_52w": round(c13, 3) if c13 is not None else None,
        "status": status,
        "series": s.since(TODAY - dt.timedelta(days=730)).out(digits=3),
        "note": "Rolling 26-week correlation of weekly BTC returns with weekly changes in Fed net liquidity.",
    }


# ----------------------------------------------------------------- main
def main():
    load_previous()
    ctx = {}
    try:
        ctx["btc_price"] = btc_price_daily()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] btc price: {e}")
    ctx["btc_supply"] = btc_supply()

    def need_price(fn):
        def w(c):
            if not c.get("btc_price"):
                raise RuntimeError("BTC price unavailable")
            return fn(c)
        return w

    spec = [
        ("macro", "Macro Liquidity", [
            (build_net_liquidity, "net_liquidity", "Fed Net Liquidity", "Rising on 4- and 13-week basis.", "FRED"),
            (build_gli, "gli", "Global Liquidity (G3 proxy)", "3-mo RoC above zero and rising.", "FRED"),
        ]),
        ("flows", "Institutional Capital Flows", [
            (build_etf_flows, "etf_flows", "Spot BTC ETF Net Flows", "5 straight inflow days averaging ≥ +$200M.", "Farside Investors"),
            (need_price(build_stablecoins), "stablecoins", "Stablecoin Supply & SSR", "Supply growing, SSR flat or falling.", "DefiLlama"),
        ]),
        ("onchain", "On-Chain Supply", [
            (build_lth, "lth", "Long-Term Holder Net Position Change", "7-day avg positive.", "Glassnode"),
            (build_whale, "whale", "Exchange Whale Ratio", "Below 0.85.", "CryptoQuant"),
        ]),
        ("leverage", "Market Structure & Leverage", [
            (need_price(build_elr), "elr", "Leverage Ratio", "≥ 10% off 90-day high, under median.", "CryptoQuant / OKX"),
            (build_funding, "funding", "Perp Funding Rate", "≤ 0.005% per 8h.", "OKX / Deribit"),
        ]),
        ("milestones", "Macro Milestones", [
            (build_core_cpi, "core_cpi", "Core CPI YoY", "Falling and ≤ 2.5%.", "FRED"),
            (build_fed_policy, "fed_policy", "Fed Funds Target", "Cutting.", "FRED"),
            (build_balance_sheet, "balance_sheet", "Fed Balance Sheet", "Growing > 0.5% / 13 wk.", "FRED"),
        ]),
    ]

    groups = []
    for gid, title, items in spec:
        inds = [guarded(lambda f=f: f(ctx), i, n, t, s) for f, i, n, t, s in items]
        groups.append({"id": gid, "title": title, "indicators": inds})

    try:
        corr = build_correlation(ctx)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] correlation: {e}")
        corr = None
    if corr is None and PREV_CORR:
        corr = dict(PREV_CORR)
        corr["note"] = "⚠ Data failed this run; showing last good reading. " + corr.get("note_fresh", corr["note"])
    elif corr is not None:
        corr["note_fresh"] = corr["note"]

    # Nothing at all came back (e.g. this runner had no network): keep the old file untouched.
    fresh = [i for g in groups for i in g["indicators"] if i["status"] != "na" and not i.get("stale")]
    if not fresh and PREV:
        print("ERROR: every source failed this run; leaving the previous btc_output.json in place.")
        raise SystemExit(0)

    scored = [i for g in groups for i in g["indicators"] if i["status"] != "na"]
    n_good = sum(i["status"] == "good" for i in scored)
    n_bad = sum(i["status"] == "bad" for i in scored)
    share = n_good / len(scored) if scored else 0
    if share >= 0.6 and n_bad <= 2:
        regime, rstat = "Downward pressure easing", "good"
    elif share >= 0.35:
        regime, rstat = "Transition: mixed signals", "warn"
    else:
        regime, rstat = "Pressure still on", "bad"

    price = ctx.get("btc_price")
    out = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "btc": {"price": round(price.last, 2), "date": price.last_date.isoformat(),
                "chg_30d": round(pct(price.last, price.asof(price.last_date - dt.timedelta(days=30))), 2)} if price else None,
        "regime": {"label": regime, "status": rstat, "good": n_good, "bad": n_bad,
                   "warn": len(scored) - n_good - n_bad, "scored": len(scored),
                   "total": sum(len(g["indicators"]) for g in groups)},
        "correlation": corr,
        "groups": groups,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {OUT}: {regime} ({n_good} green / {n_bad} red of {len(scored)} scored)")


if __name__ == "__main__":
    main()
