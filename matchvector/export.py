"""Export compact JSON for the static website in docs/ (read by docs/index.html).

The nightly GitHub Action runs this after the sync and commits docs/data/ if it changed,
so the site never needs database credentials.
"""
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                      p.home_missing, p.away_missing
               from fixtures f left join fixture_predictions p using (fixture_id)
               where f.kickoff between %s and %s
               order by f.kickoff, f.fixture_id""",
            [now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)]):
        (fid, kickoff, lid, rnd, home, away, status, hg, ag, ph, pa_, p_h, p_d, p_a,
         hxg, axg, likely, hr, ar, source, *ratings, h_miss, a_miss) = row
        team_ids.update((home, away))
        matches.append([
            fid, kickoff.isoformat(), lid, rnd, home, away, status, hg, ag, ph, pa_,
            _r(p_h, 3), _r(p_d, 3), _r(p_a, 3), _r(hxg), _r(axg), likely, _r(hr, 0), _r(ar, 0),
            source, *ratings,
            *[_r(x, 3) for x in market.get(fid, (None, None, None))],
            _r(h_miss), _r(a_miss),
        ])

    # Form: total rank change over each team's last FORM_GAMES games
    form = dict(conn.execute(
        """select team_id, sum(rank_change) from (
             select team_id, rank_change,
                    row_number() over (partition by team_id order by match_no desc) rn
             from team_rank_history) x
           where rn <= %s group by team_id""", [FORM_GAMES]).fetchall())

    rankings = []
    for team, lid, cur, st, lt, rel, played, last in conn.execute(
            """select team_id, league_id, current_rank, st_algo, lt_algo, reliability, played,
                      last_match from team_rankings order by lt_algo desc"""):
        team_ids.add(team)
        rankings.append([team, lid, _r(cur, 1), _r(st, 1), _r(lt, 1), _r(rel, 0), played,
                         last.isoformat() if last else None, _r(form.get(team), 1)])

    teams = {t: n for t, n in conn.execute(
        "select team_id, name from teams where team_id = any(%s)", [list(team_ids)])}

    generated = now.isoformat()
    (out_dir / "matches.json").write_text(json.dumps({
        "generated_at": generated,
        "fields": ["id", "kickoff", "league", "round", "home", "away", "status", "hg", "ag",
                   "pen_h", "pen_a", "p_home", "p_draw", "p_away", "home_xg", "away_xg",
                   "likely", "home_rank", "away_rank", "source", "rating", "r_winner",
                   "r_margin", "r_clean_sheets", "r_shape", "r_goals", "m_home", "m_draw", "m_away",
                   "home_missing", "away_missing"],
        "matches": matches,
        "competitions": competitions,
        "teams": teams,
    }, separators=(",", ":")), encoding="utf-8")
    (out_dir / "rankings.json").write_text(json.dumps({
        "generated_at": generated,
        "fields": ["team", "league", "current", "st", "lt", "reliability", "played", "last_match",
                   "form"],
        "rankings": rankings,
    }, separators=(",", ":")), encoding="utf-8")
    log.info("Exported %d matches and %d rankings to %s", len(matches), len(rankings), out_dir)
    export_stats(conn, out_dir)


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
