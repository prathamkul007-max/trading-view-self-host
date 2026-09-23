"""Self-computed market breadth for indices that don't publish it themselves.

NIFTY/BANKNIFTY/NIFTYIT get advances/declines for free from NSE's own `allIndices`
payload (see cas.breadth_from_all_indices) — no work needed there. Nobody publishes it
for SENSEX or any of the global indices, so wherever a real constituent list can be
gotten without fabricating one, we compute breadth ourselves: pull each constituent's
last two daily closes in one batched yfinance call and count up/down/flat.

Two tiers:
  - Small, hardcoded lists (SENSEX, Dow Jones — 30 stocks each). Stable enough to keep
    in code; polled every CONSTITUENT_POLL_SEC.
  - Large lists scraped live from Wikipedia's own constituent tables (S&P 500 — 503
    rows; CSI 300 — 300 rows) — fetched once and cached for a day, not hand-typed. Each
    is a real, verifiable batch download (measured live: 503/503 and 298/300 resolved
    via yfinance), so accuracy isn't the limiting factor for these two; request volume
    is. Polled on the slower LARGE_INDEX_POLL_SEC so the periodic 500+800-request burst
    doesn't stack on top of the small indices' own traffic every minute.

Still excluded: Nasdaq Composite (~3000 constituents — no clean free list, and the
count alone makes a single poll cycle impractical on an unauthenticated source),
KOSPI, TAIEX, Shanghai Composite (no clean full-constituent table found on Wikipedia
or elsewhere free — a fabricated subset would misrepresent the index, so these stay
without a breadth line rather than guess).
"""
import threading
import time
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

CONSTITUENT_POLL_SEC = 60
LARGE_INDEX_POLL_SEC = 300
CONSTITUENT_LIST_REFRESH_SEC = 86400  # index membership changes a handful of times a year

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# SENSEX's 30 constituents — fetched as .NS (NSE), not .BO. Verified live: Yahoo's .BO
# tickers only ever return a single day of history no matter the requested period, so
# the 2-day comparison this module needs never has a previous close to compare against.
# The same companies' .NS tickers carry full history, since nearly every BSE-listed
# large-cap is dual-listed on NSE — same stock, same closing price, better data source.
# TATAMOTORS.NS 404s: Tata Motors demerged in 2025; TMPV.NS (Tata Motors Passenger
# Vehicles) is the constituent that replaced it in the index.
SENSEX_CONSTITUENTS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "HINDUNILVR.NS",
    "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "KOTAKBANK.NS", "LT.NS",
    "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "SUNPHARMA.NS", "TITAN.NS", "ULTRACEMCO.NS",
    "NESTLEIND.NS", "NTPC.NS", "POWERGRID.NS", "M&M.NS", "TATASTEEL.NS", "TMPV.NS",
    "WIPRO.NS", "HCLTECH.NS", "JSWSTEEL.NS", "INDUSINDBK.NS", "TECHM.NS", "BAJAJFINSV.NS",
]

# Dow Jones Industrial Average (30), as of Sep 2026.
DOWJONES_CONSTITUENTS = [
    "MMM", "AXP", "AMGN", "AMZN", "AAPL", "BA", "CAT", "CVX", "CSCO", "KO",
    "DIS", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "MCD", "MRK", "MSFT",
    "NKE", "NVDA", "PG", "CRM", "SHW", "TRV", "UNH", "VZ", "V", "WMT",
]

SMALL_INDEX_CONSTITUENTS = {"SENSEX": SENSEX_CONSTITUENTS, "DOWJONES": DOWJONES_CONSTITUENTS}

