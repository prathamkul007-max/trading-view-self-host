import json
import os
import re
import tempfile
import threading
import time as time_module
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as url_quote
import requests
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
import yfinance as yf
from bse_stream import BseStream

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DEFAULT_SYMBOL = "^NSEI"  # Nifty 50 index on Yahoo Finance

SYMBOL_ALIASES = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "NIFTYIT": "^CNXIT",
    "SPX": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOWJONES": "^DJI",
    "KOSPI": "^KS11",
    "TAIEX": "^TWII",
    "CHINA": "000300.SS",  # CSI 300 — mainland China's main broad-market index
    "SSE": "000001.SS",    # Shanghai Composite — kept alongside CSI 300, not instead of it
}

# Left-rail quick-access list: (display label, alias to send to the API).
QUICK_INDICES = [
    ("NIFTY", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("NIFTYIT", "NIFTYIT"),
    ("SENSEX", "SENSEX"), ("SPX", "SPX"), ("NASDAQ", "NASDAQ"), ("DOWJONES", "DOWJONES"),
    ("KOSPI", "KOSPI"), ("TAIEX", "TAIEX"), ("CHINA", "CHINA"), ("SSE", "SSE"),
]

# Cash <-> futures ticker map. Only populated for indices where yfinance actually
# carries a futures contract (verified live) — NSE indices have none, so they're
# simply absent here and the frontend shows no toggle for them.
FUTURES_MAP = {
    "^GSPC": "ES=F",
    "^IXIC": "NQ=F",
    "^DJI": "YM=F",
}

# Readable names for the futures tickers. Note NQ=F is the Nasdaq-100 E-mini, which is
# not the Nasdaq Composite (^IXIC) the NASDAQ rail item charts — the label says so.
FUTURES_META = {
    "ES=F": {"name": "S&P 500 Futures (ES)", "alias": "SPX"},
    "NQ=F": {"name": "Nasdaq-100 Futures (NQ)", "alias": "NASDAQ"},
    "YM=F": {"name": "Dow Futures (YM)", "alias": "DOWJONES"},
}

# Range presets, ordered from shortest to longest. Each maps to the yfinance `period` arg.
RANGE_ORDER = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "5Y", "ALL"]
RANGE_TO_PERIOD = {
    "1D": "1d", "5D": "5d", "1M": "1mo", "3M": "3mo", "6M": "6mo",
    "YTD": "ytd", "1Y": "1y", "5Y": "5y", "ALL": "max",
}

# Bar size ladder, coarsest fallback last. Yahoo's actual intraday history limits:
# 1m ~7 days, 5m/15m ~60 days, 1h ~2 years, 1d unlimited.
INTERVAL_LADDER = ["1m", "5m", "15m", "1h", "1d"]
INTERVAL_MAX_RANGE = {"1m": "5D", "5m": "1M", "15m": "1M", "1h": "1Y", "1d": "ALL"}


def resolve_interval_range(interval, rng):
    """Pick the coarsest interval that can actually serve the requested range.

    Interval (bar size) and range (how much history) are independent controls, but
    Yahoo can't serve e.g. 1-minute bars over a year — rather than erroring, escalate
    to a coarser interval that can, and tell the caller what was actually used.
    """
    if interval not in INTERVAL_LADDER:
        interval = "1d"
    if rng not in RANGE_TO_PERIOD:
        rng = "1D"

    req_idx = RANGE_ORDER.index(rng)
    for candidate in INTERVAL_LADDER[INTERVAL_LADDER.index(interval):]:
        max_idx = RANGE_ORDER.index(INTERVAL_MAX_RANGE[candidate])
        if req_idx <= max_idx:
            return candidate, rng
    return "1d", rng


# Yahoo Finance tickers: letters, digits, ^ . - = are all valid (e.g. ^NSEI, RELIANCE.NS,
# BTC-USD, EURUSD=X, AAPL). Reject anything else to keep this a fixed-format passthrough,
# not a place to smuggle arbitrary strings into outbound requests.
SYMBOL_RE = re.compile(r"^[A-Za-z0-9\^\.\-=]{1,20}$")


def clean_symbol(raw):
    symbol = (raw or DEFAULT_SYMBOL).strip().upper()
    symbol = SYMBOL_ALIASES.get(symbol, symbol)
    if not SYMBOL_RE.match(symbol):
        abort(400, description="invalid symbol")
    return symbol


def drop_spurious_flat_rows(rows):
    """Yahoo occasionally leaks a non-trading-day row (e.g. a Sunday) into daily
    history: open==high==low==close (a duplicated prior close) with zero volume.
    A real session — even a quiet one — essentially never has exactly zero range,
    so this heuristic only ever removes fabricated rows, not real flat trading."""
    return [
        r for r in rows
        if not (r["open"] == r["high"] == r["low"] == r["close"] and r["volume"] <= 0)
    ]


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/config")
def config():
    return jsonify({
        "quickIndices": [alias for _, alias in QUICK_INDICES],
        "futuresMap": FUTURES_MAP,
        "futuresMeta": FUTURES_META,
    })


def df_to_rows(df):
    df = df.dropna()

    def val(row, col):
        v = row[col]
        return float(v.iloc[0]) if hasattr(v, "iloc") else float(v)

    rows = [
        {
            "time": int(ts.timestamp()),
            "open": val(row, "Open"),
            "high": val(row, "High"),
            "low": val(row, "Low"),
            "close": val(row, "Close"),
            "volume": val(row, "Volume"),
        }
        for ts, row in df.iterrows()
    ]
    return drop_spurious_flat_rows(rows)


