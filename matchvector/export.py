"""Export compact JSON for the static website in docs/ (read by docs/index.html).

The nightly GitHub Action runs this after the sync and commits docs/data/ if it changed,
so the site never needs database credentials.
"""
import html
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, positions
from .cache import WEEK, cached_rows, rank_history
from .betting import BOOKMAKER, CAUTIOUS_RULE, is_cautious
from .predictions import GOAL_LINES, goal_lines

log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"
PAST_DAYS = 21       # recent results shown on the site
FUTURE_DAYS = 60     # upcoming fixtures shown on the site
FORM_GAMES = 6       # rank change over this many recent games = "form"


def market_probabilities(conn):
    """{fixture_id: (p_home, p_draw, p_away)} from the stored match-winner odds: each
    bookmaker's 1/odds normalised to remove its margin, then averaged across bookmakers."""
    books = {}
    for fid, bm, sel, odd in conn.execute(
            "select fixture_id, bookmaker_id, selection, odd from odds where bet_id = 1 and odd > 1"):
        books.setdefault((fid, bm), {})[sel] = 1 / float(odd)
    per_fixture = {}
    for (fid, _), p in books.items():
        if len(p) == 3:
            total = p["Home"] + p["Draw"] + p["Away"]
            per_fixture.setdefault(fid, []).append((p["Home"] / total, p["Draw"] / total, p["Away"] / total))
    return {fid: tuple(sum(x[i] for x in ps) / len(ps) for i in range(3)) for fid, ps in per_fixture.items()}


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _xi_lines(vals):
    """[GK, DEF, MID, FWD] average ranks rounded, or None when no line has anyone."""
    vals = [_r(v, 1) for v in vals]
    return vals if any(v is not None for v in vals) else None


