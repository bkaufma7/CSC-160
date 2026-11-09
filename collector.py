#!/usr/bin/env python3
"""Week 1 collector: timestamped pregame NBA moneyline quotes from
Kalshi, Polymarket, and (via The Odds API) DraftKings / FanDuel.

    python collector.py run            # collect forever (Ctrl+C to stop)
    python collector.py run --once     # one snapshot, then exit
    python collector.py show           # four-row table for one game

Every row means: "at time `ts`, buying <team> to win on <source> cost
`buy_price` (dollars per $1 payout) for up to `buy_size` contracts."
Sportsbook prices include the bookmaker's margin and have no size.
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ODDS = "https://api.the-odds-api.com/v4"
log = logging.getLogger("collector")
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "nba-arb-class-project/0.1"


def get(url, **params):
    r = HTTP.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Team normalisation: every platform names teams differently, so map them all
# to one 3-letter code. Anything that fails to map is saved with game_key=NULL
# so you can see (and count) the coverage gaps.
# --------------------------------------------------------------------------
TEAMS = {  # code: (nickname, city, extra aliases)
    "ATL": ("hawks", "atlanta", []), "BOS": ("celtics", "boston", []),
    "BKN": ("nets", "brooklyn", ["BRK"]), "CHA": ("hornets", "charlotte", ["CHO"]),
    "CHI": ("bulls", "chicago", []), "CLE": ("cavaliers", "cleveland", ["cavs"]),
    "DAL": ("mavericks", "dallas", ["mavs"]), "DEN": ("nuggets", "denver", []),
    "DET": ("pistons", "detroit", []), "GSW": ("warriors", "golden state", ["GS"]),
    "HOU": ("rockets", "houston", []), "IND": ("pacers", "indiana", []),
    "LAC": ("clippers", None, []), "LAL": ("lakers", None, []),
    "MEM": ("grizzlies", "memphis", []), "MIA": ("heat", "miami", []),
    "MIL": ("bucks", "milwaukee", []), "MIN": ("timberwolves", "minnesota", ["wolves"]),
    "NOP": ("pelicans", "new orleans", ["NO"]), "NYK": ("knicks", "new york", ["NY"]),
    "OKC": ("thunder", "oklahoma city", []), "ORL": ("magic", "orlando", []),
    "PHI": ("76ers", "philadelphia", ["sixers"]), "PHX": ("suns", "phoenix", ["PHO"]),
    "POR": ("trail blazers", "portland", ["blazers"]), "SAC": ("kings", "sacramento", []),
    "SAS": ("spurs", "san antonio", ["SA"]), "TOR": ("raptors", "toronto", []),
    "UTA": ("jazz", "utah", ["UTH"]), "WAS": ("wizards", "washington", []),
}


def team_code(text):
    """'Boston Celtics' / 'Celtics' / 'BOS' -> 'BOS'. None if unknown or ambiguous."""
    if not text:
        return None
    s = text.strip().lower()
    for code, (nick, city, extra) in TEAMS.items():
        names = [nick] + [e.lower() for e in extra if len(e) > 3]
        if any(re.search(rf"\b{re.escape(n)}\b", s) for n in names):
            return code
    for code, (nick, city, extra) in TEAMS.items():
        if city and re.search(rf"\b{re.escape(city)}\b", s):
            return code
    token = text.strip().upper()
    for code, (_, _, extra) in TEAMS.items():
        if token == code or token in [e.upper() for e in extra]:
            return code
    return None  # e.g. a bare "Los Angeles" could be either LA team


def make_key(date, a, b):
    if not (date and a and b) or a == b:
        return None
    return f"{date}:{'-'.join(sorted([a, b]))}"


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_dt(s):
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def et_date(s):
    return parse_dt(s).astimezone(ET).date().isoformat()


def today_et():
    return datetime.now(ET).date().isoformat()


MONTHS = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def kalshi_date(event_ticker):
    """KXNBAGAME-26NOV03BOSNYK -> 2026-11-03"""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    return f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"


def slug_date(slug):
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", slug or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Row construction + storage
# --------------------------------------------------------------------------
def row(source, key, date, start, team, code, market_id, asks, bids,
        pregame, quote_time, raw):
    """asks/bids: lists of (price, size), best first."""
    return dict(
        ts=now_iso(), source=source, game_key=key, game_date=date,
        game_start=start, team=team, team_code=code, market_id=market_id,
        buy_price=asks[0][0] if asks else None,
        buy_size=asks[0][1] if asks else None,
        best_bid=bids[0][0] if bids else None,
        depth=json.dumps({"asks": asks[:5], "bids": bids[:5]}),
        pregame=None if pregame is None else int(pregame),
        quote_time=quote_time, raw=json.dumps(raw))


SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
  id INTEGER PRIMARY KEY, ts TEXT, source TEXT, game_key TEXT, game_date TEXT,
  game_start TEXT, team TEXT, team_code TEXT, market_id TEXT, buy_price REAL,
  buy_size REAL, best_bid REAL, depth TEXT, pregame INTEGER, quote_time TEXT,
  raw TEXT);
CREATE INDEX IF NOT EXISTS ix_quotes ON quotes(game_key, source, ts);
"""
COLS = ["ts", "source", "game_key", "game_date", "game_start", "team",
        "team_code", "market_id", "buy_price", "buy_size", "best_bid", "depth",
        "pregame", "quote_time", "raw"]


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def save(db, rows):
    with db:
        db.executemany(
            f"INSERT INTO quotes({','.join(COLS)}) VALUES({','.join(':' + c for c in COLS)})",
            rows)


