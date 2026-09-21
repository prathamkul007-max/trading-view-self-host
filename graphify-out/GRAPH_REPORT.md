# Graph Report - sasta-trading-view  (2026-09-21)

## Corpus Check
- Corpus is ~8,313 words - fits in a single context window. You may not need a graph.

## Summary
- 80 nodes · 123 edges · 11 communities (8 shown, 3 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 12 edges (avg confidence: 0.87)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Flask Backend API
- Candle Loading & Chart State
- Config, Futures & Options Panel
- Live Quote & Chart Setup
- Interval/Range Resolution
- Day Boundary Overlay
- Symbol Rail & Search
- Spec, Plan & Todo Docs
- Chrome DevTools MCP
- Flask Dependencies
- Wheel-Zoom Granularity (Removed)

## God Nodes (most connected - your core abstractions)
1. `loadCandles()` - 15 edges
2. `loadMoreHistory()` - 10 edges
3. `candles_history()` - 7 edges
4. `latestCandles (state)` - 7 edges
5. `candles()` - 6 edges
6. `quote()` - 6 edges
7. `options()` - 6 edges
8. `pollQuote()` - 6 edges
9. `computeDayBoundaries()` - 6 edges
10. `Stale request race (bug) and mitigations` - 6 edges

## Surprising Connections (you probably didn't know these)
- `Range click reused wider range's interval (bug)` --references--> `resolve_interval_range()`  [INFERRED]
  tasks/todo.md → app.py
- `NSE low-latency quote source` --references--> `nse_live_quote()`  [INFERRED]
  tasks/spec.md → app.py
- `Server-side interval/range validation boundary` --references--> `resolve_interval_range()`  [EXTRACTED]
  tasks/spec.md → app.py
- `loadConfig()` --references--> `config()`  [EXTRACTED]
  static/index.html → app.py
- `Left quick-access index rail` --references--> `config()`  [EXTRACTED]
  tasks/spec.md → app.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Stale/programmatic-event guard mechanisms in the chart frontend** — static_index_loadgeneration, static_index_candlesabortcontroller, static_index_suppressrangeevents, static_index_loadcandles, static_index_loadmorehistory [INFERRED 0.85]
- **Drag-to-load history flow** — static_index_visible_range_listener, static_index_loadmorehistory, app_candles_history, static_index_latestcandles, static_index_computedayboundaries [INFERRED 0.85]
- **Symbol switch flow (rail, search, futures toggle)** — static_index_rail, static_index_symbol_search_handler, static_index_updatefuturestoggle, static_index_switchsymbol, static_index_loadcandles, static_index_pollquote [INFERRED 0.85]

## Communities (11 total, 3 thin omitted)

### Community 0 - "Flask Backend API"
Cohesion: 0.21
Nodes (18): candles(), candles_history(), clean_symbol(), config(), df_to_rows(), drop_spurious_flat_rows(), index(), nse_live_quote() (+10 more)

### Community 1 - "Candle Loading & Chart State"
Cohesion: 0.20
Nodes (15): candlesAbortController, Crosshair legend handler, latestCandles (state), loadCandles(), loadGeneration (counter), loadMoreHistory(), renderIndicators(), suppressRangeEvents (flag) (+7 more)

### Community 2 - "Config, Futures & Options Panel"
Cohesion: 0.20
Nodes (11): yfinance dependency, CONFIG (quickIndices, futuresMap), futuresCounterpart(), highlightRail(), loadConfig(), openOptions(), #options-modal, updateFuturesToggle() (+3 more)

### Community 3 - "Live Quote & Chart Setup"
Cohesion: 0.29
Nodes (7): Lightweight-Charts chart instance + series, pollQuote(), Candle freshness: patch last bar from 5s quote tick, All range truncated by minBarSpacing (bug), Playwright / chrome-devtools QA testing, pollQuote stale-symbol response (bug), Volume pane shrink (scaleMargins)

### Community 4 - "Interval/Range Resolution"
Cohesion: 0.40
Nodes (5): Pick the coarsest interval that can actually serve the requested range.…, resolve_interval_range(), #range-bar buttons, Server-side interval/range validation boundary, Range click reused wider range's interval (bug)

### Community 5 - "Day Boundary Overlay"
Cohesion: 0.50
Nodes (4): computeDayBoundaries(), #day-lines DOM overlay, renderDayLines(), Day/session boundary indication

### Community 6 - "Symbol Rail & Search"
Cohesion: 0.40
Nodes (5): #rail index sidebar, switchSymbol(), Symbol search input handler (debounced), Left quick-access index rail, Search dropdown reopening after Enter (bug)

### Community 7 - "Spec, Plan & Todo Docs"
Cohesion: 0.50
Nodes (4): Plan: Live chart parity + TradingView-style UX, Build order (independent vs sequential), Spec: Live chart parity + TradingView-style UX, Todo: task checklist (Increments 1-5)

## Knowledge Gaps
- **14 isolated node(s):** `npx`, `Crosshair legend handler`, `#options-modal`, `flask dependency`, `flask-cors dependency` (+9 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `loadCandles()` connect `Candle Loading & Chart State` to `Flask Backend API`, `Config, Futures & Options Panel`, `Live Quote & Chart Setup`, `Interval/Range Resolution`, `Day Boundary Overlay`, `Symbol Rail & Search`?**
  _High betweenness centrality (0.323) - this node is a cross-community bridge._
- **Why does `candles()` connect `Flask Backend API` to `Candle Loading & Chart State`, `Interval/Range Resolution`?**
  _High betweenness centrality (0.117) - this node is a cross-community bridge._
- **Why does `loadMoreHistory()` connect `Candle Loading & Chart State` to `Flask Backend API`, `Day Boundary Overlay`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **What connects `npx`, `Crosshair legend handler`, `#options-modal` to the rest of the system?**
  _14 weakly-connected nodes found - possible documentation gaps or missing edges._