def export_site_data(conn, out_dir=OUT_DIR):
    now = datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)

    competitions = {
        lid: {"name": name, "country": country, "type": ltype}
        for lid, name, country, ltype in conn.execute(
            "select league_id, name, country, type from leagues")
    }

    market = market_probabilities(conn)
    matches = []
    team_ids = set()
    for row in conn.execute(
            """select f.fixture_id, f.kickoff, f.league_id, f.round, f.home_team_id, f.away_team_id,
                      f.status_short, f.home_goals, f.away_goals, f.pen_home, f.pen_away,
                      p.p_home, p.p_draw, p.p_away, p.home_xg, p.away_xg, p.likely_score,
                      p.home_rank, p.away_rank, p.source, p.rating, p.rating_winner,
                      p.rating_margin, p.rating_clean_sheets, p.rating_shape, p.rating_goals,
                      p.home_missing, p.away_missing, p.p_over25, p.p_btts,
                      coalesce(rh.actual_xi_rating, rh.predicted_xi_rating), rh.recent_xi_rating,
                      coalesce(ra.actual_xi_rating, ra.predicted_xi_rating), ra.recent_xi_rating,
                      coalesce(rh.actual_gk, rh.predicted_gk), coalesce(rh.actual_def, rh.predicted_def),
                      coalesce(rh.actual_mid, rh.predicted_mid), coalesce(rh.actual_fwd, rh.predicted_fwd),
                      coalesce(ra.actual_gk, ra.predicted_gk), coalesce(ra.actual_def, ra.predicted_def),
                      coalesce(ra.actual_mid, ra.predicted_mid), coalesce(ra.actual_fwd, ra.predicted_fwd)
               from fixtures f left join fixture_predictions p using (fixture_id)
               left join fixture_team_ratings rh on rh.fixture_id = f.fixture_id and rh.team_id = f.home_team_id
               left join fixture_team_ratings ra on ra.fixture_id = f.fixture_id and ra.team_id = f.away_team_id
               where f.kickoff between %s and %s
               order by f.kickoff, f.fixture_id""",
            [now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)]):
        (fid, kickoff, lid, rnd, home, away, status, hg, ag, ph, pa_, p_h, p_d, p_a,
         hxg, axg, likely, hr, ar, source, *ratings, h_miss, a_miss, p_over, p_btts,
         h_xi, h_recent, a_xi, a_recent) = row[:-8]
        lines = row[-8:]            # home then away XI average by line (GK, DEF, MID, FWD)
        team_ids.update((home, away))
        matches.append([
            fid, kickoff.isoformat(), lid, rnd, home, away, status, hg, ag, ph, pa_,
            _r(p_h, 3), _r(p_d, 3), _r(p_a, 3), _r(hxg), _r(axg), likely, _r(hr, 0), _r(ar, 0),
            source, *ratings,
            *[_r(x, 3) for x in market.get(fid, (None, None, None))],
            _r(h_miss), _r(a_miss), _r(p_over, 3), _r(p_btts, 3),
            _r(h_xi, 1), _r(h_recent, 1), _r(a_xi, 1), _r(a_recent, 1),
            _xi_lines(lines[:4]), _xi_lines(lines[4:]),
        ])

    # Form: total rank change over each team's last FORM_GAMES games
    form = dict(conn.execute(
        """select team_id, sum(rank_change) from (
             select team_id, rank_change,
                    row_number() over (partition by team_id order by match_no desc) rn
             from team_rank_history) x
           where rn <= %s group by team_id""", [FORM_GAMES]).fetchall())

    # League each club is playing in this season (league fixtures in a current season); null for
    # clubs relegated out of every tracked league or only seen in cups
    current_league = dict(conn.execute(
        """select distinct on (team_id) team_id, league_id from (
             select f.home_team_id team_id, f.league_id, f.kickoff from fixtures f
               join leagues l using (league_id) join league_seasons ls using (league_id, season)
               where l.type = 'League' and ls.is_current
             union all
             select f.away_team_id, f.league_id, f.kickoff from fixtures f
               join leagues l using (league_id) join league_seasons ls using (league_id, season)
               where l.type = 'League' and ls.is_current) x
           order by team_id, kickoff desc""").fetchall())

    rankings = []
    for team, lid, cur, st, lt, rel, played, last, att, dfn, home_r, away_r in conn.execute(
            """select team_id, league_id, current_rank, st_algo, lt_algo, reliability, played,
                      last_match, attack, defence, home_rating, away_rating
               from team_rankings order by lt_algo desc"""):
        team_ids.add(team)
        rankings.append([team, current_league.get(team, lid), _r(cur, 1), _r(st, 1), _r(lt, 1), _r(rel, 0),
                         played, last.isoformat() if last else None, _r(form.get(team), 1),
                         1 if team in current_league else 0,
                         _r(att, 1), _r(dfn, 1), _r(home_r, 1), _r(away_r, 1)])

    teams = {t: n for t, n in conn.execute(
        "select team_id, name from teams where team_id = any(%s)", [list(team_ids)])}

    generated = now.isoformat()
    (out_dir / "matches.json").write_text(json.dumps({
        "generated_at": generated,
        "fields": ["id", "kickoff", "league", "round", "home", "away", "status", "hg", "ag",
                   "pen_h", "pen_a", "p_home", "p_draw", "p_away", "home_xg", "away_xg",
                   "likely", "home_rank", "away_rank", "source", "rating", "r_winner",
                   "r_margin", "r_clean_sheets", "r_shape", "r_goals", "m_home", "m_draw", "m_away",
                   "home_missing", "away_missing", "p_over25", "p_btts",
                   "home_xi", "home_recent_xi", "away_xi", "away_recent_xi", "home_lines", "away_lines"],
        "matches": matches,
        "competitions": competitions,
        "teams": teams,
    }, separators=(",", ":")), encoding="utf-8")
    (out_dir / "rankings.json").write_text(json.dumps({
        "generated_at": generated,
        "fields": ["team", "league", "current", "st", "lt", "reliability", "played", "last_match",
                   "form", "in_league", "attack", "defence", "home", "away"],
        "rankings": rankings,
    }, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d matches and %d rankings to %s", len(matches), len(rankings), out_dir)
    export_stats(conn, out_dir)
    export_bets(conn, out_dir)
    export_injuries(conn, out_dir)
    export_players(conn, out_dir)
    export_player_seasons(conn, out_dir)
    export_clubs(conn, out_dir)
    export_leagues(conn, out_dir)
    export_player_pages(conn, out_dir)


STAT_RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
ENGLISH = [39, 40, 41, 42, 43, 50, 51, 45, 46, 47, 48, 528]


def _stats(rows):
    """Accuracy summary for (p_home, p_draw, p_away, home_xg, away_xg, likely, hg, ag, source,
    rating, r_winner, r_margin, r_clean_sheets, r_shape, r_goals)."""
    n = len(rows)
    if not n:
        return None
    correct = exact = live = home_wins = 0
    logloss = brier = goal_err = 0.0
    calib = [[0, 0.0, 0] for _ in range(10)]   # per 10% bin: count, sum of predicted, hits
    ratings = [0] * 5                               # count of 1s..5s
    factor_sums = [0.0] * 5
    rated = 0
    mk = {"n": 0, "model_ll": 0.0, "market_ll": 0.0, "model_correct": 0, "market_correct": 0}
    for ph, pd, pa, hxg, axg, likely, hg, ag, source, rating, *factors, mprobs in rows:
        if mprobs:
            r_ = 0 if hg > ag else 1 if hg == ag else 2
            mk["n"] += 1
            mk["model_ll"] += -math.log(max((ph, pd, pa)[r_], 1e-6))
            mk["market_ll"] += -math.log(max(mprobs[r_], 1e-6))
            mk["model_correct"] += (ph, pd, pa).index(max(ph, pd, pa)) == r_
            mk["market_correct"] += mprobs.index(max(mprobs)) == r_
        if rating:
            rated += 1
            ratings[rating - 1] += 1
            for i, f in enumerate(factors):
                factor_sums[i] += f
        res = 0 if hg > ag else 1 if hg == ag else 2
        probs = (ph, pd, pa)
        correct += probs.index(max(probs)) == res
        exact += likely == f"{hg}-{ag}"
        live += source == "live"
        home_wins += res == 0
        logloss += -math.log(max(probs[res], 1e-6))
        brier += sum((p - (i == res)) ** 2 for i, p in enumerate(probs))
        goal_err += (abs(hxg - hg) + abs(axg - ag)) / 2
        for i, p in enumerate(probs):
            b = calib[min(9, int(p * 10))]
            b[0] += 1; b[1] += p; b[2] += i == res
    return {
        "n": n, "live": live,
        "correct": round(correct / n, 4), "exact": round(exact / n, 4),
        "home_rate": round(home_wins / n, 4),
        "log_loss": round(logloss / n, 4), "brier": round(brier / n, 4),
        "goal_error": round(goal_err / n, 3),
        "market": {"n": mk["n"],
                   "model_ll": round(mk["model_ll"] / mk["n"], 4),
                   "market_ll": round(mk["market_ll"] / mk["n"], 4),
                   "model_correct": round(mk["model_correct"] / mk["n"], 4),
                   "market_correct": round(mk["market_correct"] / mk["n"], 4)} if mk["n"] else None,
        "rated": rated,
        "rating_avg": round(sum((i + 1) * c for i, c in enumerate(ratings)) / rated, 3) if rated else None,
        "rating_counts": ratings,
        "factor_avgs": dict(zip(["winner", "margin", "clean_sheets", "shape", "goals"],
                                [round(x / rated, 2) for x in factor_sums])) if rated else None,
        "calibration": [[c, round(s / c, 3), round(h / c, 3)] if c else [0, None, None]
                        for c, s, h in calib],
    }


# Stats tab, model vs bookmakers per market: market -> (bet id, API line or "", selections)
STAT_MARKETS = {
    "1X2": (1, "", ("Home", "Draw", "Away")),
    "BTTS": (8, "", ("Yes", "No")),
    **{f"OU{int(l * 10)}": (5, str(l), ("Over", "Under")) for l in GOAL_LINES},
}


def market_consensus(conn, since):
    """{(fixture, market): {"close": [probs], "open": [probs] or None}} for finished fixtures
    since `since`, in STAT_MARKETS' selection order.

    Each bookmaker's complete set of prices with its margin removed, averaged, worked out in the
    database so only a dozen numbers per fixture come back. Closing = the last price before
    kickoff (odds.odd); opening = odds.first_odd, only where the bookmaker's whole set was
    first seen before kickoff (odds loaded after a match was played have no real opening)."""
    lines = [f"{side} {l}" for l in GOAL_LINES for side in ("Over", "Under")]
    out = defaultdict(lambda: {"close": {}, "open": {}})
    for fid, bet, line, sel, close, open_ in conn.execute(
            """with o as (
                 select o.fixture_id, o.bookmaker_id, o.bet_id,
                        case when o.bet_id = 5 then split_part(o.selection, ' ', 2) else '' end as line,
                        case when o.bet_id = 5 then split_part(o.selection, ' ', 1) else o.selection end as sel,
                        o.odd::float8 as odd, o.first_odd::float8 as first_odd,
                        o.first_seen_at < f.kickoff and o.first_odd > 1 as has_open
                 from odds o join fixtures f using (fixture_id)
                 where f.status_short = any(%s) and f.kickoff >= %s and o.odd > 1
                   and (o.bet_id in (1, 8) or (o.bet_id = 5 and o.selection = any(%s)))),
               s as (
                 select *, sum(1 / odd) over w as tot, count(*) over w as n,
                        bool_and(has_open) over w as all_open,
                        sum(case when has_open then 1 / first_odd end) over w as tot_open
                 from o window w as (partition by fixture_id, bookmaker_id, bet_id, line))
               select fixture_id, bet_id, line, sel, avg(1 / odd / tot),
                      avg(1 / first_odd / tot_open) filter (where all_open)
               from s where n = case when bet_id = 1 then 3 else 2 end
               group by 1, 2, 3, 4""",
            [["FT", "AET", "PEN"], since, lines]):
        market = next((m for m, (b, l, _) in STAT_MARKETS.items() if b == bet and l == line), None)
        if market:
            out[(fid, market)]["close"][sel] = close
            if open_ is not None:
                out[(fid, market)]["open"][sel] = open_
    result = {}
    for (fid, market), d in out.items():
        sels = STAT_MARKETS[market][2]
        if all(x in d["close"] for x in sels):
            result[(fid, market)] = {"close": [d["close"][x] for x in sels],
                                     "open": [d["open"][x] for x in sels] if all(x in d["open"] for x in sels) else None}
    return result


def _market_stats(rows):
    """Model vs bookmakers per market for rows of (model {market: probs}, index of what
    happened per market, consensus {market: {close, open}}): log losses on the same matches."""
    out = {}
    for market in STAT_MARKETS:
        n = no = 0
        m_ll = c_ll = mo_ll = o_ll = 0.0
        for model, happened, cons in rows:
            c = cons.get(market)
            if not c or market not in model:
                continue
            i = happened[market]
            ll = lambda p: -math.log(max(p[i], 1e-6))
            n += 1; m_ll += ll(model[market]); c_ll += ll(c["close"])
            if c["open"]:
                no += 1; mo_ll += ll(model[market]); o_ll += ll(c["open"])
        if n:
            out[market] = {"n": n, "model_ll": round(m_ll / n, 4), "close_ll": round(c_ll / n, 4),
                           "open_n": no, "model_open_ll": round(mo_ll / no, 4) if no else None,
                           "open_ll": round(o_ll / no, 4) if no else None}
    return out or None


def export_stats(conn, out_dir=OUT_DIR):
    """Prediction accuracy by date range and competition, for the site's Stats tab."""
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """select f.kickoff, f.league_id, p.p_home, p.p_draw, p.p_away, p.home_xg, p.away_xg,
                  p.likely_score, f.home_goals, f.away_goals, p.source, p.rating, p.rating_winner,
                  p.rating_margin, p.rating_clean_sheets, p.rating_shape, p.rating_goals,
                  f.fixture_id
           from fixture_predictions p join fixtures f using (fixture_id)
           where f.status_short = any(%s) and f.home_goals is not null and f.kickoff >= %s""",
        [["FT", "AET", "PEN"], now - timedelta(days=max(STAT_RANGES.values()))]).fetchall()
    market = market_probabilities(conn)
    rows = [(*r, market.get(r[-1])) for r in rows]

    # every market with odds: the model's chances (goal lines from its projected goals) and
    # what happened over 90 minutes, per fixture
    since = now - timedelta(days=max(STAT_RANGES.values()))
    cons = market_consensus(conn, since)
    with_odds = {fid for fid, _ in cons}
    per_fixture = {}
    for fid, ph, pd, pa, pb, hx, ax, hg, ag in conn.execute(
            """select f.fixture_id, p.p_home, p.p_draw, p.p_away, p.p_btts, p.home_xg, p.away_xg,
                      f.ft_home, f.ft_away
               from fixture_predictions p join fixtures f using (fixture_id)
               where f.fixture_id = any(%s) and f.ft_home is not null and p.home_xg is not null""",
            [list(with_odds)]):
        model = {"1X2": [float(ph), float(pd), float(pa)]}
        if pb is not None:
            model["BTTS"] = [float(pb), 1 - float(pb)]
        for line, po in goal_lines(float(hx), float(ax)).items():
            model[f"OU{int(line * 10)}"] = [po, 1 - po]
        happened = {"1X2": 0 if hg > ag else 1 if hg == ag else 2, "BTTS": 0 if hg > 0 and ag > 0 else 1,
                    **{f"OU{int(l * 10)}": 0 if hg + ag > l else 1 for l in GOAL_LINES}}
        per_fixture[fid] = (model, happened, {m: cons[(fid, m)] for m in STAT_MARKETS if (fid, m) in cons})
    stats = {}
    for key, days in STAT_RANGES.items():
        recent = [r for r in rows if r[0] >= now - timedelta(days=days)]
        by_group = {"all": recent, "eng": [r for r in recent if r[1] in ENGLISH]}
        for r in recent:
            by_group.setdefault(str(r[1]), []).append(r)
        # drop the fixture_id column (second to last) before summarising
        stats[key] = {g: _stats([(*r[2:-2], r[-1]) for r in rs]) for g, rs in by_group.items() if rs}
        for g, rs in by_group.items():
            if g in stats[key]:
                stats[key][g]["markets"] = _market_stats([per_fixture[r[-2]] for r in rs if r[-2] in per_fixture])
    (out_dir / "stats.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "ranges": stats}, separators=(",", ":")), encoding="utf-8")
    log.info("Exported prediction stats for %d finished fixtures", len(rows))


# Paper money shown on the Bets tab: a flat stake per bet out of a starting bank
BET_BANK_GBP = 1000
BET_STAKE_GBP = 10


def _summary(bets):
    settled = [b for b in bets if b["result"] in ("win", "loss")]
    clvs = [b["clv"] for b in settled if b["clv"] is not None]
    staked = len(settled)
    profit = sum(b["profit"] for b in settled)
    return {
        "bets": len(bets), "settled": staked, "pending": sum(1 for b in bets if b["result"] is None),
        "wins": sum(1 for b in settled if b["result"] == "win"),
        "profit": round(profit, 2), "roi": round(profit / staked, 4) if staked else None,
        "avg_odds": round(sum(b["odds"] for b in settled) / staked, 3) if staked else None,
        "avg_clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
        "beat_close": round(sum(1 for c in clvs if c > 0) / len(clvs), 4) if clvs else None,
    }


INJURY_LOOKBACK_DAYS = 21
# reasons that only cover the match they were listed for: left out of a past match's list
ONE_MATCH_REASONS = {"Red Card", "Yellow Cards", "Suspended", "Coach's decision", "Rest", "International duty",
                     "Transfer negotiations", "Personal Reasons"}


def export_injuries(conn, out_dir=OUT_DIR):
    """Each club's injury list for the club page (docs/data/injuries.json): out and doubtful
    players for its next match that has a list, else its latest list from the last
    INJURY_LOOKBACK_DAYS (API-Football publishes a match's list only shortly before it), less
    the one-match reasons (a ban already served). Until the next match's list is out, players
    sent off in the club's latest match are added as suspended. Small, so the match-day run
    refreshes it too.

    missed: how many of the club's played matches in a row he has been on its list, back from its
    latest (matches with no list for the club, e.g. cups, are skipped).
    """
    teams = {}
    for team, fid, kickoff, upcoming, player, name, kind, reason in conn.execute(
            """with pick as (
                   select distinct on (i.team_id) i.team_id, i.fixture_id, f.kickoff,
                          f.status_short in ('NS', 'TBD') and f.kickoff > now() as upcoming
                   from injuries i join fixtures f using (fixture_id)
                   where f.kickoff > now() - %s * interval '1 day'
                   order by i.team_id, (f.status_short in ('NS', 'TBD') and f.kickoff > now()) desc,
                            case when f.kickoff > now() then f.kickoff end, f.kickoff desc)
               select i.team_id, i.fixture_id, pick.kickoff, pick.upcoming, i.player_id, p.name, i.type, i.reason
               from injuries i join pick using (team_id, fixture_id) left join players p using (player_id)
               order by i.team_id, i.type, p.name""", [INJURY_LOOKBACK_DAYS]):
        if not upcoming and reason in ONE_MATCH_REASONS:
            continue
        entry = teams.setdefault(str(team), {"fixture": fid, "kickoff": kickoff.isoformat(), "upcoming": upcoming,
                                             "players": []})
        entry["players"].append([player, html.unescape(name or ""), kind, reason])
    for team, fid, kickoff, player, name in conn.execute(
            """with last as (
                   select distinct on (fp.team_id) fp.team_id, fp.fixture_id, f.kickoff
                   from fixture_players fp join fixtures f using (fixture_id)
                   where f.status_short = any(%s) and f.kickoff > now() - %s * interval '1 day'
                   order by fp.team_id, f.kickoff desc)
               select last.team_id, last.fixture_id, last.kickoff, fp.player_id, p.name
               from last join fixture_players fp using (team_id, fixture_id) left join players p using (player_id)
               where fp.red_cards > 0""", [list(config.FINISHED_STATUSES), INJURY_LOOKBACK_DAYS]):
        entry = teams.get(str(team))
        if entry and entry["upcoming"]:      # the next match's list is out, with any bans on it
            continue
        entry = entry or teams.setdefault(str(team), {"fixture": fid, "kickoff": kickoff.isoformat(),
                                                      "upcoming": False, "players": []})
        if all(row[0] != player for row in entry["players"]):
            entry["players"].append([player, html.unescape(name or ""), "Suspended", "Red card"])
    listed = defaultdict(dict)           # team -> {fixture: (kickoff, {players on its list})}
    for team, fid, kickoff, player in conn.execute(
            """select i.team_id, i.fixture_id, f.kickoff, i.player_id from injuries i join fixtures f using (fixture_id)
               where i.team_id = any(%s) and f.status_short = any(%s) and f.kickoff > now() - interval '365 days'""",
            [[int(t) for t in teams], list(config.FINISHED_STATUSES)]):
        listed[team].setdefault(fid, (kickoff, set()))[1].add(player)
    for team, entry in teams.items():
        lists = [ps for _, ps in sorted(listed[int(team)].values(), key=lambda x: x[0], reverse=True)]
        for row in entry["players"]:
            row.append(next((k for k, ps in enumerate(lists) if row[0] not in ps), len(lists)))
    (out_dir / "injuries.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fields": ["player", "name", "type", "reason", "missed"], "teams": teams,
    }, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("Exported injury lists for %d clubs", len(teams))


def export_bets(conn, out_dir=OUT_DIR):
    """Every paper bet and its result for the site's Bets tab (docs/data/bets.json), all of them
    so the bank there runs from the first bet."""
    rows = conn.execute(
        """select b.bet_id, b.strategy, b.fixture_id, b.kickoff, b.league_id, b.market, b.selection,
                  b.model_prob, b.fair_prob, b.odds_taken, bk.name, b.edge, b.closing_odds, b.clv,
                  b.result, b.profit, b.placed_at, b.settled_at, h.name, a.name,
                  coalesce(f.ft_home, f.home_goals), coalesce(f.ft_away, f.away_goals), b.tags,
                  f.home_team_id, f.away_team_id
           from paper_bets b join fixtures f using (fixture_id)
           join teams h on h.team_id = f.home_team_id join teams a on a.team_id = f.away_team_id
           left join bookmakers bk on bk.bookmaker_id = b.bookmaker_id
           where b.bookmaker_id = %s       -- bets taken elsewhere before Bet365-only stay in the table
           order by b.kickoff desc, b.bet_id""", [BOOKMAKER]).fetchall()
    bets = [{
        "id": r[0], "strategy": r[1], "fixture": r[2], "kickoff": r[3].isoformat(), "league": r[4],
        "market": r[5], "selection": r[6], "model_prob": _r(r[7], 3), "fair_prob": _r(r[8], 3),
        "odds": float(r[9]), "bookmaker": r[10], "edge": _r(r[11], 3),
        "closing_odds": float(r[12]) if r[12] is not None else None, "clv": _r(r[13], 4),
        "result": r[14], "profit": float(r[15]) if r[15] is not None else None,
        "home": r[18], "away": r[19], "home_id": r[23], "away_id": r[24], "score": f"{r[20]}-{r[21]}" if r[20] is not None else None,
        "tags": r[22] or [], "cautious": is_cautious(r[5], r[22]),
    } for r in rows]
    by = lambda key: {k: _summary([b for b in bets if key(b) == k]) for k in sorted({key(b) for b in bets})}
    summary = {
        "all": _summary(bets),
        "strategy": by(lambda b: b["strategy"]),
        "market": by(lambda b: b["market"]),
        "strategy_market": by(lambda b: f"{b['strategy']}|{b['market']}"),
        "league": by(lambda b: str(b["league"])),
        "cautious": _summary([b for b in bets if b["cautious"]]),
        "tag": {t: _summary([b for b in bets if t in b["tags"]]) for t in sorted({t for b in bets for t in b["tags"]})},
    }
    last = max([r[16] for r in rows] + [r[17] for r in rows if r[17]], default=None)
    # No generation timestamp, so the file only changes (and gets committed) when bets do
    (out_dir / "bets.json").write_text(json.dumps({
        "last_change": last.isoformat() if last else None,
        "rules": {"min_edge": 0.03, "max_odds": 10.0, "bookmaker": "Bet365", "stake": 1, "stake_gbp": BET_STAKE_GBP, "bank": BET_BANK_GBP, "cautious_rule": CAUTIOUS_RULE},
        "summary": summary, "bets": bets,
    }, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d paper bets", len(bets))


PLAYER_SEASONS = list(range(2026, 2020, -1))     # season ranks shown, newest first


POSITION_SHARE = 0.25      # a position counts for the filter at this share of his starting minutes

LISTED = """p.current_rank is not null and (p.rank_minutes >= 450 or exists (
    select 1 from player_season_ranks r where r.player_id = p.player_id and r.season = any(%s)
      and r.minutes >= 1500) or exists (select 1 from team_squads s where s.player_id = p.player_id))"""


def export_players(conn, out_dir=OUT_DIR):
    """Current player ranks, season ranks and each team's predicted XI for its next match
    (players.json). A season rank is the player's average rank across that season (after each
    match, weighted by minutes; player_season_ranks), blank if he didn't play in these leagues."""
    # Listed: 450+ minutes in his last 20 appearances, a 1,500+ minute season in the seasons
    # shown (an established player back from injury, e.g. John Stones), or in a current squad
    # (a new signing). His club: the squad he's in now (team_squads), else the club the weekly
    # current-club check found this season (player_career_checks), else his last appearance.
    # Nationality can be corrected by hand in player_overrides
    players = conn.execute(
        f"""with club_league as (           -- each club's league: its latest league fixture
                select distinct on (team_id) team_id, league_id from (
                    select f.home_team_id team_id, f.league_id, f.kickoff from fixtures f join leagues l using (league_id)
                    where l.type = 'League'
                    union all
                    select f.away_team_id, f.league_id, f.kickoff from fixtures f join leagues l using (league_id)
                    where l.type = 'League') x
                order by team_id, kickoff desc),
            this_season as (select max(season) s from fixtures where league_id = any(%s))
           select p.player_id, p.name, p.rank_position, p.current_rank, p.rank_minutes, c.team_id,
                  coalesce(cl.league_id, f.league_id), extract(year from age(p.birth_date))::int,
                  coalesce(o.nationality, p.nationality)
           from players p
           left join player_overrides o using (player_id)
           join lateral (select fp.team_id, fp.fixture_id from fixture_players fp
                         where fp.player_id = p.player_id order by fp.fixture_id desc limit 1) x on true
           join fixtures f on f.fixture_id = x.fixture_id
           left join lateral (select s.team_id from team_squads s where s.player_id = p.player_id
                              order by (s.team_id = x.team_id) desc, s.fetched_at desc limit 1) sq on true
           left join player_career_checks cc on cc.player_id = p.player_id
                and cc.season = (select s from this_season) and cc.team_id is not null
           -- his last club, unless we have its current squad and he isn't in it (he has left and his
           -- new club isn't known yet: no club rather than the wrong one)
           cross join lateral (select coalesce(sq.team_id, cc.team_id,
                case when exists (select 1 from team_squads s2 where s2.team_id = x.team_id) then null
                     else x.team_id end) team_id) c
           left join club_league cl on cl.team_id = c.team_id
           where {LISTED}
           order by p.current_rank desc""", [config.MATCH_PLAYER_LEAGUES, PLAYER_SEASONS]).fetchall()
    season_ranks, estimated = defaultdict(dict), defaultdict(set)
    for player, season, rank, minutes in conn.execute(
            "select player_id, season, season_rank, minutes from player_season_ranks where season = any(%s)",
            [PLAYER_SEASONS]):
        season_ranks[player][season] = float(rank)
        if minutes == 0:                 # a gap season filled from the age curve
            estimated[player].add(season)
    # Positions he started in for POSITION_SHARE+ of his starting minutes over the last 12 months
    # (the position filter includes him there too); minutes off the bench have no position
    role_mins = defaultdict(dict)
    for player, role, mins in conn.execute(
            """select fp.player_id, fp.role, sum(fp.minutes) from fixture_players fp join fixtures f using (fixture_id)
               where fp.player_id = any(%s) and fp.role is not null and fp.minutes > 0
                 and f.status_short = any(%s) and f.kickoff > now() - interval '365 days'
               group by 1, 2""", [[r[0] for r in players], list(config.FINISHED_STATUSES)]):
        role_mins[player][role] = mins
    pos_12m = {p: sorted((r for r, m in rm.items() if m >= POSITION_SHARE * sum(rm.values())), key=lambda r: -rm[r])
               for p, rm in role_mins.items()}
    # his position on the site: where he's started most minutes over the last 12 months (his
    # latest rating window's most common start if he hasn't started in that time)
    main_pos = {p: max(rm, key=rm.get) for p, rm in role_mins.items()}
    pos_ranks = defaultdict(dict)        # {player: {role group: rank as that position}}
    for player, g, r in conn.execute("select player_id, role_group, position_rank from player_position_ranks"):
        pos_ranks[player][g] = float(r)
    # anchored on the position shown for him, so "As <his position>" equals his rank, and no
    # position above his rank (De Cuyper, shown as LW, isn't better as a full-back than overall)
    for r in players:
        ranks = pos_ranks.get(r[0])
        if not ranks:
            continue
        g = positions.group(main_pos.get(r[0], r[2]))
        shift = float(r[3]) - ranks[g] if g in ranks else 0.0
        pos_ranks[r[0]] = {k: round(min(max(v + shift, 0), float(r[3])), 1) for k, v in ranks.items()}
    lineups = conn.execute(
        """select distinct on (pl.team_id, pl.player_id) pl.team_id, pl.fixture_id, pl.player_id,
                  p.name, pl.position, pl.player_rank
           from predicted_lineups pl join players p using (player_id) join fixtures f using (fixture_id)
           where (pl.team_id, f.kickoff) in (select pl2.team_id, min(f2.kickoff) from predicted_lineups pl2
                                             join fixtures f2 using (fixture_id) group by pl2.team_id)
           order by pl.team_id, pl.player_id""").fetchall()
    next_xi = {}
    for team, fid, player, name, pos, rank in lineups:
        entry = next_xi.setdefault(str(team), {"fixture": fid, "players": []})
        entry["players"].append([player, name, pos, float(rank) if rank is not None else None])
    # team-sheet order: keeper, defence right to left, midfield, attack
    order = {r: i for i, r in enumerate(["GK", "RB", "RWB", "CB", "LB", "LWB", "DM", "CM", "RM", "LM",
                                          "AM", "RW", "LW", "ST"])}
    for entry in next_xi.values():
        entry["players"].sort(key=lambda x: (order.get(x[2], 99), -(x[3] or 0)))
    # his next seasons, projected along his age curve (player_ratings.py)
    future = defaultdict(dict)
    for player, season, rank in conn.execute("select player_id, season, projected_rank from player_projected_ranks"):
        future[player][season] = float(rank)
    future_seasons = sorted({y for ys in future.values() for y in ys})
    (out_dir / "players.json").write_text(json.dumps({
        "fields": ["id", "name", "position", "rank", "minutes", "team", "league", "seasons", "age", "estimated",
                   "nationality", "positions_12m", "position_ranks", "future"],
        "seasons": PLAYER_SEASONS,
        "future_seasons": future_seasons,    # oldest first
        "players": [[r[0], r[1], main_pos.get(r[0], r[2]), float(r[3]), r[4], r[5], r[6],
                     [season_ranks[r[0]].get(y) for y in PLAYER_SEASONS], r[7],
                     [i for i, y in enumerate(PLAYER_SEASONS) if y in estimated[r[0]]], r[8],
                     pos_12m.get(r[0], []), pos_ranks.get(r[0], {}),
                     [future[r[0]].get(y) for y in future_seasons]] for r in players],
        "next_xi": next_xi,
        # names of players' clubs outside the club rankings (a move out of our leagues)
        "teams": {str(t): n for t, n in conn.execute(
            "select team_id, name from teams where team_id = any(%s)", [list({r[5] for r in players if r[5]})])},
    }, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("Exported %d player ranks and %d predicted XIs", len(players), len(next_xi))


def _club_spells(rows):
    """[(player, key, team, minutes, club rank, rating, goals, assists)]
    -> {player: {key: [[team, mins, rank, rating, goals, assists], ...]}}, clubs by minutes, most first."""
    out = defaultdict(dict)
    for player, key, team, mins, rank, rating, goals, assists in rows:
        out[player].setdefault(key, []).append(
            [team, int(mins), round(float(rank)) if rank is not None else None,
             round(float(rating), 2) if rating is not None else None, int(goals or 0), int(assists or 0)])
    for seasons in out.values():
        for spells in seasons.values():
            spells.sort(key=lambda x: -x[1])
    return out


def export_player_seasons(conn, out_dir=OUT_DIR):
    """Hover detail for the Players table (player_seasons.json, loaded when that view opens).

    For each exported player and each season in PLAYER_SEASONS, and for "now" (his last 20
    appearances, the ones the current rank is built from): the clubs he played for, his minutes
    for each, the club's average rank over those matches and his average match rating.
    """
    ids = [r[0] for r in conn.execute(f"select player_id from players p where {LISTED}", [PLAYER_SEASONS])]
    per_club = """sum(fp.minutes),
                  sum(h.lt_before * fp.minutes) / nullif(sum(fp.minutes) filter (where h.lt_before is not null), 0),
                  sum(fp.rating * fp.minutes) filter (where fp.rating is not null)
                    / nullif(sum(fp.minutes) filter (where fp.rating is not null), 0),
                  sum(fp.goals), sum(fp.assists)"""
    seasons = conn.execute(
        f"""select fp.player_id, f.season, fp.team_id, {per_club}
            from fixture_players fp join fixtures f using (fixture_id)
            left join team_rank_history h on h.fixture_id = fp.fixture_id and h.team_id = fp.team_id
            where fp.player_id = any(%s) and f.season = any(%s) and f.status_short = any(%s) and fp.minutes > 0
            group by 1, 2, 3""", [ids, PLAYER_SEASONS, list(config.FINISHED_STATUSES)]).fetchall()
    recent = conn.execute(
        f"""with apps as (
                select fp.*, row_number() over (partition by fp.player_id order by f.kickoff desc) as n
                from fixture_players fp join fixtures f using (fixture_id)
                where fp.player_id = any(%s) and f.status_short = any(%s) and fp.minutes > 0
                  and f.kickoff > now() - interval '540 days')
            select fp.player_id, 'now', fp.team_id, {per_club}
            from apps fp left join team_rank_history h on h.fixture_id = fp.fixture_id and h.team_id = fp.team_id
            where fp.n <= 20 group by 1, 3""", [ids, list(config.FINISHED_STATUSES)]).fetchall()
    # seasons away from the per-match leagues: the club he was at and its level that season, with
    # his season totals where the league has them (player_seasons; minutes 0: an estimate)
    gaps = conn.execute(
        """select r.player_id, r.season, r.team_id, r.minutes, avg(h.lt_before),
                  (select sum(ps.rating * ps.minutes) / nullif(sum(ps.minutes) filter (where ps.rating is not null), 0)
                   from player_seasons ps where ps.player_id = r.player_id and ps.season = r.season
                     and ps.team_id = r.team_id)::float8,
                  coalesce((select sum(ps.goals) from player_seasons ps where ps.player_id = r.player_id
                            and ps.season = r.season and ps.team_id = r.team_id), 0),
                  coalesce((select sum(ps.assists) from player_seasons ps where ps.player_id = r.player_id
                            and ps.season = r.season and ps.team_id = r.team_id), 0)
           from player_season_ranks r
           left join fixtures f on f.season = r.season and r.team_id in (f.home_team_id, f.away_team_id)
           left join team_rank_history h on h.fixture_id = f.fixture_id and h.team_id = r.team_id
           where r.team_id is not null and r.player_id = any(%s)
           group by 1, 2, 3, 4""", [ids]).fetchall()
    spells = _club_spells(seasons + recent + gaps)
    # Starting minutes by position per season: the role he started in (from the line-up grid and
    # formation), or his broad position (G/D/M/F) if a start has no grid. Minutes off the bench
    # have no position and aren't counted. Seasons in leagues without per-match data have none
    positions = defaultdict(dict)
    for player, season, role, mins in conn.execute(
            """select fp.player_id, f.season,
                      coalesce(fp.role, fp.position), sum(fp.minutes)
               from fixture_players fp join fixtures f using (fixture_id)
               where fp.player_id = any(%s) and f.season = any(%s) and f.status_short = any(%s) and fp.minutes > 0
                 and fp.started
               group by 1, 2, 3 order by 4 desc""", [ids, PLAYER_SEASONS, list(config.FINISHED_STATUSES)]):
        positions[player].setdefault(str(season), []).append([role, int(mins)])
    # ... and over the last 12 months ("12m") and all our data from 2020/21 ("all")
    for key, since in (("12m", "now() - interval '365 days'"), ("all", "'-infinity'::timestamptz")):
        for player, role, mins in conn.execute(
                f"""select fp.player_id, coalesce(fp.role, fp.position),
                           sum(fp.minutes)
                    from fixture_players fp join fixtures f using (fixture_id)
                    where fp.player_id = any(%s) and f.status_short = any(%s) and fp.minutes > 0
                      and fp.started and f.kickoff > {since}
                    group by 1, 2 order by 3 desc""", [ids, list(config.FINISHED_STATUSES)]):
            positions[player].setdefault(key, []).append([role, int(mins)])
    team_ids = {x[0] for p in spells.values() for v in p.values() for x in v}
    names = dict(conn.execute("select team_id, name from teams where team_id = any(%s)", [list(team_ids)]))
    (out_dir / "player_seasons.json").write_text(json.dumps({
        "fields": ["team", "minutes", "club_rank", "rating", "goals", "assists"],
        "teams": {str(t): names.get(t) for t in team_ids},
        "born": {str(p): b.isoformat() for p, b in conn.execute(
            "select player_id, birth_date from players where player_id = any(%s) and birth_date is not null", [ids])},
        "players": {str(p): {str(k): v for k, v in d.items()} for p, d in spells.items()},
        "positions": {str(p): d for p, d in positions.items()},   # {player: {season: [[role, minutes], ...]}}
    }, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("Exported season detail for %d players", len(spells))


PLAYER_MATCHES = 20      # match log on a player's page: his last this-many appearances
SEASON_FIELDS = ["season", "team", "league", "apps", "starts", "minutes", "rating", "goals", "assists",
                 "shots_on", "key_passes", "passes", "pass_acc", "tackles", "interceptions", "blocks",
                 "duels_won", "duels", "dribbles_won", "fouls", "yellow", "red", "saves", "conceded"]
MATCH_FIELDS = ["fixture", "date", "league", "team", "opponent", "home", "gf", "ga", "started", "minutes",
                "role", "rating", "rank", "goals", "assists", "shots_on", "key_passes", "tackles_int",
                "duels_won", "duels", "yellow", "red", "saves", "conceded"]


def build_player_pages(ids, apps, fixtures, other_seasons):
    """{player: page payload} for docs/data/players/<id>.json, from cached rows (runs offline).

    apps: player_ratings._appearances rows; fixtures: cache.finished_fixtures rows;
    other_seasons: player_ratings._other_seasons rows. Season lines are per club and league: from
    his appearances in the per-match leagues, and the season totals (player_seasons) elsewhere,
    where starts and pass accuracy aren't known. A match's rank (going into it) is filled in later.
    """
    ids = set(ids)
    fx = {r[0]: r for r in fixtures}
    # per (season, team, league): apps, starts, minutes, rated minutes, rating x minutes, goals,
    # assists, shots_on, key_passes, passes, accurate passes, tackles, interceptions, blocks,
    # duels_won, duels, dribbles_won, fouls, yellow, red, saves, conceded
    lines = defaultdict(lambda: defaultdict(lambda: [0] * 22))
    recent = defaultdict(list)
    for r in apps:
        if r[2] not in ids or r[3] <= 0 or r[10] not in config.FINISHED_STATUSES or r[0] not in fx:
            continue
        (goals, assists, shots_on, key_passes, passes, passes_acc, tackles, interceptions, blocks, duels,
         duels_won, dribbles_won, fouls, yellow, red, saves, conceded, _, _) = (x or 0 for x in r[11:30])
        s = lines[r[2]][(r[8], r[1], r[9])]
        for i, v in enumerate((1, 1 if r[4] else 0, r[3], r[3] if r[7] else 0, (r[7] or 0) * r[3],
                               goals, assists, shots_on, key_passes, passes, passes_acc, tackles, interceptions,
                               blocks, duels_won, duels, dribbles_won, fouls, yellow, red, saves, conceded)):
            s[i] += v
        recent[r[2]].append(r)
    out = {}
    for player in ids:
        seasons = [[season, team, league, *s[:3], round(s[4] / s[3], 2) if s[3] else None, *s[5:10],
                    round(100 * s[10] / s[9]) if s[9] else None, *s[11:]]
                   for (season, team, league), s in lines.get(player, {}).items()]
        matches = []
        for r in sorted(recent.get(player, []), key=lambda r: fx[r[0]][1], reverse=True)[:PLAYER_MATCHES]:
            f = fx[r[0]]
            home = r[1] == f[4]
            st = [x or 0 for x in r[11:30]]
            matches.append([r[0], f[1].date().isoformat(), r[9], r[1], f[5] if home else f[4], 1 if home else 0,
                            f[6] if home else f[7], f[7] if home else f[6], 1 if r[4] else 0, r[3],
                            r[6] or r[5], _r(r[7], 1), None, st[0], st[1], st[2], st[3], st[6] + st[7],
                            st[10], st[9], st[13], st[14], st[15], st[16]])
        out[player] = {"seasons": seasons, "matches": matches, "injury": None}
    for (player, team, league, season, _, minutes, n, rating, goals, assists, shots_on, key_passes, passes, _,
         tackles, interceptions, blocks, duels, duels_won, dribbles_won, fouls, yellow, yellow_red, red,
         saves, conceded, _, _) in other_seasons:
        if player in out:
            out[player]["seasons"].append(
                [season, team, league, n or 0, None, minutes, _r(rating), goals or 0, assists or 0, shots_on,
                 key_passes, passes, None, tackles, interceptions, blocks, duels_won, duels, dribbles_won,
                 fouls, yellow, (red or 0) + (yellow_red or 0), saves, conceded])
    for page in out.values():
        page["seasons"].sort(key=lambda x: (-x[0], -x[5]))     # newest season first, then most minutes
    return out


def export_player_pages(conn, out_dir=OUT_DIR):
    """One small file per listed player for his page: docs/data/players/<player_id>.json, with his
    stats per season and club, his last PLAYER_MATCHES appearances (with his rank going into each)
    and his injury status for his next fixture. Built from the query cache (cache.py), so the
    only database reads are the per-match ranks and injuries for those few rows."""
    from .cache import finished_fixtures
    from .player_ratings import _appearances, _other_seasons
    ids = [r[0] for r in conn.execute(f"select player_id from players p where {LISTED}", [PLAYER_SEASONS])]
    apps, fixtures, other = _appearances(conn), finished_fixtures(conn), _other_seasons(conn)
    pages = build_player_pages(ids, apps, fixtures, other)
    fids = sorted({m[0] for p in pages.values() for m in p["matches"]})
    ranks = {(fid, player): float(rank) for fid, player, rank in conn.execute(
        """select fixture_id, player_id, player_rank from fixture_player_ranks
           where fixture_id = any(%s) and player_id = any(%s)""", [fids, ids])}
    injuries = {}
    for player, fid, kind, reason in conn.execute(
            """select i.player_id, i.fixture_id, i.type, i.reason from injuries i join fixtures f using (fixture_id)
               where i.player_id = any(%s) and f.status_short in ('NS', 'TBD') and f.kickoff > now()
               order by f.kickoff""", [ids]):
        injuries.setdefault(player, [fid, kind, reason])
    for pid, page in pages.items():
        for m in page["matches"]:
            m[12] = _r(ranks.get((m[0], pid)), 1)
        page["injury"] = injuries.get(pid)
    team_ids = {x for p in pages.values() for x in [s[1] for s in p["seasons"]] + [m[4] for m in p["matches"]]}
    names = dict(conn.execute("select team_id, name from teams where team_id = any(%s)", [list(team_ids)]))
    player_dir = out_dir / "players"
    player_dir.mkdir(parents=True, exist_ok=True)
    for old in player_dir.glob("*.json"):
        if int(old.stem) not in pages:
            old.unlink()
    for pid, page in pages.items():
        teams = {s[1] for s in page["seasons"]} | {m[4] for m in page["matches"]}
        payload = json.dumps({"id": pid, "season_fields": SEASON_FIELDS, "match_fields": MATCH_FIELDS, **page,
                              "teams": {str(t): names.get(t) for t in sorted(teams)}},
                             separators=(",", ":"), ensure_ascii=False)
        path = player_dir / f"{pid}.json"
        if not path.exists() or path.read_text(encoding="utf-8") != payload:
            path.write_text(payload, encoding="utf-8")
    log.info("Exported %d player pages", len(pages))


CLUB_ACTIVE_DAYS = 400
# Finished matches at a neutral ground: in a city where neither club played its league home games
# that season (2+ of them) and not at either's league ground by name (the API's venue names and
# cities vary: Bayern's league games are at "Fußball Arena München", its cup games at "Allianz
# Arena"; BayArena's city is sometimes "Bayer Leverkusen"). Wembley, the Stade de France and La
# Cartuja host finals in their clubs' own cities, so they're neutral unless they're the home
# club's league ground (Betis at La Cartuja while their stadium is rebuilt).
NEUTRAL_SQL = """
    with hv as (
        select f.home_team_id t, f.season, lower(trim(split_part(f.venue_city, ',', 1))) c, f.venue_name n
        from fixtures f join leagues l using (league_id) where l.type = 'League'),
    hc as (select t, season, c from hv where c is not null group by 1, 2, 3 having count(*) >= 2),
    hn as (select t, season, n from hv where n is not null group by 1, 2, 3 having count(*) >= 2)
    select f.fixture_id from fixtures f
    where f.venue_city is not null and f.status_short = any(%s)
      and exists (select 1 from hc where hc.t = f.home_team_id and hc.season = f.season)
      and not exists (select 1 from hn where hn.t = f.home_team_id and hn.season = f.season and hn.n = f.venue_name)
      and (f.venue_name ~* '^(wembley|stade de france|estadio de la cartuja)'
           or (not exists (select 1 from hn where hn.t = f.away_team_id and hn.season = f.season and hn.n = f.venue_name)
               and not exists (select 1 from hc where hc.t in (f.home_team_id, f.away_team_id) and hc.season = f.season
                               and hc.c = lower(trim(split_part(f.venue_city, ',', 1))))))"""


def export_clubs(conn, out_dir=OUT_DIR):
    """One small file per active club for its club page: docs/data/clubs/<team_id>.json.

    history: every match since 2020 as [date, rank after, opponent, home (1, 0 away, 2 neutral), goals for, against,
    competition, formation (null where the line-up isn't known), attack and defence after,
    starting XI average rank by line [GK, DEF, MID, FWD] (null outside the line-up leagues)]; plus 12-month home/away goal
    averages, the current manager and the home kit colours. Loaded only when the page opens.
    """
    now = datetime.now(timezone.utc)
    active = {r[0] for r in conn.execute(
        """select team_id from team_rankings where last_match >= %s
           union select home_team_id from fixtures where status_short in ('NS','TBD') and kickoff > now()
           union select away_team_id from fixtures where status_short in ('NS','TBD') and kickoff > now()""",
        [now - timedelta(days=CLUB_ACTIVE_DAYS)])}
    formations = {(f, t): fm for f, t, fm in cached_rows(conn, "formations", f"""
            select {WEEK.format('f.kickoff')} as part, ff.fixture_id, ff.team_id, ff.formation
            from fixture_formations ff join fixtures f using (fixture_id) where ff.formation is not null""",
            order_by="fixture_id, team_id")}
    coaches = {t: {"id": c, "name": n, "photo": p, "since": s.isoformat() if s else None}
               for t, c, n, p, s in conn.execute("select team_id, coach_id, name, photo, since from team_coaches")}
    colors = {t: [s, n] for t, s, n in conn.execute("select team_id, shirt, number from team_colors")}
    xi_lines = {(f, t): _xi_lines(rest) for f, t, *rest in cached_rows(conn, "xi_lines", f"""
            select {WEEK.format('f.kickoff')} as part, r.fixture_id, r.team_id,
                   r.actual_gk::float8, r.actual_def::float8, r.actual_mid::float8, r.actual_fwd::float8
            from fixture_team_ratings r join fixtures f using (fixture_id) where r.actual_xi_rating is not null""",
            order_by="fixture_id, team_id")}
    neutral = {f for (f,) in conn.execute(NEUTRAL_SQL, [list(config.FINISHED_STATUSES)])}
    history = {}
    club_rows = sorted((r for r in rank_history(conn) if r[1] in active), key=lambda r: (r[1], r[2]))
    for fid, team, _, kickoff, is_home, opp, rank_before, rank_after, _, hg, ag, league, att, dfn, *_ in club_rows:
        rows = history.setdefault(team, {"start": round(rank_before), "matches": []})["matches"]
        gf, ga = (hg, ag) if is_home else (ag, hg)
        rows.append([kickoff.date().isoformat(), round(rank_after, 1), opp, 2 if fid in neutral else 1 if is_home else 0,
                     gf, ga, league,
                     formations.get((fid, team)), _r(att, 1), _r(dfn, 1), xi_lines.get((fid, team))])
    stats = {t: [_r(x) for x in rest] for t, *rest in conn.execute(
        "select team_id, hg, ha, ag, aa from team_rankings where team_id = any(%s)", [list(active)])}
    club_dir = out_dir / "clubs"
    club_dir.mkdir(parents=True, exist_ok=True)
    for old in club_dir.glob("*.json"):
        if int(old.stem) not in active:
            old.unlink()
    names = dict(conn.execute("select team_id, name from teams"))
    for team in active:
        h = history.get(team, {"start": None, "matches": []})
        opponents = {m[2] for m in h["matches"]}
        payload = {"id": team, "start": h["start"],
                   "fields": ["date", "rank", "opponent", "home", "gf", "ga", "league", "formation",
                              "attack", "defence", "xi_lines"],
                   "matches": h["matches"], "goal_averages": stats.get(team), "coach": coaches.get(team),
                   "colors": colors.get(team), "teams": {o: names.get(o) for o in opponents}}
        (club_dir / f"{team}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d club pages", len(active))


def export_leagues(conn, out_dir=OUT_DIR):
    """One file per competition for its league page: docs/data/leagues/<league_id>.json.

    The current season's table (every group, as API-Football sends it) and all its fixtures.
    Loaded only when the page opens.
    """
    seasons = {lid: (season, start) for lid, season, start in conn.execute(
        """select distinct on (league_id) league_id, season, start_date from league_seasons
           where is_current order by league_id, season desc""")}
    tables = defaultdict(list)
    for (lid, season, group, team, rank, pts, gd, form, desc, pl, w, d, l, gf, ga) in conn.execute(
            """select league_id, season, group_name, team_id, rank, points, goal_diff, form, description,
                      played, win, draw, lose, goals_for, goals_against
               from standings where (league_id, season) in (select league_id, max(season) from league_seasons
                                                             where is_current group by league_id)
               order by league_id, group_name, rank"""):
        tables[lid].append([group, rank, team, pl, w, d, l, gf, ga, gd, pts, form, desc])
    fixtures = defaultdict(list)
    for fid, lid, kickoff, rnd, home, away, status, hg, ag, ph, pa_ in conn.execute(
            """select fixture_id, league_id, kickoff, round, home_team_id, away_team_id, status_short,
                      home_goals, away_goals, pen_home, pen_away
               from fixtures where (league_id, season) in (select league_id, max(season) from league_seasons
                                                          where is_current group by league_id)
               order by kickoff, fixture_id"""):
        fixtures[lid].append([fid, kickoff.isoformat(), rnd, home, away, status, hg, ag, ph, pa_])
    names = dict(conn.execute("select team_id, name from teams"))
    league_dir = out_dir / "leagues"
    league_dir.mkdir(parents=True, exist_ok=True)
    for old in league_dir.glob("*.json"):
        if int(old.stem) not in seasons:
            old.unlink()
    for lid, (season, start) in seasons.items():
        teams = {r[2] for r in tables[lid]} | {t for f in fixtures[lid] for t in (f[3], f[4])}
        payload = {"id": lid, "season": season, "start": start.isoformat() if start else None,
                   "table_fields": ["group", "rank", "team", "played", "win", "draw", "lose", "gf", "ga", "gd",
                                    "points", "form", "description"],
                   "table": tables[lid],
                   "fixture_fields": ["id", "kickoff", "round", "home", "away", "status", "hg", "ag", "pen_h", "pen_a"],
                   "fixtures": fixtures[lid],
                   "teams": {t: names.get(t) for t in teams}}
        (league_dir / f"{lid}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d league pages", len(seasons))
