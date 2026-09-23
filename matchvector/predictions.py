"""Projected scores and win/draw/loss chances for upcoming fixtures.

Based on the Club Ranking sheet's RG tabs, improved by backtesting 51,000 matches (2024-26):

    exp_diff   = (home_rank - away_rank + HOME_ADVANTAGE_POINTS) / 100   (as in ranking.py)
    base_home  = mean(home team's avg goals scored at home, away team's avg conceded away)
    base_away  = mean(away team's avg goals scored away, home team's avg conceded at home)
                 each average covers the last 12 months, shrunk toward the league average
                 by SHRINK_GAMES so teams with few games aren't extreme
    buff       = (exp_diff - (base_home - base_away)) / 2     (the sheet's "Buff")
    home_xg    = base_home + buff;  away_xg = base_away - buff (so the margin = exp_diff)
    P(score)   = Poisson(home_xg) x Poisson(away_xg), 0-10 goals each
    draw       = P(draw) x DRAW_INFLATION, win/loss rescaled to fill the rest

The sheet averaged this with a "36% x strength ratio" rule; that made predictions worse, so
it's dropped. Log loss: sheet method 1.016, this 1.005 (base-rate guessing ~1.07).
"""
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config
from .ranking import DEFAULT_STARTING_RANK, HOME_ADVANTAGE_POINTS

log = logging.getLogger(__name__)

SHRINK_GAMES = 6          # weight of the league average, in matches
DRAW_INFLATION = 1.1      # independent Poisson slightly under-predicts draws
MAX_GOALS = 10
DEFAULT_HOME_GOALS, DEFAULT_AWAY_GOALS = 1.45, 1.15
UPCOMING_STATUSES = ("NS", "TBD")


def _pmf(lam):
    lam = max(lam, 0.01)
    return [math.exp(-lam) * lam ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]


def project(base_home, base_away, exp_diff):
    """Projected goals for each side, keeping total goals and matching the expected margin."""
    buff = (exp_diff - (base_home - base_away)) / 2
    home, away = base_home + buff, base_away - buff
    if away < 0:
        home, away = home - away, 0.0
    if home < 0:
        home, away = 0.0, away - home
    return home, away


def outcome_probabilities(home_xg, away_xg):
    """(p_home, p_draw, p_away, most likely score) from two Poisson goal rates."""
    ph, pa = _pmf(home_xg), _pmf(away_xg)
    grid = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    total = sum(map(sum, grid))
    home = sum(grid[i][j] for i in range(MAX_GOALS + 1) for j in range(i)) / total
    draw = sum(grid[i][i] for i in range(MAX_GOALS + 1)) / total
    away = 1 - home - draw
    likely = max(((i, j) for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1)),
                 key=lambda s: grid[s[0]][s[1]])
    draw_adj = min(draw * DRAW_INFLATION, 0.9)
    scale = (1 - draw_adj) / (home + away)
    return home * scale, draw_adj, away * scale, f"{likely[0]}-{likely[1]}"


def update_predictions(conn):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=365)

    ranks = {t: (r, rel) for t, r, rel in conn.execute(
        "select team_id, current_rank, reliability from team_rankings")}
    starting = {k: float(v) for k, v in conn.execute(
        "select league_id, starting_rank from leagues where starting_rank is not null")}

    # Last 12 months of results: per-team home/away records and per-competition averages
    home_rec, away_rec = defaultdict(list), defaultdict(list)
    comp_goals = defaultdict(list)
    for league_id, home, away, hg, ag in conn.execute(
            """select league_id, home_team_id, away_team_id, home_goals, away_goals from fixtures
               where status_short = any(%s) and home_goals is not null and kickoff >= %s""",
            [list(config.FINISHED_STATUSES), since]):
        home_rec[home].append((hg, ag))
        away_rec[away].append((ag, hg))
        comp_goals[league_id].append((hg, ag))

    def shrunk(records, idx, league_avg):
        n = len(records)
        total = sum(r[idx] for r in records)
        return (total + league_avg * SHRINK_GAMES) / (n + SHRINK_GAMES)

    upcoming = conn.execute(
        """select fixture_id, kickoff, league_id, home_team_id, away_team_id from fixtures
           where status_short = any(%s) and kickoff >= %s order by kickoff""",
        [list(UPCOMING_STATUSES), now - timedelta(hours=3)]).fetchall()

    rows = []
    for fid, kickoff, league_id, home, away in upcoming:
        games = comp_goals.get(league_id)
        lg_home = sum(g[0] for g in games) / len(games) if games else DEFAULT_HOME_GOALS
        lg_away = sum(g[1] for g in games) / len(games) if games else DEFAULT_AWAY_GOALS
        default_rank = starting.get(league_id, DEFAULT_STARTING_RANK)
        h_rank, h_rel = ranks.get(home, (default_rank, 0.0))
        a_rank, a_rel = ranks.get(away, (default_rank, 0.0))

        exp_diff = (h_rank - a_rank + HOME_ADVANTAGE_POINTS) / 100
        base_home = (shrunk(home_rec[home], 0, lg_home) + shrunk(away_rec[away], 1, lg_home)) / 2
        base_away = (shrunk(away_rec[away], 0, lg_away) + shrunk(home_rec[home], 1, lg_away)) / 2
        home_xg, away_xg = project(base_home, base_away, exp_diff)
        p_home, p_draw, p_away, likely = outcome_probabilities(home_xg, away_xg)
        rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank, exp_diff,
                     home_xg, away_xg, p_home, p_draw, p_away, likely, h_rel, a_rel))

    with conn.cursor() as cur:
        cur.execute("truncate fixture_predictions")
        cur.executemany(
            """insert into fixture_predictions (fixture_id, kickoff, league_id, home_team_id,
               away_team_id, home_rank, away_rank, exp_diff, home_xg, away_xg, p_home, p_draw,
               p_away, likely_score, home_reliability, away_reliability)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", rows)
    conn.commit()
    log.info("Predictions: %d upcoming fixtures", len(rows))
