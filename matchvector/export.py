"""Export compact JSON for the static website in docs/ (read by docs/index.html).

The nightly GitHub Action runs this after the sync and commits docs/data/ if it changed,
so the site never needs database credentials.
"""
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

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
                      coalesce(ra.actual_xi_rating, ra.predicted_xi_rating), ra.recent_xi_rating
               from fixtures f left join fixture_predictions p using (fixture_id)
               left join fixture_team_ratings rh on rh.fixture_id = f.fixture_id and rh.team_id = f.home_team_id
               left join fixture_team_ratings ra on ra.fixture_id = f.fixture_id and ra.team_id = f.away_team_id
               where f.kickoff between %s and %s
               order by f.kickoff, f.fixture_id""",
            [now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)]):
        (fid, kickoff, lid, rnd, home, away, status, hg, ag, ph, pa_, p_h, p_d, p_a,
         hxg, axg, likely, hr, ar, source, *ratings, h_miss, a_miss, p_over, p_btts,
         h_xi, h_recent, a_xi, a_recent) = row
        team_ids.update((home, away))
        matches.append([
            fid, kickoff.isoformat(), lid, rnd, home, away, status, hg, ag, ph, pa_,
            _r(p_h, 3), _r(p_d, 3), _r(p_a, 3), _r(hxg), _r(axg), likely, _r(hr, 0), _r(ar, 0),
            source, *ratings,
            *[_r(x, 3) for x in market.get(fid, (None, None, None))],
            _r(h_miss), _r(a_miss), _r(p_over, 3), _r(p_btts, 3),
            _r(h_xi, 1), _r(h_recent, 1), _r(a_xi, 1), _r(a_recent, 1),
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
    for team, lid, cur, st, lt, rel, played, last in conn.execute(
            """select team_id, league_id, current_rank, st_algo, lt_algo, reliability, played,
                      last_match from team_rankings order by lt_algo desc"""):
        team_ids.add(team)
        rankings.append([team, current_league.get(team, lid), _r(cur, 1), _r(st, 1), _r(lt, 1), _r(rel, 0),
                         played, last.isoformat() if last else None, _r(form.get(team), 1),
                         1 if team in current_league else 0])

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
                   "home_xi", "home_recent_xi", "away_xi", "away_recent_xi"],
        "matches": matches,
        "competitions": competitions,
        "teams": teams,
    }, separators=(",", ":")), encoding="utf-8")
    (out_dir / "rankings.json").write_text(json.dumps({
        "generated_at": generated,
        "fields": ["team", "league", "current", "st", "lt", "reliability", "played", "last_match",
                   "form", "in_league"],
        "rankings": rankings,
    }, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d matches and %d rankings to %s", len(matches), len(rankings), out_dir)
    export_stats(conn, out_dir)
    export_bets(conn, out_dir)
    export_players(conn, out_dir)
    export_player_seasons(conn, out_dir)
    export_clubs(conn, out_dir)


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
    stats = {}
    for key, days in STAT_RANGES.items():
        recent = [r for r in rows if r[0] >= now - timedelta(days=days)]
        by_group = {"all": recent, "eng": [r for r in recent if r[1] in ENGLISH]}
        for r in recent:
            by_group.setdefault(str(r[1]), []).append(r)
        # drop the fixture_id column (second to last) before summarising
        stats[key] = {g: _stats([(*r[2:-2], r[-1]) for r in rs]) for g, rs in by_group.items() if rs}
    (out_dir / "stats.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "ranges": stats}, separators=(",", ":")), encoding="utf-8")
    log.info("Exported prediction stats for %d finished fixtures", len(rows))


BET_HISTORY_DAYS = 120


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


def export_bets(conn, out_dir=OUT_DIR):
    """Paper bets and their running results for the site's Bets tab (docs/data/bets.json)."""
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """select b.bet_id, b.strategy, b.fixture_id, b.kickoff, b.league_id, b.market, b.selection,
                  b.model_prob, b.fair_prob, b.odds_taken, bk.name, b.edge, b.closing_odds, b.clv,
                  b.result, b.profit, b.placed_at, b.settled_at, h.name, a.name,
                  coalesce(f.ft_home, f.home_goals), coalesce(f.ft_away, f.away_goals)
           from paper_bets b join fixtures f using (fixture_id)
           join teams h on h.team_id = f.home_team_id join teams a on a.team_id = f.away_team_id
           left join bookmakers bk on bk.bookmaker_id = b.bookmaker_id
           where b.kickoff >= %s or b.settled_at is null
           order by b.kickoff desc, b.bet_id""", [now - timedelta(days=BET_HISTORY_DAYS)]).fetchall()
    bets = [{
        "id": r[0], "strategy": r[1], "fixture": r[2], "kickoff": r[3].isoformat(), "league": r[4],
        "market": r[5], "selection": r[6], "model_prob": _r(r[7], 3), "fair_prob": _r(r[8], 3),
        "odds": float(r[9]), "bookmaker": r[10], "edge": _r(r[11], 3),
        "closing_odds": float(r[12]) if r[12] is not None else None, "clv": _r(r[13], 4),
        "result": r[14], "profit": float(r[15]) if r[15] is not None else None,
        "home": r[18], "away": r[19], "score": f"{r[20]}-{r[21]}" if r[20] is not None else None,
    } for r in rows]
    by = lambda key: {k: _summary([b for b in bets if key(b) == k]) for k in sorted({key(b) for b in bets})}
    summary = {
        "all": _summary(bets),
        "strategy": by(lambda b: b["strategy"]),
        "market": by(lambda b: b["market"]),
        "strategy_market": by(lambda b: f"{b['strategy']}|{b['market']}"),
        "league": by(lambda b: str(b["league"])),
    }
    last = max([r[16] for r in rows] + [r[17] for r in rows if r[17]], default=None)
    # No generation timestamp, so the file only changes (and gets committed) when bets do
    (out_dir / "bets.json").write_text(json.dumps({
        "last_change": last.isoformat() if last else None,
        "rules": {"min_edge": 0.03, "max_odds": 10.0, "stake": 1},
        "summary": summary, "bets": bets,
    }, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d paper bets", len(bets))


PLAYER_SEASONS = list(range(2026, 2020, -1))     # season ranks shown, newest first


def export_players(conn, out_dir=OUT_DIR):
    """Current player ranks, season ranks and each team's predicted XI for its next match
    (players.json). A season rank is the player's average rank across that season (after each
    match, weighted by minutes; player_season_ranks), blank if he didn't play in these leagues."""
    players = conn.execute(
        """select p.player_id, p.name, p.rank_position, p.current_rank, p.rank_minutes, x.team_id,
                  f.league_id, extract(year from age(p.birth_date))::int
           from players p
           join lateral (select fp.team_id, fp.fixture_id from fixture_players fp
                         where fp.player_id = p.player_id order by fp.fixture_id desc limit 1) x on true
           join fixtures f on f.fixture_id = x.fixture_id
           where p.current_rank is not null and p.rank_minutes >= 450
           order by p.current_rank desc""").fetchall()
    season_ranks = defaultdict(dict)
    for player, season, rank in conn.execute(
            "select player_id, season, season_rank from player_season_ranks where season = any(%s)",
            [PLAYER_SEASONS]):
        season_ranks[player][season] = float(rank)
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
    (out_dir / "players.json").write_text(json.dumps({
        "fields": ["id", "name", "position", "rank", "minutes", "team", "league", "seasons", "age"],
        "seasons": PLAYER_SEASONS,
        "players": [[r[0], r[1], r[2], float(r[3]), r[4], r[5], r[6],
                     [season_ranks[r[0]].get(y) for y in PLAYER_SEASONS], r[7]] for r in players],
        "next_xi": next_xi,
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
    ids = [r[0] for r in conn.execute(
        "select player_id from players where current_rank is not null and rank_minutes >= 450")]
    per_club = """sum(fp.minutes),
                  sum(h.rank_before * fp.minutes) / nullif(sum(fp.minutes) filter (where h.rank_before is not null), 0),
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
    spells = _club_spells(seasons + recent)
    team_ids = {x[0] for p in spells.values() for v in p.values() for x in v}
    names = dict(conn.execute("select team_id, name from teams where team_id = any(%s)", [list(team_ids)]))
    (out_dir / "player_seasons.json").write_text(json.dumps({
        "fields": ["team", "minutes", "club_rank", "rating", "goals", "assists"],
        "teams": {str(t): names.get(t) for t in team_ids},
        "born": {str(p): b.isoformat() for p, b in conn.execute(
            "select player_id, birth_date from players where player_id = any(%s) and birth_date is not null", [ids])},
        "players": {str(p): {str(k): v for k, v in d.items()} for p, d in spells.items()},
    }, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("Exported season detail for %d players", len(spells))


CLUB_ACTIVE_DAYS = 400


def export_clubs(conn, out_dir=OUT_DIR):
    """One small file per active club for its club page: docs/data/clubs/<team_id>.json.

    history: every match since 2020 as [date, rank after, opponent, home?, goals for, against,
    competition]; plus 12-month home/away goal averages. Loaded only when the page opens.
    """
    now = datetime.now(timezone.utc)
    active = {r[0] for r in conn.execute(
        """select team_id from team_rankings where last_match >= %s
           union select home_team_id from fixtures where status_short in ('NS','TBD') and kickoff > now()
           union select away_team_id from fixtures where status_short in ('NS','TBD') and kickoff > now()""",
        [now - timedelta(days=CLUB_ACTIVE_DAYS)])}
    history = {}
    for team, kickoff, rank_after, rank_before, opp, is_home, hg, ag, league in conn.execute(
            """select h.team_id, h.kickoff, h.rank_after, h.rank_before, h.opponent_id, h.is_home,
                      f.home_goals, f.away_goals, f.league_id
               from team_rank_history h join fixtures f using (fixture_id)
               where h.team_id = any(%s) order by h.team_id, h.match_no""", [list(active)]):
        rows = history.setdefault(team, {"start": round(rank_before), "matches": []})["matches"]
        gf, ga = (hg, ag) if is_home else (ag, hg)
        rows.append([kickoff.date().isoformat(), round(rank_after, 1), opp, 1 if is_home else 0, gf, ga, league])
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
                   "fields": ["date", "rank", "opponent", "home", "gf", "ga", "league"],
                   "matches": h["matches"], "goal_averages": stats.get(team),
                   "teams": {o: names.get(o) for o in opponents}}
        (club_dir / f"{team}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d club pages", len(active))
