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
from collections import defaultdict, deque
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


def _shrunk(records, idx, league_avg):
    """Mean of records[*][idx], pulled toward league_avg by SHRINK_GAMES pseudo-matches."""
    total = sum(r[idx] for r in records)
    return (total + league_avg * SHRINK_GAMES) / (len(records) + SHRINK_GAMES)


def predict_match(h_rank, a_rank, home_records, away_records, lg_home, lg_away):
    """(exp_diff, home_xg, away_xg, p_home, p_draw, p_away, likely_score) for one fixture.

    home_records: the home side's home games as (scored, conceded); away_records: the away
    side's away games as (scored, conceded); lg_*: competition average goals.
    """
    exp_diff = (h_rank - a_rank + HOME_ADVANTAGE_POINTS) / 100
    base_home = (_shrunk(home_records, 0, lg_home) + _shrunk(away_records, 1, lg_home)) / 2
    base_away = (_shrunk(away_records, 0, lg_away) + _shrunk(home_records, 1, lg_away)) / 2
    home_xg, away_xg = project(base_home, base_away, exp_diff)
    return (exp_diff, home_xg, away_xg, *outcome_probabilities(home_xg, away_xg))


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

        rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                     *predict_match(h_rank, a_rank, home_rec[home], away_rec[away], lg_home, lg_away),
                     h_rel, a_rel))

    # Upsert only upcoming fixtures: once a match kicks off its row is left alone, so it
    # keeps the last pre-kickoff projection for comparing with the result.
    with conn.cursor() as cur:
        cur.executemany(
            """insert into fixture_predictions (fixture_id, kickoff, league_id, home_team_id,
               away_team_id, home_rank, away_rank, exp_diff, home_xg, away_xg, p_home, p_draw,
               p_away, likely_score, home_reliability, away_reliability)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (fixture_id) do update set
                 kickoff = excluded.kickoff, league_id = excluded.league_id,
                 home_team_id = excluded.home_team_id, away_team_id = excluded.away_team_id,
                 home_rank = excluded.home_rank, away_rank = excluded.away_rank,
                 exp_diff = excluded.exp_diff, home_xg = excluded.home_xg,
                 away_xg = excluded.away_xg, p_home = excluded.p_home, p_draw = excluded.p_draw,
                 p_away = excluded.p_away, likely_score = excluded.likely_score,
                 home_reliability = excluded.home_reliability,
                 away_reliability = excluded.away_reliability, updated_at = now()""", rows)
    conn.commit()
    log.info("Predictions: %d upcoming fixtures", len(rows))


BACKFILL_FROM = datetime(2023, 7, 1, tzinfo=timezone.utc)


def backfill_predictions(conn):
    """Reconstruct pre-match projections for finished fixtures that have none (source='backfill').

    Uses each team's rank before the match (team_rank_history.rank_before) and goal averages
    from the 12 months before kickoff, so it's what the current model would have said at the
    time. Live snapshots (source='live', made the night before) are never overwritten.
    """
    have = {r[0] for r in conn.execute("select fixture_id from fixture_predictions")}
    ranks_before = {}
    for fid, team, is_home, rank in conn.execute(
            "select fixture_id, team_id, is_home, rank_before from team_rank_history"):
        ranks_before[(fid, is_home)] = rank

    fixtures = conn.execute(
        """select fixture_id, kickoff, league_id, home_team_id, away_team_id, home_goals, away_goals
           from fixtures where status_short = any(%s) and home_goals is not null
           order by kickoff, fixture_id""", [list(config.FINISHED_STATUSES)]).fetchall()

    window = timedelta(days=365)
    home_rec, away_rec, comp = defaultdict(deque), defaultdict(deque), defaultdict(deque)

    def trim(dq, now):
        while dq and dq[0][0] < now - window:
            dq.popleft()

    rows = []
    for fid, kickoff, league_id, home, away, hg, ag in fixtures:
        for dq in (home_rec[home], away_rec[away], comp[league_id]):
            trim(dq, kickoff)
        if kickoff >= BACKFILL_FROM and fid not in have \
                and (fid, True) in ranks_before and (fid, False) in ranks_before:
            games = comp[league_id]
            lg_home = sum(g[1] for g in games) / len(games) if games else DEFAULT_HOME_GOALS
            lg_away = sum(g[2] for g in games) / len(games) if games else DEFAULT_AWAY_GOALS
            h_rank, a_rank = ranks_before[(fid, True)], ranks_before[(fid, False)]
            rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                         *predict_match(h_rank, a_rank, [r[1:] for r in home_rec[home]],
                                        [r[1:] for r in away_rec[away]], lg_home, lg_away)))
        home_rec[home].append((kickoff, hg, ag))
        away_rec[away].append((kickoff, ag, hg))
        comp[league_id].append((kickoff, hg, ag))

    with conn.cursor() as cur:
        cur.executemany(
            """insert into fixture_predictions (fixture_id, kickoff, league_id, home_team_id,
               away_team_id, home_rank, away_rank, exp_diff, home_xg, away_xg, p_home, p_draw,
               p_away, likely_score, source)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'backfill')
               on conflict (fixture_id) do nothing""", rows)
    conn.commit()
    log.info("Backfilled predictions for %d finished fixtures", len(rows))
