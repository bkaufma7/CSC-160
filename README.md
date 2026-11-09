# NBA price-gap scanner

Compares pregame NBA moneyline prices on Kalshi, Polymarket, DraftKings and FanDuel.

## Setup
    python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    export ODDS_API_KEY=...        # your own free key from the-odds-api.com

## Run
    python collector.py run        # collect quotes into quotes.db (one person runs this)
    python dashboard.py            # view them at http://localhost:8000
    python test_collector.py       # offline tests (needs pytest)

## Team rules
- `main` always works. Make a branch per task, open a pull request, get one review.
- Never commit `quotes.db` or API keys (see .gitignore). Share data snapshots via the team drive.
- Run the tests before opening a pull request.