@app.route("/api/candles")
def candles():
    ensure_poller()
    symbol = clean_symbol(request.args.get("symbol"))
    requested_interval = request.args.get("interval", "1m")
    requested_range = request.args.get("range", "1D")
    interval, rng = resolve_interval_range(requested_interval, requested_range)
    period = RANGE_TO_PERIOD[rng]

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    rows = df_to_rows(df)

    # Splice in bars we built ourselves for feeds Yahoo publishes late (SENSEX).
    live_added = 0
    if symbol in LIVE_BAR_SOURCES and interval in INTERVAL_SECONDS and rows:
        extra = live_bars_after(symbol, rows[-1]["time"], INTERVAL_SECONDS[interval])
        rows += extra
        live_added = len(extra)

    return jsonify({
        "symbol": symbol,
        "interval": interval,
        "range": rng,
        "candles": rows,
        "liveBars": live_added,
    })


# Real yfinance intraday ceilings per bar size — how far back a lazy-load ("drag left")
# request can reach before there's simply no more data to fetch.
HISTORY_PERIOD = {"1m": "7d", "5m": "1mo", "15m": "1mo", "1h": "1y", "1d": "max"}


@app.route("/api/candles/history")
def candles_history():
    symbol = clean_symbol(request.args.get("symbol"))
    interval = request.args.get("interval", "1m")
    if interval not in HISTORY_PERIOD:
        interval = "1m"
    try:
        before = int(request.args.get("before", ""))
    except ValueError:
        abort(400, description="before must be a unix timestamp")

    df = yf.download(symbol, period=HISTORY_PERIOD[interval], interval=interval, progress=False)
    rows = [r for r in df_to_rows(df) if r["time"] < before]
    # One page per call: the most recent trading day strictly before `before`, so the
    # frontend can prepend it and the session-boundary marker lands cleanly.
    if rows:
        last_day = time_module.gmtime(rows[-1]["time"] + 19800).tm_yday  # +5:30 for IST day boundary
        rows = [r for r in rows if time_module.gmtime(r["time"] + 19800).tm_yday == last_day]

    return jsonify({
        "symbol": symbol,
        "interval": interval,
        "candles": rows,
        "exhausted": len(rows) == 0,
    })


# ---------------------------------------------------------------------------
# Live quotes. Measured on 2026-09-21 with the market open:
#   * Yahoo's chart endpoint ticks every ~11s for NSE names and is always <=11s old.
#     (interval=1d&range=1d keeps the payload ~1 KB and still carries the live tick.)
#   * NSE's allIndices feed only refreshes ~once a minute and runs 1-2 min behind the
#     wall clock, so it is NOT used.
#   * Yahoo's SENSEX (^BSESN) is delayed a flat ~15 min (licensing); BSE's own feed is
#     current to the minute, so SENSEX comes from BSE.
# Every source is best-effort and falls through to the next one.
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

_yahoo = requests.Session()
_yahoo.headers.update(_UA)
_bse = requests.Session()
_bse.headers.update({**_UA, "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
                     "Accept": "application/json, text/plain, */*"})

QUOTE_CACHE_TTL = 1.5  # seconds; coalesces concurrent polls (several tabs / retries)
_quote_cache = {}


def _cached(key, fn):
    now = time_module.time()
    hit = _quote_cache.get(key)
    if hit and now - hit[0] < QUOTE_CACHE_TTL:
        return hit[1]
    value = fn()
    _quote_cache[key] = (now, value)
    return value


def yahoo_live_quote(symbol):
    def fetch():
        try:
            r = _yahoo.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + url_quote(symbol, safe=""),
                params={"interval": "1d", "range": "1d"}, timeout=4,
            )
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            return {
                "price": float(meta["regularMarketPrice"]),
                "prevClose": float(meta["chartPreviousClose"]),
                "marketTime": int(meta["regularMarketTime"]),
                "currency": meta.get("currency", ""),
            }
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            return None
    return _cached(("yahoo", symbol), fetch)


def bse_live_quote(_symbol=None):
    # BSE's push stream ticks ~2x/second; the REST endpoint below refreshes once a
    # minute. Use the stream whenever it is alive and only fall back to REST.
    snap = bse_stream.snapshot() if bse_stream.fresh() else None
    if snap:
        return {"price": snap["value"], "prevClose": snap["prevClose"],
                "marketTime": snap["t"], "currency": "INR"}

    def fetch():
        try:
            r = _bse.get("https://api.bseindia.com/RealTimeBseIndiaAPI/api/GetSensexData/w", timeout=4)
            r.raise_for_status()
            row = r.json()[0]
            # "dttm" looks like "21 Sep 26 | 12:28" (IST, minute resolution).
            stamp = datetime.strptime(row["dttm"], "%d %b %y | %H:%M").replace(tzinfo=IST)
            return {
                "price": float(row["ltp"].replace(",", "")),
                "prevClose": float(row["Prev_Close"].replace(",", "")),
                "marketTime": int(stamp.timestamp()),
                "currency": "INR",
            }
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            return None
    return _cached(("bse", "sensex"), fetch)


