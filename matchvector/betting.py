"""Paper betting: record the bets the model would place, settle them, and measure CLV.

No real money. Two strategies are tracked separately:
    early - placed by the nightly run for matches in the next EARLY_HOURS hours
    late  - placed by the match-day run shortly before kickoff (after late injury news)

A bet is placed on any selection where model_prob * best price across bookmakers - 1 is at
least MIN_EDGE (prices above MAX_ODDS are skipped). Each selection is bet at most once per
strategy, 1 unit flat stake. Settlement uses the 90-minute score, like bookmakers.

Closing line value: clv = odds_taken * closing fair probability - 1. Consistently positive CLV
is the standard early sign of a real edge, long before profit is statistically meaningful.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .predictions import goal_lines

log = logging.getLogger(__name__)

MIN_EDGE = 0.03
MAX_ODDS = 10.0
EARLY_HOURS = 36
LATE_MINUTES = 75

def _over(line):
    return lambda p: (p["lines"][line], 1 - p["lines"][line])


# market -> (API-Football bet id, selections, how to read model probabilities from a prediction).
# The goal lines other than 2.5 were added on 25 September 2026: in the model-vs-bookmaker check
# the goal markets were the closest to the bookmakers, so they're the ones to gather bets on.
MARKETS = {
    "1X2": (1, ("Home", "Draw", "Away"), lambda p: (p["p_home"], p["p_draw"], p["p_away"])),
    "OU15": (5, ("Over 1.5", "Under 1.5"), _over(1.5)),
    "OU25": (5, ("Over 2.5", "Under 2.5"), lambda p: (p["p_over25"], 1 - p["p_over25"])),
    "OU35": (5, ("Over 3.5", "Under 3.5"), _over(3.5)),
    "OU45": (5, ("Over 4.5", "Under 4.5"), _over(4.5)),
    "BTTS": (8, ("Yes", "No"), lambda p: (p["p_btts"], 1 - p["p_btts"])),
}
SELECTION_MARKET = {(bet, sel): m for m, (bet, sels, _) in MARKETS.items() for sel in sels}


def load_prices(conn, fixture_ids):
    """{fixture: {market: {"best": {sel: (odd, bookmaker)}, "fair": {sel: prob}}}}.

    fair = each bookmaker's complete set of prices with its margin removed, averaged."""
    by_book = defaultdict(lambda: defaultdict(dict))    # (fid, market) -> bookmaker -> {sel: odd}
    for fid, bm, bet, sel, odd in conn.execute(
            """select fixture_id, bookmaker_id, bet_id, selection, odd from odds
               where fixture_id = any(%s) and bet_id = any(%s) and odd > 1""",
            [list(fixture_ids), list({bet for bet, _ in SELECTION_MARKET})]):
        market = SELECTION_MARKET.get((bet, sel))
        if market:
            by_book[(fid, market)][bm][sel] = float(odd)
    out = defaultdict(dict)
    for (fid, market), books in by_book.items():
        sels = MARKETS[market][1]
        best = {}
        fair_sets = []
        for bm, prices in books.items():
            for sel, odd in prices.items():
                if sel not in best or odd > best[sel][0]:
                    best[sel] = (odd, bm)
            if len(prices) == len(sels):
                inv = {s: 1 / prices[s] for s in sels}
                total = sum(inv.values())
                fair_sets.append({s: inv[s] / total for s in sels})
        fair = {s: sum(f[s] for f in fair_sets) / len(fair_sets) for s in sels} if fair_sets else {}
        out[fid][market] = {"best": best, "fair": fair}
    return out


