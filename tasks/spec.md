# Spec: Live chart parity + TradingView-style UX

## Objective
The chart currently lags the live index, has no way to scroll into prior-day intraday
history, a volume pane that overlaps candles, no quick-access index list, and no
futures/options view. This spec closes that gap toward TradingView-like behavior on a
single symbol page. User: the person actively trading/watching NIFTY & friends intraday.
Success = chart visibly tracks the index in real time, prior-day data is one drag away,
volume reads as a thin strip, common indices are one click away, and cash/futures +
option premium are reachable where the data actually supports it.

## Verified facts (checked live against yfinance before writing this)
- Index aliases resolve: `^CNXIT` (NIFTYIT), `^GSPC` (SPX), `^IXIC` (NASDAQ), `^DJI`
  (DOWJONES), `^KS11` (KOSPI), `^TWII` (TAIEX) — all returned a live price.
- `^NSEI` (NIFTY) has **no option chain** via yfinance (`t.options` returns empty).
  US names (e.g. AAPL) do have one.
- No NSE index/futures ticker resolves via yfinance (`NIFTY_FUT`, `NIFTY.NS`, `^NSEI=F`
  all 404). Global index futures DO resolve (`NQ=F` Nasdaq, `ES=F` S&P).

## Assumptions (proceeding unless corrected)
1. "Fix the 1-minute lag" means: for 1m/5m intervals, the chart should update within
   ~5s of the live price (matching the existing quote-poll cadence), not that yfinance's
   own upstream delay (which can itself be ~1 min for Indian indices) needs eliminating —
   that upstream delay isn't in this app's control.
2. Drag-to-previous-day applies to intraday intervals (1m/5m/15m/1h) only; daily/weekly
   view already spans years and doesn't need this.
3. Session-boundary marker = a vertical line + small date label at the prior-close →
   next-open gap, drawn once extra history is loaded (not baked into every candle fetch).
4. Cash/Futures toggle: **decided — dropped for NIFTY/BANKNIFTY/NIFTYIT.** These three
   always show cash/spot only, no toggle shown at all (not even disabled). The toggle
   still applies to global indices where yfinance has a real futures ticker (SPX→ES=F,
   NASDAQ→NQ=F, DOWJONES→YM=F).
5. Option premium viewer: **scoped by data availability.** NIFTY/BANKNIFTY/NIFTYIT show
   a disabled "options data unavailable" state (yfinance has no chain for them);
   US-listed symbols with a real chain (via `Ticker.options` / `Ticker.option_chain(date)`)
   get a working panel. This is a real data-source gap, not a bug to code around.
6. Left rail is a fixed, static list (the 8 named indices) — not user-editable/searchable
   in this pass.
7. "Swipe/drag left" is implemented as native chart panning (Lightweight-Charts already
   supports drag-to-pan) plus a scroll-triggered lazy-load of older bars, not a custom
   gesture layer.
→ Correct me now on 4/5 in particular (the futures/options scope) or I'll build to this.

## Tech Stack
Flask (Python) + yfinance backend, vanilla JS + TradingView Lightweight-Charts frontend.
No new frontend framework. New backend deps only if unavoidable (none currently expected
— option chain and futures both go through `yfinance.Ticker`).

## Commands
Run: `python app.py` (serves on :5000)
No automated test runner in this project; verification is curl against the running
Flask server plus manual chart checks in-browser.

## Project Structure
```
app.py              → Flask routes: /api/candles, /api/quote, /api/search (+ new routes below)
static/index.html   → chart UI (JS inline)
tasks/               → spec/plan/todo for this change
```
New routes needed:
```
/api/candles/history  → older intraday page for drag-to-load-more (before a given timestamp)
/api/futures-symbol    → resolve cash symbol -> futures ticker, or "unavailable"
/api/options           → option chain (expiries + strikes/premiums) for a symbol, or "unavailable"
```

## Code Style
Existing style: small pure functions, no framework, comments only where behavior is
non-obvious (e.g. why a row is filtered, why a symbol is aliased). New endpoints follow
the same pattern as `/api/candles` (validate → fetch via yfinance → shape JSON → return
`{available: false, reason: "..."}` instead of erroring when data genuinely doesn't exist).

## Testing Strategy
No automated suite. Verify each new endpoint with curl against real symbols (one that
should work, one that should report unavailable), then a manual browser pass: drag chart
left across a session boundary, toggle futures on a supported vs unsupported symbol, open
the options panel on AAPL vs NIFTY.

## Boundaries
- Always: keep interval+range validation server-side (existing pattern in
  `resolve_interval_range`); never let the frontend request combos yfinance can't serve.
- Ask first: adding new Python dependencies beyond yfinance/requests/flask/flask-cors;
  changing the default landing symbol/interval away from NIFTY/1m/1D.
- Never: fabricate futures or options data for symbols yfinance doesn't cover — always
  show explicit "unavailable" state instead of a silent zero/empty chart.

## Success Criteria
1. On a 1m-interval symbol, the last visible candle's close updates within ~5s of the
   quote ticker, not up to 60s behind it.
2. Dragging the chart left past the start of today's session loads and displays prior
   sessions' intraday candles continuously (up to ~5-7 days back, yfinance's 1m limit),
   with a visible marker at each session boundary.
3. Volume bars occupy no more than ~12% of the pane height and don't visually overlap
   candle wicks/bodies.