def yfinance_quote(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = float(info["lastPrice"])
        prev = info.get("previousClose") if hasattr(info, "get") else info["previousClose"]
        if prev is None:
            closes = t.history(period="5d", interval="1d")["Close"].dropna()
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else price
        return {"price": price, "prevClose": float(prev), "marketTime": None, "currency": info.get("currency", "")}
    except Exception:
        return None


# Yahoo's feed for the NSE indices stops at the auction: measured on 21 Sep 2026 its last tick
# was 15:17:32 and never carried the closing value (official NIFTY close 23414.30 vs the 23429.00
# Yahoo still reported an hour later). NSE's own index feed does carry the close, so it takes over
# whenever Yahoo's tick is stale and NSE's stamp is newer.
NSE_INDEX_BY_YAHOO = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK", "^CNXIT": "NIFTY IT"}
YAHOO_STALE_SEC = 120


def nse_index_quote(symbol):
    name = NSE_INDEX_BY_YAHOO.get(symbol)
    if not name:
        return None

    def fetch():
        d = nse_get("/api/allIndices")
        if not d:
            return None
        for row in d.get("data", []):
            if row.get("index") == name and row.get("last") is not None:
                try:
                    stamp = datetime.strptime(d.get("timestamp", ""), "%d-%b-%Y %H:%M").replace(tzinfo=IST)
                    return {"price": float(row["last"]), "prevClose": float(row["previousClose"]),
                            "marketTime": int(stamp.timestamp()), "currency": "INR"}
                except (ValueError, KeyError, TypeError):
                    return None
        return None
    return _cached(("nse-index", symbol), fetch)


# Symbols whose best live source is not Yahoo.
SPECIAL_QUOTE_SOURCES = {"^BSESN": ("bse", bse_live_quote)}

# ---------------------------------------------------------------------------
# Live bar aggregation.
# Yahoo delays SENSEX *bars* by ~15 minutes (the quote fix above only covered the
# price). There is no public intraday-bar feed for the index, so we build the recent
# bars ourselves: poll BSE every few seconds and fold each tick into a 1-minute OHLC
# bucket. /api/candles then splices these onto Yahoo's older, authoritative history.
# Coverage is "since this process started", which is why Yahoo still supplies history.
# ---------------------------------------------------------------------------
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
LIVE_BAR_SOURCES = {"^BSESN": bse_live_quote}
LIVE_BAR_POLL_SEC = 3
_live_bars = {}          # yahoo symbol -> {minute_epoch: [open, high, low, close]}
_live_bars_lock = threading.Lock()
_poller_started = False


def record_live_tick(symbol, price, market_time, sampled=True):
    """Fold one sample into its 1-minute bucket.

    Measured: BSE publishes exactly one SENSEX value per minute, so this is a sampled
    series, not a tick stream. A new bar therefore opens at the PREVIOUS sample —
    otherwise every candle would be flat (open == high == low == close). The high/low
    are the range across the samples we saw, not true intra-minute extremes.
    """
    minute = market_time - (market_time % 60)
    with _live_bars_lock:
        bars = _live_bars.setdefault(symbol, {})
        bar = bars.get(minute)
        if bar is None:
            earlier = [m for m in bars if m < minute]
            # `sampled` = once-a-minute REST values: open at the previous sample so bars
            # aren't flat. Real stream ticks open at the minute's first tick instead.
            start = bars[max(earlier)][3] if (sampled and earlier) else price
            bars[minute] = [start, max(start, price), min(start, price), price]
        else:
            bar[1] = max(bar[1], price)
            bar[2] = min(bar[2], price)
            bar[3] = price
        if len(bars) > 1200:  # ~a full session; drop the oldest
            for stale in sorted(bars)[:-900]:
                del bars[stale]


def _live_bar_poller():
    while True:
        for symbol, fetch in LIVE_BAR_SOURCES.items():
            try:
                # The stream records its own ticks; polling REST on top would only add
                # coarse once-a-minute samples to buckets that already hold real ticks.
                if symbol == "^BSESN" and bse_stream.fresh():
                    continue
                q = fetch(symbol)
                # Only record a tick the exchange published just now: a frozen
                # post-close quote must not keep extending flat bars forever.
                if q and q.get("marketTime") and time_module.time() - q["marketTime"] < 180:
                    record_live_tick(symbol, q["price"], q["marketTime"])
            except Exception:
                pass  # a transient source failure must never kill the poller
        time_module.sleep(LIVE_BAR_POLL_SEC)


def _on_bse_tick(tick):
    record_live_tick("^BSESN", tick["value"], tick["t"], sampled=False)


bse_stream = BseStream(on_tick=_on_bse_tick)


def ensure_poller():
    global _poller_started
    if not _poller_started:
        _poller_started = True
        bse_stream.start()
        threading.Thread(target=_live_bar_poller, daemon=True, name="live-bars").start()
        threading.Thread(target=_seed_loop, daemon=True, name="cas-seed").start()
        ensure_nse_poller()


def live_bars_after(symbol, after_ts, step):
    """Our own bars strictly newer than `after_ts`, re-bucketed to `step` seconds."""
    with _live_bars_lock:
        minutes = dict(_live_bars.get(symbol, {}))
    buckets = {}
    for minute in sorted(minutes):
        if minute <= after_ts:
            continue
        o, h, l, c = minutes[minute]
        slot = minute - (minute % step)
        cur = buckets.get(slot)
        if cur is None:
            buckets[slot] = [o, h, l, c]
        else:
            cur[1] = max(cur[1], h)
            cur[2] = min(cur[2], l)
            cur[3] = c
    return [{"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 0}
            for t, (o, h, l, c) in sorted(buckets.items())]


@app.route("/api/quote")
def quote():
    ensure_poller()
    symbol = clean_symbol(request.args.get("symbol"))

    source, q = None, None
    if symbol in SPECIAL_QUOTE_SOURCES:
        source, fn = SPECIAL_QUOTE_SOURCES[symbol]
        q = fn(symbol)
    if q is None:
        source, q = "yahoo", yahoo_live_quote(symbol)
        if q is not None and symbol in NSE_INDEX_BY_YAHOO and time_module.time() - q["marketTime"] > YAHOO_STALE_SEC:
            nse_q = nse_index_quote(symbol)
            if nse_q and nse_q["marketTime"] > q["marketTime"]:
                source, q = "nse", nse_q
    if q is None:
        source, q = "yfinance", yfinance_quote(symbol)
    if q is None:
        abort(502, description="no quote source available")

    now = int(time_module.time())
    price, prev_close = q["price"], q["prevClose"]
    change = price - prev_close
    return jsonify({
        "symbol": symbol,
        "price": price,
        "time": now,
        "marketTime": q["marketTime"],
        "delaySec": max(0, now - q["marketTime"]) if q["marketTime"] else None,
        "change": change,
        "changePercent": (change / prev_close) * 100 if prev_close else 0,
        "currency": q["currency"],
        "source": source,
    })


# ---------------------------------------------------------------------------
# Call Auction Session (CAS).
#   Pre-open  09:00-09:15 IST: orders collected 09:00-09:08, matched 09:08-09:12.
#     Price discovery is real here, and NSE publishes each stock's IEP (indicative
#     equilibrium price). The index level it implies shows up as that index's `open`.
#   Post-close 15:40-16:00 IST: this is NOT price discovery — it is trading AT the
#     already-determined close, so there is no IEP. What is meaningful is the
#     indicative/official closing value, which NSE and BSE both publish.
# Only NSE/BSE indices have a public auction feed; the rest are reported as such
# rather than filled with a guess.
# ---------------------------------------------------------------------------
# Verified against NSE and broker documentation (Sept 2026), not assumed:
#   SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/2765/2026 (16 Jan 2026) introduced the
#   Closing Auction Session, live from 3 August 2026, for F&O-eligible stocks only.
#   Continuous trading in those names now STOPS at 15:15 — it does not run to 15:30.
#   Non-F&O stocks keep the old VWAP close and trade through to 15:30.
CAS_SESSIONS = [
    {
        "phase": "pre-open", "label": "Pre-open auction", "start": (9, 0), "end": (9, 15),
        "stages": [
            ((9, 0), (9, 5), "Order entry — market & limit"),
            ((9, 5), (9, 10), "Limit orders only — closes randomly 09:08-09:10"),
            ((9, 10), (9, 12), "Matching & opening price"),
            ((9, 12), (9, 15), "Buffer before continuous trading"),
        ],
    },
    {
        "phase": "closing-auction", "label": "Closing auction (CAS)", "start": (15, 15), "end": (15, 35),
        "stages": [
            ((15, 15), (15, 20), "Reference price (VWAP 15:00-15:15) — no new orders"),
            ((15, 20), (15, 25), "Order entry — market & limit"),
            ((15, 25), (15, 30), "Limit orders only — closes randomly 15:28-15:30"),
            ((15, 30), (15, 35), "Matching, confirmation & closing price"),
        ],
    },
    {
        "phase": "post-market", "label": "Post-market", "start": (15, 50), "end": (16, 0),
        "stages": [((15, 50), (16, 0), "Market orders at the closing price")],
    },
]

# Rail alias -> the name NSE's allIndices feed uses.
NSE_INDEX_NAMES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "NIFTYIT": "NIFTY IT"}

_nse = requests.Session()
_nse.headers.update({**_UA, "Accept": "application/json"})
_nse_cookie_at = 0


def nse_get(path, params=None):
    """NSE needs a cookie from the landing page before its JSON endpoints answer."""
    global _nse_cookie_at
    now = time_module.time()
    if now - _nse_cookie_at > 240:
        try:
            _nse.get("https://www.nseindia.com", timeout=5)
            _nse_cookie_at = now
        except requests.RequestException:
            return None
    try:
        r = _nse.get("https://www.nseindia.com" + path, params=params, timeout=6)
        if r.status_code != 200:
            _nse_cookie_at = 0
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        _nse_cookie_at = 0
        return None


def _mins(hm):
    return hm[0] * 60 + hm[1]


def nse_session():
    """What NSE itself says is running right now.

    Clock windows are a guess that goes stale whenever SEBI changes the timetable
    (a closing auction session has been under consultation), so the live session is
    read from NSE and the clock is only used for the countdown. Returns
    (phase or None, status message, indicative index dict or None).
    """
    d = _cached(("nse-status", ""), lambda: nse_get("/api/marketStatus"))
    if not d:
        return None, None, None

    message = ""
    for m in d.get("marketState", []):
        if m.get("market") == "Capital Market":
            message = m.get("marketStatusMessage") or ""
            break

    ind = d.get("indicativenifty50") or {}
    # Populated only while an auction is actually matching; "CLOSE" means no auction.
    live_auction = bool(ind.get("indicativeTime")) and (ind.get("status") or "").upper() != "CLOSE"
    indicative = {
        "value": ind.get("indexLast"),
        "change": ind.get("change"),
        "percentChange": ind.get("indexPercChange") if ind.get("indexPercChange") is not None else ind.get("perChange"),
        "time": ind.get("indicativeTime") or ind.get("dateTime"),
        "status": ind.get("status"),
    } if ind else None

    low = message.lower()
    phase = None
    if "pre open" in low or "pre-open" in low:
        phase = "pre-open"
    elif "closing" in low or "post close" in low:
        phase = "post-close"
    elif live_auction:
        phase = "auction"  # NSE is matching an auction the clock windows don't know about
    return phase, message, indicative


def cas_phase(now_ist):
    """Which auction window we are in, its current stage, and seconds to its start/end.

    Returns (phase, seconds, stage, label). `phase` is a session name while one is
    running, otherwise "waiting:<next session>".
    """
    mins = now_ist.hour * 60 + now_ist.minute + now_ist.second / 60
    weekday = now_ist.weekday() < 5

    if weekday:
        for sess in CAS_SESSIONS:
            if _mins(sess["start"]) <= mins < _mins(sess["end"]):
                stage = next((text for st, en, text in sess["stages"]
                              if _mins(st) <= mins < _mins(en)), None)
                return sess["phase"], int((_mins(sess["end"]) - mins) * 60), stage, sess["label"]

        upcoming = [(_mins(s["start"]) - mins, s) for s in CAS_SESSIONS
                    if _mins(s["start"]) > mins]
        if upcoming:
            delta, sess = min(upcoming, key=lambda x: x[0])
            return "waiting:" + sess["phase"], int(delta * 60), None, sess["label"]

    return "waiting:pre-open", None, None, "Pre-open auction"


@app.route("/api/cas")
def cas():
    ensure_poller()
    now_ist = datetime.now(IST)
    clock_phase, seconds, stage, session_label = cas_phase(now_ist)
    nse_phase, nse_message, indicative = nse_session()
    # NSE wins when it names a session; the clock only supplies the countdown.
    phase = nse_phase or clock_phase
    if nse_phase and nse_phase != clock_phase:
        seconds = None

    all_idx = nse_get("/api/allIndices") or {}
    by_name = {row.get("index"): row for row in all_idx.get("data", [])}

    rows = []
    for _label, alias in QUICK_INDICES:
        nse_name = NSE_INDEX_NAMES.get(alias)
        if nse_name and nse_name in by_name:
            r = by_name[nse_name]
            prev = r.get("previousClose") or 0
            open_ = r.get("open") or 0
            icls = r.get("indicativeClose") or 0
            rows.append({
                "alias": alias, "available": True, "source": "nse",
                "prevClose": prev,
                "open": open_ or None,
                "openChangePct": ((open_ - prev) / prev * 100) if (open_ and prev) else None,
                "indicativeClose": icls or None,
                "indicativeClosePct": ((icls - prev) / prev * 100) if (icls and prev) else None,
                "last": r.get("last"),
            })
            continue
        if alias == "SENSEX":
            # BSE publishes the same two numbers for SENSEX on its own feed.
            q = bse_live_quote(None) or {}
            raw = _bse_raw_sensex()
            icls = raw.get("indicativeClose")
            prev = q.get("prevClose")
            rows.append({
                "alias": alias, "available": bool(q), "source": "bse",
                "prevClose": prev,
                "open": raw.get("open"),
                "openChangePct": ((raw["open"] - prev) / prev * 100) if (raw.get("open") and prev) else None,
                "indicativeClose": icls,
                "indicativeClosePct": ((icls - prev) / prev * 100) if (icls and prev) else None,
                "last": q.get("price"),
            })
            continue
        rows.append({"alias": alias, "available": False,
                     "reason": "No public call-auction feed for this market"})

    breadth = None
    if phase in ("pre-open", "closing-auction"):
        pre = nse_get("/api/market-data-pre-open", {"key": "FO"})
        if pre:
            breadth = {
                "advances": pre.get("advances"), "declines": pre.get("declines"),
                "unchanged": pre.get("unchanged"), "tradedValue": pre.get("totalTradedValue"),
                "asOf": pre.get("timestamp"),
            }

    return jsonify({
        "phase": phase,
        "clockPhase": clock_phase,
        "stage": stage,
        "sessionLabel": session_label,
        "secondsRemaining": seconds,
        "serverTime": int(time_module.time()),
        # Straight from NSE, so the UI can show the session's real name instead of
        # whatever our hard-coded windows assume.
        "nseStatus": nse_message,
        "indicativeNifty": indicative,
        "windows": {s["phase"]: "%02d:%02d-%02d:%02d IST" % (s["start"] + s["end"])
                    for s in CAS_SESSIONS},
        "breadth": breadth,
        "indices": rows,
    })


def _cas_payload():
    """NSE's dedicated closing-auction feed.

    This is what nseindia.com/market-data/closing-auction-session calls; the path was
    read out of that page's JavaScript bundle (it is documented nowhere else). Outside
    the 15:15-15:35 window `data` is empty but `symbols` still lists the eligible names.
    """
    return _cached(("cas-api", ""), lambda: nse_get(
        "/api/NextApi/apiClient/casApi", {"functionName": "getCASData"}))


def normalise_cas_rows(payload):
    """Map NSE's CAS row fields to ours. Verified against the live 15:20 payload (210 rows):
      IEP        indicative equilibrium price. NOT `finalPrice`, which stays 0 until matching.
      iiqAtMO    indicative imbalance from market orders = atoBuy - atoSell (210/210 rows),
                 so positive means a buy surplus.
      iiqAtEP    indicative imbalance at the equilibrium price (same sign convention assumed).
      change/perChange are measured against `refrencePrice` (NSE's spelling).
    """
    rows = []
    for r in (payload or {}).get("data") or []:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        rows.append({
            "symbol": r["symbol"],
            "refPrice": r.get("refrencePrice"),
            "iep": r.get("IEP"),
            "finalPrice": r.get("finalPrice"),
            "change": r.get("change"),
            "pChange": r.get("perChange"),
            "imbalanceEP": r.get("iiqAtEP"),
            "imbalanceMO": r.get("iiqAtMO"),
            "buyQty": r.get("totalBuyQuantity"),
            "sellQty": r.get("totalSellQuantity"),
            "atoBuy": r.get("atoBuyQuantity"),
            "atoSell": r.get("atoSellQuantity"),
            "lastPrice": r.get("lastTradedPrice"),
            "upperBand": r.get("upperBand"),
            "lowerBand": r.get("lowerBand"),
            "bestBid": r.get("bestBidPrice"), "bestBidQty": r.get("bestBidQty"),
            "bestAsk": r.get("bestAskPrice"), "bestAskQty": r.get("bestAskQty"),
            "tradedQty": r.get("totTradedQty"),
            "finalQuantity": r.get("finalQuantity"),
            "finalValue": r.get("finalValue"),
            "orderBook": r.get("orderBook"),
        })
    return rows


def normalise_cas_book(order_book):
    """`orderBook` items: price / buyQuantity / sellQuantity / flag. Observed: `flag` is a
    boolean that is true exactly on the equilibrium-price rung (38/38 rungs had price == IEP)."""
    items = order_book if isinstance(order_book, list) else []
    return [{"price": i.get("price"), "buyQty": i.get("buyQuantity"),
             "sellQty": i.get("sellQuantity"), "flag": None, "isIep": i.get("flag") is True}
            for i in items if isinstance(i, dict)]


# ---------------------------------------------------------------------------
# Live CAS movement: how each index's value (and, during an auction, its indicative
# close) moves over time. SENSEX comes from BSE's push stream; the NSE indices are
# sampled from allIndices (which NSE refreshes about once a minute, so their series is
# coarser). Series live in memory since process start.
# ---------------------------------------------------------------------------
NSE_POLL_SEC = 3
_nse_series = {alias: deque(maxlen=6000) for alias in NSE_INDEX_NAMES}   # (t, last, indicative)
_nse_prev = {}
_nse_started = False
# Traded value comes from Yahoo's real-time tick (measured <=11s old); NSE's allIndices
# lags 1-2 minutes, so it is used only for what Yahoo lacks: the indicative close.
YAHOO_INDEX_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "NIFTYIT": "^CNXIT"}