def place_bets(conn, strategy, within):
    """Place paper bets for not-started fixtures kicking off within `within` (timedelta)."""
    now = datetime.now(timezone.utc)
    preds = conn.execute(
        """select p.fixture_id, f.league_id, f.kickoff, p.p_home, p.p_draw, p.p_away,
                  p.p_over25, p.p_btts, p.home_xg, p.away_xg
           from fixture_predictions p join fixtures f using (fixture_id)
           where f.status_short in ('NS', 'TBD') and f.kickoff > %s and f.kickoff <= %s""",
        [now, now + within]).fetchall()
    if not preds:
        log.info("Paper bets (%s): no fixtures in window", strategy)
        return 0
    prices = load_prices(conn, [r[0] for r in preds])
    rows = []
    for fid, league_id, kickoff, ph, pd, pa, po, pb, hx, ax in preds:
        pred = {"p_home": ph, "p_draw": pd, "p_away": pa, "p_over25": po, "p_btts": pb}
        if None in pred.values() or hx is None or ax is None:
            continue
        pred = {k: float(v) for k, v in pred.items()}
        pred["lines"] = goal_lines(float(hx), float(ax))
        for market, (_, sels, model_fn) in MARKETS.items():
            if market not in prices.get(fid, {}):
                continue
            best, fair = prices[fid][market]["best"], prices[fid][market]["fair"]
            for sel, prob in zip(sels, model_fn(pred)):
                if sel not in best:
                    continue
                odd, bm = best[sel]
                edge = prob * odd - 1
                if edge >= MIN_EDGE and odd <= MAX_ODDS:
                    rows.append((strategy, fid, league_id, kickoff, market, sel, prob,
                                 fair.get(sel), odd, bm, edge))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into paper_bets (strategy, fixture_id, league_id, kickoff, market, selection,
               model_prob, fair_prob, odds_taken, bookmaker_id, edge)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (strategy, fixture_id, market, selection) do nothing""", rows)
        placed = cur.rowcount
    conn.commit()
    log.info("Paper bets (%s): %d candidates in %d fixtures, %d new", strategy, len(rows), len(preds), placed)
    return placed


def _won(market, selection, hg, ag):
    if market == "1X2":
        return {"Home": hg > ag, "Draw": hg == ag, "Away": hg < ag}[selection]
    if market.startswith("OU"):
        return (hg + ag > int(market[2:]) / 10) == selection.startswith("Over")
    if market == "BTTS":
        return (hg > 0 and ag > 0) == (selection == "Yes")
    raise ValueError(market)


def settle_bets(conn):
    """Settle open bets on finished or abandoned fixtures and record closing odds / CLV."""
    open_bets = conn.execute(
        """select b.bet_id, b.fixture_id, b.market, b.selection, b.odds_taken, b.stake,
                  f.status_short, coalesce(f.ft_home, f.home_goals), coalesce(f.ft_away, f.away_goals)
           from paper_bets b join fixtures f using (fixture_id)
           where b.settled_at is null
             and f.status_short in ('FT', 'AET', 'PEN', 'PST', 'CANC', 'ABD', 'AWD', 'WO')""").fetchall()
    if not open_bets:
        return 0
    prices = load_prices(conn, [r[1] for r in open_bets])     # odds stop updating at kickoff
    updates = []
    for bet_id, fid, market, sel, odds, stake, status, hg, ag in open_bets:
        closing = prices.get(fid, {}).get(market, {})
        close_odd = closing.get("best", {}).get(sel, (None,))[0]
        close_fair = closing.get("fair", {}).get(sel)
        clv = float(odds) * close_fair - 1 if close_fair else None
        if status in ("PST", "CANC", "ABD") or hg is None:
            result, profit = "void", 0
        else:
            won = _won(market, sel, hg, ag)
            result, profit = ("win", float(stake) * (float(odds) - 1)) if won else ("loss", -float(stake))
        updates.append((close_odd, close_fair, clv, result, profit, bet_id))
    with conn.cursor() as cur:
        cur.executemany(
            """update paper_bets set closing_odds = %s, closing_fair = %s, clv = %s, result = %s,
               profit = %s, settled_at = now() where bet_id = %s""", updates)
    conn.commit()
    log.info("Settled %d paper bets", len(updates))
    return len(updates)


def place_early(conn):
    return place_bets(conn, "early", timedelta(hours=EARLY_HOURS))


def place_late(conn):
    return place_bets(conn, "late", timedelta(minutes=LATE_MINUTES))
