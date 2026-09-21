# Plan: Live chart parity + TradingView-style UX

Source spec: `tasks/spec.md`

## Components

1. **Candle freshness fix** (backend: none; frontend: `static/index.html`)
   Patch the in-progress last candle from each 5s quote tick (update close/high/low,
   call `candleSeries.update()`), instead of waiting for the 60s full candle refetch.
   Keep the 60s refetch as the authoritative resync (fixes any drift/volume lag).

2. **Symbol map + left index rail** (backend: `app.py` `SYMBOL_ALIASES`; frontend: new
   sidebar in `static/index.html`)
   Add NIFTYIT/SPX/NASDAQ/DOWJONES/KOSPI/TAIEX aliases (tickers verified in spec). Static
   HTML list, one click = set `currentSymbol` + reload.

3. **Volume pane resize** (frontend only)
   `scaleMargins` on the histogram series: `top: 0.88, bottom: 0`ish so volume is a thin
   strip, not overlapping candles.

4. **Prior-day drag-to-load history** (backend: new `/api/candles/history`; frontend:
   scroll-triggered lazy load)
   - Backend: given `symbol`, `interval`, `before` (unix ts), fetch up to 7d of history
     at that interval (yfinance's real 1m ceiling) and return bars strictly before
     `before`, oldest-first, capped at one "page" (e.g. one extra trading day per call).
   - Frontend: `chart.timeScale().subscribeVisibleLogicalRangeChange()` — when the
     visible range nears the oldest loaded bar, call the endpoint, prepend results to
     the in-memory dataset, `setData()` the merged array, then restore the visible
     logical range so the view doesn't jump.
   - Depends on: nothing structurally, but must land before (5) since markers are drawn
     against the merged multi-day dataset.

5. **Session-boundary markers** (frontend only, depends on 4)
   Walk the merged candle array, detect each point where the calendar day changes,
   call `candleSeries.setMarkers()` with a small vertical marker + date label there.

6. **Cash/Futures toggle for global indices** (backend: `app.py` futures symbol map;
   frontend: toggle shown only for SPX/NASDAQ/DOWJONES)
   Map: SPX→ES=F, NASDAQ→NQ=F, DOWJONES→YM=F. Toggle switches `currentSymbol` between
   the cash and futures ticker and reloads candles/quote. Not shown for
   NIFTY/BANKNIFTY/NIFTYIT (spec decision: cash/spot only, no toggle).

7. **Option premium panel** (backend: new `/api/options`; frontend: modal/panel)
   - Backend: `Ticker(symbol).options` for expiries; `Ticker(symbol).option_chain(date)`
     for calls/puts. Return `{available: false, reason: "no option chain for this symbol"}`
     when `.options` is empty (this will be the actual response for NIFTY/BANKNIFTY/
     NIFTYIT — verified live during spec).
   - Frontend: a button on the symbol page opens the panel; shows expiry picker + a
     calls/puts premium table when available, an "unavailable" message otherwise.

## Build order

Independent, can be done in any order / parallel (no shared state, low risk):
- (2) symbol map + left rail
- (3) volume pane resize

Sequential (each depends on the previous landing and working):
1. (1) candle freshness fix — verify chart visibly keeps pace before touching anything else
2. (4) drag-to-load history — new endpoint + frontend lazy-load
3. (5) session-boundary markers — needs (4)'s merged dataset to draw against
4. (6) futures toggle — independent of 4/5, but sequenced after so the symbol-switch
   code path (already touched by (2)) is stable
5. (7) options panel — fully independent backend endpoint; do last since it's the most
   isolated, most likely to hit the "unavailable" data-gap path

## Risks & mitigations

- **yfinance rate limiting** from more frequent calls (per-tick patching avoids extra
  calls — it reuses the existing 5s quote poll, no new backend load). Only the history
  endpoint (4) and options endpoint (7) add new backend calls, and both are
  user-triggered (scroll / panel-open), not polled.
- **Lightweight-Charts jump-on-prepend**: prepending older bars via `setData()` resets
  the visible range unless explicitly restored — plan (4) accounts for this
  (`setVisibleLogicalRange` after merge).
- **Options/futures data gaps are real, not bugs** — both endpoints must return a
  structured `available: false` response, never throw a 500, so the frontend can render
  the "unavailable" state cleanly.

## Verification checkpoints

- After (1): watch the chart next to a live index quote for 2 minutes; last bar should
  never lag the printed price by more than ~5s.
- After (4)+(5): drag left from today's session on NIFTY 1m; should smoothly reveal
  prior day(s) up to ~5-7 days back with a marker at each boundary, no gaps/dupes.
- After (6): toggle on SPX loads ES=F and back; toggle absent entirely on NIFTY page.
- After (7): open options panel on AAPL (real data) and NIFTY (unavailable state) —
  both must render without console errors.