def _nse_index_poller():
    while True:
        try:
            d = nse_get("/api/allIndices") or {}
            by = {r.get("index"): r for r in d.get("data", [])}
            _p, _m, ind = nse_session()
            for alias, name in NSE_INDEX_NAMES.items():
                r = by.get(name) or {}
                y = yahoo_live_quote(YAHOO_INDEX_SYMBOL[alias])
                last = y["price"] if y else r.get("last")
                if last is None:
                    continue
                t = y["marketTime"] if y else int(time_module.time())
                icls = r.get("indicativeClose") or None
                if alias == "NIFTY" and ind and ind.get("value"):
                    icls = icls or ind["value"]
                _nse_prev[alias] = (y or {}).get("prevClose") or r.get("previousClose")
                series = _nse_series[alias]
                # keep only changes: Yahoo's tick moves every ~11s, the indicative every ~1 min
                if not series or (series[-1][1], series[-1][2]) != (last, icls):
                    series.append((t, last, icls))
        except Exception:
            pass  # a bad poll must not kill the thread
        time_module.sleep(NSE_POLL_SEC)


def ensure_nse_poller():
    global _nse_started
    if not _nse_started:
        _nse_started = True
        threading.Thread(target=_nse_index_poller, daemon=True, name="nse-index").start()


