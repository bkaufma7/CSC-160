"""Offline tests: run with `python -m pytest -q` (or `python test_collector.py`).
They use hand-written API responses shaped like the documented ones, so they
check our parsing and math, not the live services."""
import json
from datetime import datetime, timedelta, timezone

import collector as c

SOON = (datetime.now(timezone.utc) + timedelta(hours=5))
DATE = c.et_date(SOON.isoformat())
YYMONDD = f"{DATE[2:4]}{list(c.MONTHS)[int(DATE[5:7]) - 1]}{DATE[8:]}"


def test_team_names():
    assert c.team_code("Boston Celtics") == "BOS"
    assert c.team_code("Celtics") == "BOS"
    assert c.team_code("New York") == "NYK"
    assert c.team_code("NYK") == "NYK"
    assert c.team_code("Portland Trail Blazers") == "POR"
    assert c.team_code("Los Angeles") is None  # ambiguous on purpose


def test_kalshi_book_buy_price_comes_from_no_bids():
    ob = {"orderbook_fp": {"yes_dollars": [["0.4000", "50.00"], ["0.4500", "80.00"]],
                           "no_dollars": [["0.5000", "30.00"], ["0.5200", "120.00"]]}}
    asks, bids = c.kalshi_levels(ob)
    assert asks[0] == (0.48, 120.0)  # best YES ask = 1 - best NO bid
    assert bids[0] == (0.45, 80.0)


def test_american_odds():
    assert abs(1 / c.american_to_decimal(110) - 0.4762) < 1e-4
    assert abs(1 / c.american_to_decimal(-150) - 0.6) < 1e-9


def test_end_to_end(tmp_path, monkeypatch):
    ev = f"KXNBAGAME-{YYMONDD}BOSNYK"
    kmarkets = [{"ticker": f"{ev}-BOS", "event_ticker": ev, "yes_sub_title": "Boston", "title": "t"},
                {"ticker": f"{ev}-NYK", "event_ticker": ev, "yes_sub_title": "New York", "title": "t"}]
    books = {f"{ev}-BOS": {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.52", "100"]]}},
             f"{ev}-NYK": {"orderbook_fp": {"yes_dollars": [["0.50", "10"]], "no_dollars": [["0.49", "100"]]}}}
    pm_events = [{"slug": f"nba-bos-nyk-{DATE}", "startTime": SOON.isoformat(), "markets": [{
        "question": "Celtics vs. Knicks", "sportsMarketType": "moneyline",
        "outcomes": json.dumps(["Celtics", "Knicks"]), "clobTokenIds": json.dumps(["t1", "t2"]),
        "gameStartTime": SOON.isoformat(), "conditionId": "0xabc"}]}]
    pm_books = {"t1": {"asks": [{"price": "0.49", "size": "40"}, {"price": "0.47", "size": "25"}],
                       "bids": [{"price": "0.45", "size": "9"}]},
                "t2": {"asks": [{"price": "0.55", "size": "5"}], "bids": []}}

    def fake_get(url, **params):
        if url.endswith("/markets"):
            return {"markets": kmarkets, "cursor": ""}
        if "/orderbook" in url:
            return books[url.split("/markets/")[1].split("/")[0]]
        if url.endswith("/events"):
            return pm_events
        if url.endswith("/book"):
            return pm_books[params["token_id"]]
        raise AssertionError(url)

    monkeypatch.setattr(c, "get", fake_get)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)

    class R:
        headers = {"x-requests-remaining": "400"}
        def raise_for_status(self): pass
        def json(self):
            return [{"id": "g1", "commence_time": SOON.isoformat(), "home_team": "New York Knicks",
                     "away_team": "Boston Celtics", "bookmakers": [{"key": "fanduel", "last_update": "x",
                     "markets": [{"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": -110},
                                                              {"name": "New York Knicks", "price": 110}]}]}]}]
    monkeypatch.setattr(c.HTTP, "get", lambda *a, **k: R())

    db = c.connect(str(tmp_path / "q.db"))
    c.save(db, c.collect_kalshi("KXNBAGAME"))
    c.save(db, c.collect_polymarket("10345", "100639"))
    c.save(db, c.collect_odds("k", "fanduel"))
    rows = db.execute("SELECT source, team_code, buy_price, buy_size, game_key FROM quotes").fetchall()
    assert len(rows) == 6 and {r["game_key"] for r in rows} == {f"{DATE}:BOS-NYK"}
    pm_bos = [r for r in rows if r["source"] == "polymarket" and r["team_code"] == "BOS"][0]
    assert (pm_bos["buy_price"], pm_bos["buy_size"]) == (0.47, 25.0)  # cheapest ask, unsorted input

    class A: db = str(tmp_path / "q.db"); game = None
    c.show(A)


if __name__ == "__main__":
    import pathlib, tempfile, pytest
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