# --------------------------------------------------------------------------
# Kalshi: two YES/NO markets per game (one per team). The public orderbook
# lists BIDS only; the price to BUY yes is 1 - (best NO bid).
# --------------------------------------------------------------------------
def kalshi_levels(ob):
    fp = ob.get("orderbook_fp")
    if fp:
        yes = [(float(p), float(q)) for p, q in fp.get("yes_dollars") or []]
        no = [(float(p), float(q)) for p, q in fp.get("no_dollars") or []]
    else:  # legacy cents format
        leg = ob.get("orderbook") or {}
        yes = [(p / 100, float(q)) for p, q in leg.get("yes") or []]
        no = [(p / 100, float(q)) for p, q in leg.get("no") or []]
    bids = sorted(yes, reverse=True)
    asks = sorted((round(1 - p, 4), q) for p, q in no)
    return asks, bids


def collect_kalshi(series):
    markets, cursor = [], None
    while True:
        params = dict(series_ticker=series, status="open", limit=1000)
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets", **params)
        markets += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    events = {}
    for m in markets:
        events.setdefault(m["event_ticker"], []).append(m)
    rows = []
    for ev, ms in events.items():
        date = kalshi_date(ev)
        if len(ms) != 2 or (date and date < today_et()):
            continue
        codes = [team_code(m.get("yes_sub_title")) or team_code(m["ticker"].rsplit("-", 1)[-1])
                 for m in ms]
        key = make_key(date, *codes)
        for m, code in zip(ms, codes):
            asks, bids = kalshi_levels(get(f"{KALSHI}/markets/{m['ticker']}/orderbook"))
            rows.append(row("kalshi", key, date, None, m.get("yes_sub_title"), code,
                            m["ticker"], asks, bids, None, None,
                            {"title": m.get("title"), "rules": m.get("rules_primary"),
                             "close_time": m.get("close_time")}))
            time.sleep(0.1)
    return rows


# --------------------------------------------------------------------------
# Polymarket: Gamma finds the game + token IDs, CLOB /book gives the depth.
# --------------------------------------------------------------------------
def jl(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def is_moneyline(m, outcomes):
    if m.get("sportsMarketType"):
        return m["sportsMarketType"] == "moneyline"
    q = (m.get("question") or "").lower()
    bad = ("spread", "o/u", "total", "over", "under", "1h", "half", "quarter")
    return not any(b in q for b in bad) and not {o.lower() for o in outcomes} & {
        "yes", "no", "over", "under"}


def collect_polymarket(series_id, tag_id):
    events = get(f"{GAMMA}/events", series_id=series_id, tag_id=tag_id, active="true",
                 closed="false", order="startTime", ascending="true", limit=100)
    now, rows = datetime.now(timezone.utc), []
    for ev in events:
        for m in ev.get("markets", []):
            outs, toks = jl(m.get("outcomes")), jl(m.get("clobTokenIds"))
            if len(outs) != 2 or len(toks) != 2 or not is_moneyline(m, outs):
                continue
            start = m.get("gameStartTime") or ev.get("startTime")
            date = slug_date(ev.get("slug")) or (et_date(start) if start else None)
            pregame = (parse_dt(start) > now) if start else None
            if pregame is False or (date and date < today_et()):
                continue
            codes = [team_code(o) for o in outs]
            key = make_key(date, *codes)
            for o, c, tok in zip(outs, codes, toks):
                book = get(f"{CLOB}/book", token_id=tok)
                asks = sorted((float(x["price"]), float(x["size"])) for x in book.get("asks", []))
                bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids", [])),
                              reverse=True)
                rows.append(row("polymarket", key, date, start, o, c, tok, asks, bids,
                                pregame, None,
                                {"event": ev.get("slug"), "question": m.get("question"),
                                 "condition_id": m.get("conditionId")}))
                time.sleep(0.1)
    return rows