# ---- The reference price -----------------------------------------------------------------------
# The auction is measured against the LAST VALUE AT 15:14:59, i.e. the final traded value before
# continuous trading stops at 15:15. Once found it is locked for the day.
CAS_REFERENCE_AT = (15, 14, 59)
_cas_reference = {}   # (yyyy-mm-dd, alias) -> {"value","t","source"}


# The reference price is locked once for the day and the chart history lives in memory, so a
# server restart (Flask's debug reloader restarts on every edit to this file) would silently
# wipe both mid-auction. Persist them every few seconds and reload on start.
SEED_FILE = os.path.join(tempfile.gettempdir(), "orazio_cas_seed.json")


def _save_seed():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    seed = {
        "date": today,
        "refs": {alias: ref for (day, alias), ref in _cas_reference.items() if day == today},
        "series": {**{a: [list(p) for p in list(_nse_series[a])[-3000:]] for a in _nse_series},
                   "SENSEX": [list(p) for p in bse_stream.series(3000)]},
    }
    tmp = SEED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    os.replace(tmp, SEED_FILE)


def _load_seed():
    """Restore today's reference and series after a restart. Ignored if it is from another day."""
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            seed = json.load(f)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if seed.get("date") != today:
            return
        for alias, ref in (seed.get("refs") or {}).items():
            _cas_reference[(today, alias)] = ref
        for alias, pts in (seed.get("series") or {}).items():
            if alias == "SENSEX":
                bse_stream.history.extend((p[0], p[1], p[2]) for p in pts)
            elif alias in _nse_series:
                _nse_series[alias].extend((p[0], p[1], p[2]) for p in pts)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass  # no usable seed: start fresh