# Each entry describes how to turn that Wikipedia page's constituent table into
# yfinance-ready tickers. Fetched live, not hand-typed — see module docstring.
LARGE_INDEX_SOURCES = {
    "SPX": {
        "wiki_page": "List_of_S%26P_500_companies",
        "table_index": 0,
        "ticker_col": "Symbol",
        "to_yf": lambda sym: sym.replace(".", "-"),  # yfinance wants BRK-B, Wikipedia has BRK.B
    },
    "CHINA": {
        "wiki_page": "CSI_300_Index",
        "table_index": 3,
        "ticker_col": "Ticker",
        # Cell looks like "SSE: 600519" or "SZSE: 300750".
        "to_yf": lambda cell: cell.split(":")[1].strip() + (".SS" if "SSE" in cell.split(":")[0] else ".SZ"),
    },
}

_lock = threading.Lock()
_cache = {}  # alias -> {"advances", "declines", "unchanged"}
_list_cache = {}  # alias -> (tickers, fetched_at) — for the Wikipedia-sourced indices


def summarize(last_two_closes):
    """Pure counting: {ticker: (prev_close, last_close)} -> {advances, declines, unchanged}.
    A ticker with missing/unusable data is skipped, not counted as unchanged."""
    advances = declines = unchanged = 0
    for prev, last in last_two_closes.values():
        if prev is None or last is None:
            continue
        if last > prev:
            advances += 1
        elif last < prev:
            declines += 1
        else:
            unchanged += 1
    return {"advances": advances, "declines": declines, "unchanged": unchanged}


def _fetch_last_two_closes(tickers):
    """One batched yfinance call for all of `tickers` — not one call per stock."""
    df = yf.download(tickers, period="5d", interval="1d", progress=False, group_by="ticker", threads=True)
    out = {}
    for t in tickers:
        try:
            closes = df[t]["Close"].dropna()
            out[t] = (float(closes.iloc[-2]), float(closes.iloc[-1])) if len(closes) >= 2 else (None, None)
        except (KeyError, IndexError, ValueError, TypeError):
            out[t] = (None, None)
    return out


def _fetch_constituent_list(alias):
    cfg = LARGE_INDEX_SOURCES[alias]
    r = requests.get(f"https://en.wikipedia.org/wiki/{cfg['wiki_page']}", headers=_UA, timeout=15)
    r.raise_for_status()
    table = pd.read_html(StringIO(r.text))[cfg["table_index"]]
    return [cfg["to_yf"](v) for v in table[cfg["ticker_col"]].astype(str)]


def _get_constituent_list(alias):
    """Cached for a day — index membership doesn't change often enough to re-scrape
    Wikipedia every poll cycle. Falls back to the last good list on a fetch failure."""
    cached = _list_cache.get(alias)
    now = time.time()
    if cached and now - cached[1] < CONSTITUENT_LIST_REFRESH_SEC:
        return cached[0]
    try:
        tickers = _fetch_constituent_list(alias)
        _list_cache[alias] = (tickers, now)
        return tickers
    except Exception:
        return cached[0] if cached else []


def _update_breadth(alias, tickers):
    if not tickers:
        return
    try:
        result = summarize(_fetch_last_two_closes(tickers))
    except Exception:
        return  # a bad poll must not kill the thread; the cache just keeps its last value
    if result["advances"] + result["declines"] + result["unchanged"] > 0:
        with _lock:
            _cache[alias] = result


def _poll_small():
    while True:
        for alias, tickers in SMALL_INDEX_CONSTITUENTS.items():
            _update_breadth(alias, tickers)
        time.sleep(CONSTITUENT_POLL_SEC)


def _poll_large():
    while True:
        for alias in LARGE_INDEX_SOURCES:
            _update_breadth(alias, _get_constituent_list(alias))
        time.sleep(LARGE_INDEX_POLL_SEC)


def start_constituent_poller():
    threading.Thread(target=_poll_small, daemon=True, name="constituent-breadth-small").start()
    threading.Thread(target=_poll_large, daemon=True, name="constituent-breadth-large").start()


def breadth_rows():
    with _lock:
        return [{"alias": alias, **data} for alias, data in _cache.items()]
