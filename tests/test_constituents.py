from orazio.constituents import summarize, top_movers


def _row(prev, last, open_=None, high=None, low=None, volume=None):
    return {
        "prevClose": prev, "last": last,
        "open": open_ if open_ is not None else prev,
        "high": high if high is not None else max(prev, last),
        "low": low if low is not None else min(prev, last),
        "volume": volume if volume is not None else 1000,
    }


def test_summarize_counts_advances_declines_and_unchanged():
    data = {
        "A": _row(100.0, 105.0),   # up
        "B": _row(100.0, 95.0),    # down
        "C": _row(100.0, 100.0),   # flat
        "D": _row(50.0, 51.0),     # up
    }
    assert summarize(data) == {"advances": 2, "declines": 1, "unchanged": 1}


def test_summarize_skips_missing_rows_instead_of_counting_them():
    data = {"A": _row(100.0, 105.0), "B": None}
    assert summarize(data) == {"advances": 1, "declines": 0, "unchanged": 0}


def test_summarize_empty_input():
    assert summarize({}) == {"advances": 0, "declines": 0, "unchanged": 0}


def test_top_movers_ranks_by_percent_change_and_strips_ticker_suffix():
    data = {
        "AAA.NS": _row(100.0, 110.0),   # +10%
        "BBB.NS": _row(100.0, 90.0),    # -10%
        "CCC.NS": _row(200.0, 202.0),   # +1%
        "DDD.NS": _row(50.0, 40.0),     # -20%
    }
    result = top_movers(data, n=2)
    assert [r["symbol"] for r in result["gainers"]] == ["AAA", "CCC"]
    assert [r["symbol"] for r in result["losers"]] == ["DDD", "BBB"]
    assert result["gainers"][0]["perChange"] == 10.0


def test_top_movers_skips_missing_or_zero_prev_close():
    data = {"A": _row(100.0, 110.0), "B": None, "C": _row(0, 5)}
    result = top_movers(data, n=5)
    symbols = {r["symbol"] for r in result["gainers"]} | {r["symbol"] for r in result["losers"]}
    assert symbols == {"A"}