def _seed_loop():
    while True:
        time_module.sleep(10)
        try:
            _save_seed()
        except Exception:
            pass


def cas_reference_epoch(now_ist):
    h, m, sec = CAS_REFERENCE_AT
    return int(now_ist.replace(hour=h, minute=m, second=sec, microsecond=0).timestamp())


def _last_at_or_before(points, epoch, max_gap=120):
    """Last (t, value) with t <= epoch, provided it is not implausibly stale."""
    best = None
    for pt in points:
        if pt[0] <= epoch:
            best = pt
        else:
            break
    return best if best and epoch - best[0] <= max_gap else None


def _yahoo_minute_close(symbol, epoch):
    """Close of the 1-minute bar covering `epoch` (the 15:14 bar's close IS the value at 15:14:59)."""
    try:
        rows = df_to_rows(yf.download(symbol, period="1d", interval="1m", progress=False))
        bar_start = epoch - (epoch % 60)
        for r in reversed(rows):
            if r["time"] <= bar_start:
                return {"value": r["close"], "t": r["time"] + 59, "source": "Yahoo 1m bar close"} \
                    if bar_start - r["time"] <= 120 else None
    except Exception:
        return None
    return None


def cas_reference(alias, now_ist, points):
    """Locked reference for `alias`, or None while it is not yet knowable."""
    if now_ist.weekday() >= 5 or (now_ist.hour, now_ist.minute, now_ist.second) < (15, 15, 0):
        return None  # 15:14:59 has not passed yet
    key = (now_ist.strftime("%Y-%m-%d"), alias)
    if key in _cas_reference:
        return _cas_reference[key]
    epoch = cas_reference_epoch(now_ist)
    hit = _last_at_or_before([(t, v) for t, v, _i in points], epoch)
    ref = {"value": hit[1], "t": hit[0], "source": "last traded value at 15:14:59"} if hit else None
    if ref is None and alias in YAHOO_INDEX_SYMBOL:
        ref = _yahoo_minute_close(YAHOO_INDEX_SYMBOL[alias], epoch)
    if ref:
        _cas_reference[key] = ref
    return ref


