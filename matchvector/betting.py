"""Paper betting: record the bets the model would place, settle them, and measure CLV.

No real money. Two strategies are tracked separately:
    early - placed by the nightly run for matches in the next EARLY_HOURS hours
    late  - placed by the match-day run shortly before kickoff (after late injury news)

A bet is placed on any selection where model_prob * Bet365's price - 1 is at least MIN_EDGE
(prices above MAX_ODDS are skipped): Bet365 (BOOKMAKER) is the only bookmaker bet with, so only
its prices are taken. The fair (margin-free) chances still average every bookmaker. Each selection is bet at most once per
strategy, 1 unit flat stake. Settlement uses the 90-minute score, like bookmakers.

Closing line value: clv = odds_taken * closing fair probability - 1. Consistently positive CLV
is the standard early sign of a real edge, long before profit is statistically meaningful.

Tags (paper_bets.tags, see bet_tags): what kind of disagreement each bet is, so returns and CLV
can be compared by kind as bets build up. From the check of the first 82 model-vs-bookmaker
disagreements (README), the model did worst backing outsiders and in streak matches, so the
Bets tab also shows a "cautious" view that leaves those out (is_cautious). It's a view over the
same bets rather than a separate strategy: same prices and timing, so the comparison is exact.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .predictions import goal_lines

log = logging.getLogger(__name__)

MIN_EDGE = 0.03
MAX_ODDS = 10.0
BOOKMAKER = 8              # Bet365 (bookmakers.bookmaker_id): the one bookmaker bets are taken with
BIG5 = {39, 140, 135, 78, 61}
EUROPE = {2, 3, 848, 531}
STREAK_POINTS = 30         # |(Form - Rating) home - (Form - Rating) away| at or above this: "streak"
THIN_DATA_GAMES = 25       # home side's home games + away side's away games in 12 months below this
BIG_GAP = 0.10             # model chance this far above the bookmakers': "gap10"
CAUTIOUS_RULE = "leaves out result bets on outsiders and any bet in a streak match"


def is_cautious(market, tags):
    """The cautious view: the same bets less result-market bets on an outsider (the evidence was
    on the result market only) and any bet in a streak match."""
    tags = tags or []
    return "streak" not in tags and not (market == "1X2" and "outsider" in tags)
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

    best = BOOKMAKER's price (the one bets are taken at); fair = each bookmaker's complete set
    of prices with its margin removed, averaged over every bookmaker."""
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
            if bm == BOOKMAKER:
                best = {sel: (odd, bm) for sel, odd in prices.items()}
            if len(prices) == len(sels):
                inv = {s: 1 / prices[s] for s in sels}
                total = sum(inv.values())
                fair_sets.append({s: inv[s] / total for s in sels})
        fair = {s: sum(f[s] for f in fair_sets) / len(fair_sets) for s in sels} if fair_sets else {}
        out[fid][market] = {"best": best, "fair": fair}
    return out


