import re
import time as time_module
import requests
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
import yfinance as yf

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
    symbol = clean_symbol(request.args.get("symbol"))
    requested_interval = request.args.get("interval", "1m")
    requested_range = request.args.get("range", "1D")
    interval, rng = resolve_interval_range(requested_interval, requested_range)
    period = RANGE_TO_PERIOD[rng]

    df = yf.download(symbol, period=period, interval=interval, progress=False)
    rows = df_to_rows(df)

    return jsonify({
        "symbol": symbol,
        "interval": interval,
        "range": rng,
        "candles": rows,
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


# NSE publishes its own index feed (the same one nseindia.com's site calls) —
# materially fresher than Yahoo's for these three, since Yahoo's index data (as
# opposed to individual equities) tends to run behind the live exchange tape.
NSE_INDEX_LABELS = {
    "^NSEI": "NIFTY 50",
    "^NSEBANK": "NIFTY BANK",
    "^CNXIT": "NIFTY IT",
}

_nse_session = None
_nse_session_at = 0


def _nse_session_get():
    global _nse_session, _nse_session_at
    now = time_module.time()
    if _nse_session is None or now - _nse_session_at > 240:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            s.get("https://www.nseindia.com", timeout=5)
            _nse_session, _nse_session_at = s, now
        except requests.RequestException:
            _nse_session = None
    return _nse_session


def nse_live_quote(symbol):
    """Best-effort — returns None on any failure so the caller falls back to yfinance.
    Never raises: this is a latency optimization, not a required data path."""
    global _nse_session
    label = NSE_INDEX_LABELS.get(symbol)
    if not label:
        return None
    session = _nse_session_get()
    if session is None:
        return None
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=5)
        if resp.status_code != 200:
            _nse_session = None  # cookie likely stale — refresh on next call
            return None
        for row in resp.json().get("data", []):
            if row.get("index") == label:
                return {"price": float(row["last"]), "prevClose": float(row["previousClose"])}
    except (requests.RequestException, ValueError, KeyError):
        _nse_session = None
    return None


@app.route("/api/quote")
def quote():
    symbol = clean_symbol(request.args.get("symbol"))

    live = nse_live_quote(symbol)
    if live:
        price, prev_close, currency, source = live["price"], live["prevClose"], "INR", "nse"
    else:
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = float(info["lastPrice"])
        prev_close = info.get("previousClose") if hasattr(info, "get") else info["previousClose"]
        if prev_close is None:
            hist = t.history(period="5d", interval="1d")
            closes = hist["Close"].dropna()
            prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else price
        else:
            prev_close = float(prev_close)
        currency, source = info.get("currency", ""), "yfinance"

    change = price - prev_close
    change_pct = (change / prev_close) * 100 if prev_close else 0
    return jsonify({
        "symbol": symbol,
        "price": price,
        "time": int(time_module.time()),
        "change": change,
        "changePercent": change_pct,
        "currency": currency,
        "source": source,
    })


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


if __name__ == "__main__":
    # threaded=True: the default single-threaded dev server processes one request at a
    # time, so rapid UI interactions (range/symbol clicks) queue up server-side behind
    # slow yfinance round-trips — your latest click waits behind stale ones you no
    # longer care about. Threading lets them actually run concurrently.
    app.run(debug=True, port=5000, threaded=True)