@app.route("/api/cas/movement")
def cas_movement():
    ensure_poller()
    now_ist = datetime.now(IST)
    clock_phase, seconds, stage, _label = cas_phase(now_ist)
    nse_phase, nse_message, _ind = nse_session()
    phase = nse_phase or clock_phase
    ref_epoch = cas_reference_epoch(now_ist)
    window_from = ref_epoch - 600   # 10 minutes of pre-auction context

    out = []
    for alias in ("NIFTY", "BANKNIFTY", "NIFTYIT", "SENSEX"):
        if alias == "SENSEX":
            snap = bse_stream.snapshot()
            raw = bse_stream.series(20000)
            source, live = "BSE live stream", bse_stream.fresh()
            prev = snap and snap["prevClose"]
        else:
            raw = list(_nse_series[alias])
            source, live = "Yahoo tick + NSE indicative", bool(raw)
            prev = _nse_prev.get(alias)
        points = [[t, v, ind] for t, v, ind in raw]
        ref = cas_reference(alias, now_ist, points)
        recent = [pt for pt in points if pt[0] >= window_from][-3000:]
        last = points[-1] if points else None
        out.append({
            "alias": alias, "available": bool(points), "source": source, "live": live,
            "prevClose": prev, "last": last and last[1], "indicative": last and last[2],
            "reference": ref, "points": recent,
        })

    return jsonify({
        "phase": phase, "stage": stage, "secondsRemaining": seconds,
        "nseStatus": nse_message, "serverTime": int(time_module.time()),
        "referenceAt": ref_epoch, "referenceLocked": (now_ist.hour, now_ist.minute) >= (15, 15),
        "stream": {"connected": bse_stream.connected, "error": bse_stream.last_error},
        "indices": out,
    })