def match_tags(conn, fixture_ids):
    """{fixture: [tags]} for the match itself, as known before kickoff: 'cup' (cups and European
    games), 'big5' or 'league'; 'promoted' when either club's league changed since last season;
    'thin_data'; 'streak' (one side's Form is far from its Rating relative to the other's).
    Ranks come from team_rank_history (going into the match) when it's there, else team_rankings."""
    fixture_ids = list(fixture_ids)
    if not fixture_ids:
        return {}
    info = conn.execute(
        """select f.fixture_id, f.league_id, l.type, f.home_team_id, f.away_team_id, f.kickoff
           from fixtures f join leagues l using (league_id) where f.fixture_id = any(%s)""",
        [fixture_ids]).fetchall()
    teams = list({t for r in info for t in r[3:5]})
    before = {(fid, team): (now, lt) for fid, team, now, lt in conn.execute(
        """select fixture_id, team_id, rank_before, lt_before from team_rank_history
           where fixture_id = any(%s)""", [fixture_ids])}
    current = {t: (now, lt) for t, now, lt in conn.execute(
        "select team_id, current_rank, lt_algo from team_rankings where team_id = any(%s)", [teams])}
    # every finished match of these clubs in the last 13 months (league type for the moves)
    history = defaultdict(list)       # team -> [(kickoff, league, league type, at home)]
    for kickoff, league, ltype, home, away in conn.execute(
            """select f.kickoff, f.league_id, l.type, f.home_team_id, f.away_team_id
               from fixtures f join leagues l using (league_id)
               where f.status_short in ('FT', 'AET', 'PEN') and f.kickoff > now() - interval '400 days'
                 and (f.home_team_id = any(%s) or f.away_team_id = any(%s))""", [teams, teams]):
        history[home].append((kickoff, league, ltype, True))
        history[away].append((kickoff, league, ltype, False))

    def league_between(team, ko, lo_days, hi_days):
        lgs = [lg for k, lg, t, _ in history[team] if t == "League"
               and ko - timedelta(days=hi_days) <= k < ko - timedelta(days=lo_days)]
        return max(set(lgs), key=lgs.count) if lgs else None

    def moved(team, ko):
        now, old = league_between(team, ko, 0, 60), league_between(team, ko, 90, 365)
        return now is not None and old is not None and now != old

    out = {}
    for fid, league, ltype, home, away, ko in info:
        tags = ["cup" if ltype == "Cup" or league in EUROPE else "big5" if league in BIG5 else "league"]
        if moved(home, ko) or moved(away, ko):
            tags.append("promoted")
        games = sum(1 for k, _, _, at_home in history[home] if at_home and ko - timedelta(days=365) <= k < ko) + \
            sum(1 for k, _, _, at_home in history[away] if not at_home and ko - timedelta(days=365) <= k < ko)
        if games < THIN_DATA_GAMES:
            tags.append("thin_data")
        (hn, hl), (an, al) = (before.get((fid, t)) or current.get(t, (None, None)) for t in (home, away))
        if None not in (hn, hl, an, al) and abs((hn - hl) - (an - al)) >= STREAK_POINTS:
            tags.append("streak")
        out[fid] = tags
    return out


def bet_tags(match, sel, prob, fair):
    """The match's tags plus the bet's own: 'favourite' / 'outsider' by the bookmakers' fair
    chances, and 'gap10' when the model is 10+ points above them."""
    tags = list(match)
    if fair and sel in fair:
        tags.append("favourite" if fair[sel] >= max(fair.values()) else "outsider")
        if prob - fair[sel] >= BIG_GAP:
            tags.append("gap10")
    return tags


def tag_untagged(conn):
    """Tag bets placed before tagging existed (fair chances from the stored odds)."""
    bets = conn.execute("select bet_id, fixture_id, market, selection, model_prob from paper_bets "
                        "where tags is null").fetchall()
    if not bets:
        return 0
    fids = {b[1] for b in bets}
    matches, prices = match_tags(conn, fids), load_prices(conn, fids)
    with conn.cursor() as cur:
        cur.executemany("update paper_bets set tags = %s where bet_id = %s", [
            (bet_tags(matches.get(fid, []), sel, prob, prices.get(fid, {}).get(market, {}).get("fair")), bid)
            for bid, fid, market, sel, prob in bets])
    conn.commit()
    log.info("Tagged %d older paper bets", len(bets))
    return len(bets)


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
                    rows.append([strategy, fid, league_id, kickoff, market, sel, prob,
                                 fair.get(sel), odd, bm, edge, fair])
    matches = match_tags(conn, {r[1] for r in rows})
    for r in rows:
        r[-1] = bet_tags(matches.get(r[1], []), r[5], r[6], r[-1])
    with conn.cursor() as cur:
        cur.executemany(
            """insert into paper_bets (strategy, fixture_id, league_id, kickoff, market, selection,
               model_prob, fair_prob, odds_taken, bookmaker_id, edge, tags)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