4. Left rail lists NIFTY, BANKNIFTY, NIFTYIT, SPX, NASDAQ, DOWJONES, KOSPI, TAIEX; each
   is one click to load.
5. Cash/Futures toggle: works (loads a different, correctly-labeled instrument) for
   SPX/NASDAQ/DOWJONES; not shown at all for NIFTY/BANKNIFTY/NIFTYIT (cash/spot only).
6. Option premium panel opens and shows live strikes/premiums for symbols with a real
   yfinance option chain; shows "unavailable" for symbols without one (e.g. NIFTY).

## Open Questions
None blocking — resolved:
1. Futures/options scope confirmed: NIFTY/BANKNIFTY/NIFTYIT show cash/spot only (no
   futures toggle); options panel still attempted but will report unavailable for them.
2. Drag-back depth confirmed: up to yfinance's ~5-7 day 1m intraday limit.

## Increment 2: latency, glitch fixes, granularity zoom, more indices

Filed after real usage surfaced new issues. **No chrome-devtools MCP is configured in
this environment**, so these were root-caused by direct code reading + backend
verification (curl/python), not a live browser session — flagged explicitly per item.

### Verified facts (this increment)
- NSE's own public index API (`https://www.nseindia.com/api/allIndices`, same one
  nseindia.com's site uses) returns live NIFTY 50 / NIFTY BANK / NIFTY IT values with a
  session-cookie handshake — confirmed working live, materially fresher than Yahoo's
  index feed for these three.
- `resolve_interval_range` escalates "1Y" range (with the default 1m interval dropdown)
  to **1h**, not 1d — confirmed via curl, backend returns valid data (1705 real hourly
  rows spanning the full year).
- Root cause of the rubber-band/hidden-9:15 glitch **and** the ">1Y shows nothing" bug:
  the same bug. The `subscribeVisibleLogicalRangeChange` listener added in Increment 1
  fires on *any* visible-range change, including the ones `fitContent()` itself
  produces right after a fresh `setData()` — not just genuine user drags. For 1D/1m
  it just causes an unwanted premature prefetch (the rubber-band). For 1Y/YTD (which
  resolve to 1h, not 1d, so weren't excluded by the existing "skip on 1d" guard) it
  fetches an overlapping/duplicate window from `/api/candles/history` and hands
  Lightweight-Charts non-monotonic or duplicate timestamps — which the library
  rejects/throws on, breaking that chart instance until reload. That break persists
  across subsequent range clicks in the same page load, which is why 5Y/All *also*
  looked broken afterward even though their own data and code path are fine in
  isolation (verified both return correct real rows independently via curl).
- ^HSI (Hang Seng) and 000001.SS (Shanghai Composite) both resolve live via yfinance.
  SENSEX (^BSESN) was already aliased in Increment 1 but never added to the rail list.

### Assumptions (proceeding unless corrected)
1. "China's index" = Shanghai Composite (`000001.SS`, mainland China's primary index),
   not Hang Seng (Hong Kong). Aliased as `CHINA`. Correct me if Hang Seng was intended
   instead/also.
2. The NSE low-latency source is used only for the 3 NSE indices it actually covers
   (NIFTY, BANKNIFTY, NIFTYIT) — everything else keeps using yfinance. It's used for the
   `/api/quote` (ticker/price) endpoint only, not for historical candles (NSE's API
   doesn't serve OHLC history in the shape we need).
3. NSE's endpoint is unofficial (their own site's API, no public docs/SLA) and can
   start blocking/rate-limiting or change shape without notice — `/api/quote` must fall
   back to the existing yfinance path silently on any failure, never surface an error
   to the user for this.
4. "Zoom out/in changes granularity like TradingView" means: as the user zooms the
   mouse wheel and the visible time span crosses a threshold, the interval
   auto-switches (1m→5m→15m→1h→1d and back) while preserving the same visible time
   window (not re-fitting/resetting the view). Thresholds are a reasonable approximation
   of TradingView's own, not pixel-matched to it.
5. "Put the day marker on top or some other intuitive way" — reframed as: drop the
   on-chart arrow+label markers entirely, and instead show the full date (not just
   time-of-day) on the time axis label for ticks that land on a session boundary. This
   keeps the chart body completely uncluttered, at the cost of the boundary date only
   appearing when the axis happens to render a tick at that exact bar (not literally on
   every boundary at every zoom level).
→ Correct me now on 1/4/5 or I'll build to these.

## Boundaries (increment 2, additive)
- Ask first: nothing new here beyond what's already listed above.
- Never: let a failure in the NSE scrape path surface as a user-visible error or block
  `/api/quote` — always fall back to yfinance.

## Success Criteria (increment 2)
1. NIFTY/BANKNIFTY/NIFTYIT quote polling uses NSE's live feed when reachable (visibly
   fresher last-price updates), falls back to yfinance transparently when not.
2. Switching symbols or letting the 60s auto-refresh fire no longer causes any visible
   jump/rubber-band, and the session-open (9:15) candle stays in view.
3. Clicking 1Y, 5Y, YTD, and All in sequence all show correct real historical data,
   with no chart breakage carrying over between them.
4. Scrolling the mouse wheel out/in over the chart changes the bar interval at sensible
   zoom thresholds, keeping the same visible time window in view (no jump to fit-all).
5. Day-boundary indication no longer overlaps candle bodies.
6. Left rail includes SENSEX and CHINA (000001.SS) alongside the existing 8.

## Open Questions (increment 2)
None blocking — proceeding on the assumptions above.