@app.route("/api/cas/feed")
def cas_feed():
    """The auction feed itself, stock by stock.

    During the closing auction this is NSE's CAS feed (~210 F&O names). Otherwise it is
    the pre-open feed, which keeps serving the last window's snapshot after it closes -
    so `source` and `live` tell the client exactly what it is looking at.
    `key=FO|ALL` only applies to the pre-open feed. With `symbol=`, one stock's order book.
    """
    key = (request.args.get("key") or "FO").upper()
    if key not in ("FO", "ALL"):
        abort(400, description="key must be FO or ALL")
    want_symbol = (request.args.get("symbol") or "").strip().upper()

    clock_phase, seconds, stage, _label = cas_phase(datetime.now(IST))
    nse_phase, _msg, _ind = nse_session()
    phase = nse_phase or clock_phase

    cas = _cas_payload() or {}
    cas_rows = normalise_cas_rows(cas)

    # ---- closing auction -------------------------------------------------------
    if cas_rows or phase == "closing-auction":
        if want_symbol:
            for r in cas_rows:
                if r["symbol"].upper() == want_symbol:
                    return jsonify({"available": True, "source": "cas", "symbol": r["symbol"],
                                    "iep": r.get("iep"), "ladder": normalise_cas_book(r["orderBook"]),
                                    "updated": cas.get("timestamp")})
            return jsonify({"available": False, "reason": "no order book for this symbol yet", "ladder": []})
        return jsonify({
            "available": True, "source": "cas", "live": bool(cas_rows) or phase == "closing-auction",
            "phase": phase, "stage": stage, "secondsRemaining": seconds,
            "asOf": cas.get("timestamp"), "status": cas.get("status"), "statusMsg": cas.get("statusMsg"),
            "eligible": len(cas.get("symbols") or []),
            "totals": {"quantity": cas.get("totalQuantity"), "value": cas.get("totalValue"),
                       "indicativeQuantity": cas.get("indicativeTotalQuantity"),
                       "indicativeValue": cas.get("indicativeTotalValue")},
            "note": None if cas_rows else "The auction is running, but NSE has not published order data yet (the first stage only sets the reference price).",
            "rows": cas_rows,
        })

    # ---- pre-open (live 09:00-09:15, otherwise the last snapshot) ---------------
    payload = _cached(("cas-feed", key), lambda: nse_get("/api/market-data-pre-open", {"key": key}))
    if not payload:
        return jsonify({"available": False, "reason": "NSE pre-open feed unreachable", "rows": []})

    if want_symbol:
        for row in payload.get("data", []):
            meta, pre = row.get("metadata", {}), row.get("detail", {}).get("preOpenMarket", {})
            if meta.get("symbol", "").upper() != want_symbol:
                continue
            ladder = [{"price": r.get("price"), "buyQty": r.get("buyQty"),
                       "sellQty": r.get("sellQty"), "flag": None, "isIep": bool(r.get("iep"))}
                      for r in pre.get("preopen", [])]
            return jsonify({"available": True, "source": "pre-open", "symbol": meta.get("symbol"),
                            "iep": pre.get("IEP"), "ladder": ladder,
                            "atoBuy": pre.get("atoBuyQty"), "atoSell": pre.get("atoSellQty"),
                            "updated": pre.get("lastUpdateTime")})
        return jsonify({"available": False, "reason": "symbol not in this auction feed", "ladder": []})

    rows = []
    for row in payload.get("data", []):
        meta, pre = row.get("metadata", {}), row.get("detail", {}).get("preOpenMarket", {})
        if not meta.get("symbol"):
            continue
        rows.append({
            "symbol": meta["symbol"], "iep": pre.get("IEP"), "prevClose": pre.get("prevClose"),
            "change": pre.get("Change"), "pChange": pre.get("perChange"),
            "finalQuantity": pre.get("finalQuantity"),
            "buyQty": pre.get("totalBuyQuantity"), "sellQty": pre.get("totalSellQuantity"),
            "atoBuy": pre.get("atoBuyQty"), "atoSell": pre.get("atoSellQty"),
        })

    return jsonify({
        "available": True, "source": "pre-open", "key": key, "phase": phase,
        "stage": stage, "secondsRemaining": seconds,
        "asOf": payload.get("timestamp"), "live": phase == "pre-open",
        "breadth": {
            "advances": payload.get("advances"), "declines": payload.get("declines"),
            "unchanged": payload.get("unchanged"), "tradedValue": payload.get("totalTradedValue"),
        },
        "rows": rows,
    })


def _bse_raw_sensex():
    """SENSEX open + indicative close ('-' means not set). The push stream carries both
    live; the REST feed is the fallback."""
    snap = bse_stream.snapshot() if bse_stream.fresh() else None
    if snap:
        return {"open": snap["open"], "indicativeClose": snap["indicativeClose"]}

    def fetch():
        try:
            r = _bse.get("https://api.bseindia.com/RealTimeBseIndiaAPI/api/GetSensexData/w", timeout=4)
            r.raise_for_status()
            row = r.json()[0]
            def num(v):
                try:
                    return float(str(v).replace(",", ""))
                except (TypeError, ValueError):
                    return None
            return {"open": num(row.get("I_open")), "indicativeClose": num(row.get("iclsprice"))}
        except (requests.RequestException, KeyError, IndexError, ValueError):
            return {}
    return _cached(("bse-raw", "sensex"), fetch) or {}


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify([])
    # Yahoo Finance's own public symbol-search endpoint (same one their site's search
    # box calls) — not a third-party site, no anti-bot bypass involved.
    resp = requests.get(
        "https://query1.finance.yahoo.com/v1/finance/search",
        params={"q": q, "quotesCount": 8, "newsCount": 0},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=5,
    )
    resp.raise_for_status()
    quotes = resp.json().get("quotes", [])
    results = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("shortname") or item.get("longname") or item.get("symbol"),
            "exchange": item.get("exchange"),
            "type": item.get("quoteType"),
        }
        for item in quotes
        if item.get("symbol")
    ]
    return jsonify(results)


def option_rows(df):
    cols = ["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]
    return [
        {c: (None if c not in row or row[c] != row[c] else float(row[c])) for c in cols}
        for row in df.to_dict("records")
    ]


@app.route("/api/options")
def options():
    symbol = clean_symbol(request.args.get("symbol"))
    t = yf.Ticker(symbol)

    try:
        expiries = list(t.options)
    except Exception as e:
        return jsonify({"available": False, "reason": str(e)[:200], "expiries": []})

    if not expiries:
        return jsonify({
            "available": False,
            "reason": "no option chain for this symbol",
            "expiries": [],
        })

    expiry = request.args.get("expiry") or expiries[0]
    if expiry not in expiries:
        abort(400, description="unknown expiry for this symbol")

    chain = t.option_chain(expiry)
    return jsonify({
        "available": True,
        "symbol": symbol,
        "expiry": expiry,
        "expiries": expiries,
        "calls": option_rows(chain.calls),
        "puts": option_rows(chain.puts),
    })


_load_seed()

if __name__ == "__main__":
    # threaded=True: the default single-threaded dev server processes one request at a
    # time, so rapid UI interactions (range/symbol clicks) queue up server-side behind
    # slow yfinance round-trips — your latest click waits behind stale ones you no
    # longer care about. Threading lets them actually run concurrently.
    app.run(debug=True, port=5000, threaded=True)
