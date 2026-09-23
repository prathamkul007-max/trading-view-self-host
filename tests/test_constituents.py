from orazio.constituents import summarize


def test_counts_advances_declines_and_unchanged():
    closes = {
        "A": (100.0, 105.0),   # up
        "B": (100.0, 95.0),    # down
        "C": (100.0, 100.0),   # flat
        "D": (50.0, 51.0),     # up
    }
    assert summarize(closes) == {"advances": 2, "declines": 1, "unchanged": 1}


def test_skips_tickers_with_missing_data_instead_of_counting_them():
    closes = {"A": (100.0, 105.0), "B": (None, None), "C": (100.0, None)}
    assert summarize(closes) == {"advances": 1, "declines": 0, "unchanged": 0}


def test_empty_input():
    assert summarize({}) == {"advances": 0, "declines": 0, "unchanged": 0}