# --------------------------------------------------------------------------
# Sportsbooks via The Odds API (1 request = 1 credit; no size is published)
# --------------------------------------------------------------------------
def american_to_decimal(a):
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def collect_odds(api_key, bookmakers):
    r = HTTP.get(f"{ODDS}/sports/basketball_nba/odds", timeout=15,
                 params=dict(apiKey=api_key, regions="us", markets="h2h",
                             bookmakers=bookmakers, oddsFormat="american"))
    r.raise_for_status()
    log.info("Odds API credits remaining: %s", r.headers.get("x-requests-remaining"))
    now, rows = datetime.now(timezone.utc), []
    for g in r.json():
        start = g["commence_time"]
        pregame = parse_dt(start) > now
        date = et_date(start)
        key = make_key(date, team_code(g["home_team"]), team_code(g["away_team"]))
        for bk in g.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] != "h2h":
                    continue
                for o in mk["outcomes"]:
                    price = round(1 / american_to_decimal(o["price"]), 4)
                    rows.append(row(bk["key"], key, date, start, o["name"],
                                    team_code(o["name"]), g["id"], [(price, None)], [],
                                    pregame, bk.get("last_update"), {"american": o["price"]}))
    return [x for x in rows if x["pregame"]]


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run(args):
    db = connect(args.db)
    odds_key = os.environ.get("ODDS_API_KEY")
    if not odds_key:
        log.warning("ODDS_API_KEY not set: sportsbooks will be skipped")
    next_odds = 0.0
    while True:
        started, jobs = time.time(), [
            ("kalshi", lambda: collect_kalshi(args.kalshi_series)),
            ("polymarket", lambda: collect_polymarket(args.pm_series, args.pm_tag))]
        if odds_key and started >= next_odds:
            jobs.append(("odds_api", lambda: collect_odds(odds_key, args.books)))
            next_odds = started + args.odds_interval
        for name, fn in jobs:  # one source failing must not stop the others
            try:
                rows = fn()
                save(db, rows)
                unmatched = sum(r["game_key"] is None for r in rows)
                log.info("%-10s saved %3d quotes (%d without a game_key)", name, len(rows), unmatched)
            except Exception as e:
                log.error("%s failed: %s", name, e)
        if args.once:
            return
        time.sleep(max(1, args.interval - (time.time() - started)))


# --------------------------------------------------------------------------
# show: the Week 1 milestone table + the hand-trace numbers
# --------------------------------------------------------------------------
def show(args):
    db = connect(args.db)
    rows = db.execute("""SELECT q.* FROM quotes q JOIN
        (SELECT MAX(id) mid FROM quotes WHERE game_key IS NOT NULL
         GROUP BY source, game_key, team_code) m ON q.id = m.mid
        ORDER BY q.game_key, q.source""").fetchall()
    games = {}
    for r in rows:
        games.setdefault(r["game_key"], []).append(r)
    if not games:
        return print("No matched quotes yet. Run `python collector.py run --once` first.")
    key = args.game or max(sorted(games), key=lambda k: len({r["source"] for r in games[k]}))
    team_a, team_b = key.split(":")[1].split("-")
    print(f"\nGame {key}   (buy price = $ per $1 payout; lower is better)\n")
    print(f"{'source':<12}{team_a:>8}{'size':>9}{team_b:>8}{'size':>9}   latest quote")
    best = {}
    for src in sorted({r["source"] for r in games[key]}):
        cells, ts = [], ""
        for t in (team_a, team_b):
            r = next((x for x in games[key] if x["source"] == src and x["team_code"] == t), None)
            if r and r["buy_price"] is not None:
                size = "n/a" if r["buy_size"] is None else f"{r['buy_size']:.0f}"
                cells += [f"{r['buy_price']:.3f}", size]
                ts = max(ts, r["ts"])
                if t not in best or r["buy_price"] < best[t][0]:
                    best[t] = (r["buy_price"], src)
            else:
                cells += ["--", ""]
        print(f"{src:<12}{cells[0]:>8}{cells[1]:>9}{cells[2]:>8}{cells[3]:>9}   {ts}")
    if len(best) == 2:
        cost = best[team_a][0] + best[team_b][0]
        print(f"\nCheapest {team_a}: {best[team_a][0]:.3f} on {best[team_a][1]}"
              f"\nCheapest {team_b}: {best[team_b][0]:.3f} on {best[team_b][1]}"
              f"\nCost of one of each: {cost:.3f} -> gap {1 - cost:+.3f} per $1"
              "\n(before fees, size limits, and rule checks: NOT yet a tradable opportunity)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="quotes.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="collect quotes")
    r.add_argument("--once", action="store_true")
    r.add_argument("--interval", type=int, default=60, help="seconds between Kalshi/Polymarket snapshots")
    r.add_argument("--odds-interval", type=int, default=7200,
                   help="seconds between Odds API calls (free tier credits are limited)")
    r.add_argument("--books", default="draftkings,fanduel")
    r.add_argument("--kalshi-series", default="KXNBAGAME")
    r.add_argument("--pm-series", default="10345", help="Polymarket NBA series_id")
    r.add_argument("--pm-tag", default="100639", help="Polymarket 'games' tag_id")
    s = sub.add_parser("show", help="print the cross-platform table for one game")
    s.add_argument("--game", help="e.g. 2026-11-03:BOS-NYK")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    {"run": run, "show": show}[args.cmd](args)


if __name__ == "__main__":
    main()
