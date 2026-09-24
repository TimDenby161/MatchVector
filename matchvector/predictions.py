"""Projected scores and win/draw/loss chances for upcoming fixtures.

Based on the Club Ranking sheet's RG tabs, improved by backtesting 51,000 matches (2024-26):

    match rank = w x Now (current rank) + (1 - w) x LT ALGO, where w slides with how far
                 away the match is: MATCH_RANK_NOW_TODAY on the day down to MATCH_RANK_NOW_YEAR
                 a year or more out (backtested with ranks as they stood 0/91/182/365 days
                 before kickoff: the best w was 0.6 / 0.4 / 0.4 / 0.2; Now alone was worse at
                 every horizon)
    exp_diff   = (home match rank - away match rank + HOME_ADVANTAGE_POINTS) / 100
                 + EUROPE_HOME_BONUS in UEFA club competitions (home sides do ~0.2 goals better)
    base_home  = mean(home team's avg goals scored at home, away team's avg conceded away)
    base_away  = mean(away team's avg goals scored away, home team's avg conceded at home)
                 each average covers the last 12 months, uses xG instead of goals for any
                 match that has it, and is shrunk toward the competition average by SHRINK_GAMES
    home_xg    = base_home * x;  away_xg = base_away / x, with x chosen so home_xg - away_xg =
                 exp_diff (a proportional version of the sheet's "Buff", which moved goals in a
                 straight line and pushed underdogs to ~0 goals)
                 In config.INJURY_MODEL_LEAGUES the margin also shifts by injuries.BETA x
                 (away missing strength - home missing strength), from the injury lists
    P(score)   = Poisson(home_xg) x Poisson(away_xg), 0-10 goals each
    draw       = P(draw) boosted by up to DRAW_INFLATION in close games (none past a 1.5 goal
                 margin), win/loss rescaled to fill the rest

The sheet averaged this with a "36% x strength ratio" rule; that made predictions worse, so
it's dropped. Log loss: sheet method 1.016, first version 1.0053, this 1.0035
(base-rate guessing ~1.07).
"""
import logging
import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import config
from .injuries import BETA as INJURY_BETA, missing_strengths
from .ranking import DEFAULT_STARTING_RANK, HOME_ADVANTAGE_POINTS, summarise

log = logging.getLogger(__name__)

SHRINK_GAMES = 6          # weight of the league average, in matches
DRAW_INFLATION = 1.1      # max draw boost (close games); independent Poisson under-predicts draws
DRAW_FADE_MARGIN = 1.5    # no draw boost once the expected margin reaches this many goals
EUROPE_HOME_BONUS = 0.2   # extra expected home margin in the Champions/Europa/Conference League
EUROPE_COMPS = {2, 3, 848}
MATCH_RANK_NOW_TODAY = 0.6  # weight of the current rank for a match today; the rest is LT ALGO
MATCH_RANK_NOW_YEAR = 0.2   # ... for a match a year or more away (straight line in between)


def match_rank(now, lt, days_ahead=0.0):
    """Rank used for projections: the current rank blended with LT ALGO, leaning more on LT
    the further away the match is."""
    frac = min(max(days_ahead, 0.0) / 365, 1.0)
    w = MATCH_RANK_NOW_TODAY + (MATCH_RANK_NOW_YEAR - MATCH_RANK_NOW_TODAY) * frac
    return w * now + (1 - w) * (lt if lt is not None else now)
MAX_GOALS = 10
DEFAULT_HOME_GOALS, DEFAULT_AWAY_GOALS = 1.45, 1.15
UPCOMING_STATUSES = ("NS", "TBD")

# Goal markets from the same Poisson grid, pulled toward the base rate (fitted on 2021-23,
# test log loss: over 2.5 0.6805 -> 0.6801, both teams score 0.6887 -> 0.6876)
OVER25_BASE, OVER25_SHRINK = 0.514, 0.85
BTTS_BASE, BTTS_SHRINK = 0.519, 0.60


def _pmf(lam):
    lam = max(lam, 0.01)
    return [math.exp(-lam) * lam ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]


def project(base_home, base_away, exp_diff):
    """Projected goals: scale home up and away down by the same factor x until
    home - away = exp_diff (solves base_home*x - base_away/x = exp_diff)."""
    base_home, base_away = max(base_home, 0.05), max(base_away, 0.05)
    x = (exp_diff + math.sqrt(exp_diff ** 2 + 4 * base_home * base_away)) / (2 * base_home)
    return base_home * x, base_away / x


def outcome_probabilities(home_xg, away_xg):
    """(p_home, p_draw, p_away, most likely score) from two Poisson goal rates."""
    exp_diff = home_xg - away_xg
    ph, pa = _pmf(home_xg), _pmf(away_xg)
    grid = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    total = sum(map(sum, grid))
    home = sum(grid[i][j] for i in range(MAX_GOALS + 1) for j in range(i)) / total
    draw = sum(grid[i][i] for i in range(MAX_GOALS + 1)) / total
    away = 1 - home - draw
    likely = max(((i, j) for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1)),
                 key=lambda s: grid[s[0]][s[1]])
    boost = 1 + (DRAW_INFLATION - 1) * max(0.0, 1 - abs(exp_diff) / DRAW_FADE_MARGIN)
    draw_adj = min(draw * boost, 0.9)
    scale = (1 - draw_adj) / (home + away)
    return home * scale, draw_adj, away * scale, f"{likely[0]}-{likely[1]}"


def goal_markets(home_xg, away_xg):
    """(P(over 2.5 goals), P(both teams score)), calibrated."""
    ph, pa = _pmf(home_xg), _pmf(away_xg)
    total = sum(ph) * sum(pa)
    under = sum(ph[i] * pa[j] for i in range(3) for j in range(3 - i)) / total
    btts = (1 - ph[0] / sum(ph)) * (1 - pa[0] / sum(pa))
    return (OVER25_BASE + OVER25_SHRINK * ((1 - under) - OVER25_BASE),
            BTTS_BASE + BTTS_SHRINK * (btts - BTTS_BASE))


def _shrunk(records, idx, league_avg):
    """Mean of records[*][idx], pulled toward league_avg by SHRINK_GAMES pseudo-matches."""
    total = sum(r[idx] for r in records)
    return (total + league_avg * SHRINK_GAMES) / (len(records) + SHRINK_GAMES)


def predict_match(h_rank, a_rank, home_records, away_records, lg_home, lg_away, league_id=None,
                  home_missing=0.0, away_missing=0.0):
    """(exp_diff, home_xg, away_xg, p_home, p_draw, p_away, likely_score, p_over25, p_btts).

    home_records: the home side's home games as (scored, conceded); away_records: the away
    side's away games as (scored, conceded); lg_*: competition average goals.
    """
    exp_diff = (h_rank - a_rank + HOME_ADVANTAGE_POINTS) / 100
    if league_id in EUROPE_COMPS:
        exp_diff += EUROPE_HOME_BONUS
    exp_diff += INJURY_BETA * (away_missing - home_missing)
    base_home = (_shrunk(home_records, 0, lg_home) + _shrunk(away_records, 1, lg_home)) / 2
    base_away = (_shrunk(away_records, 0, lg_away) + _shrunk(home_records, 1, lg_away)) / 2
    home_xg, away_xg = project(base_home, base_away, exp_diff)
    return (exp_diff, home_xg, away_xg, *outcome_probabilities(home_xg, away_xg),
            *goal_markets(home_xg, away_xg))


def _load_xg(conn, since=None):
    """{(fixture_id, is_home): expected goals} from the match statistics."""
    sql = """select s.fixture_id, s.is_home, s.expected_goals from fixture_team_stats s
             join fixtures f using (fixture_id) where s.expected_goals is not null"""
    params = []
    if since is not None:
        sql += " and f.kickoff >= %s"
        params = [since]
    return {(fid, is_home): float(v) for fid, is_home, v in conn.execute(sql, params)}


def _form(fixture_id, home_goals, away_goals, xg):
    """(home, away) form values for one match: xG when recorded, otherwise goals."""
    return (xg.get((fixture_id, True), home_goals), xg.get((fixture_id, False), away_goals))


def update_predictions(conn):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=365)

    ranks = {t: (cur, lt, rel) for t, cur, lt, rel in conn.execute(
        "select team_id, current_rank, lt_algo, reliability from team_rankings")}
    starting = {k: float(v) for k, v in conn.execute(
        "select league_id, starting_rank from leagues where starting_rank is not null")}

    # Last 12 months of results: per-team home/away records (xG where available) and
    # per-competition average goals
    xg = _load_xg(conn, since)
    home_rec, away_rec = defaultdict(list), defaultdict(list)
    comp_goals = defaultdict(list)
    for fid, league_id, home, away, hg, ag in conn.execute(
            """select fixture_id, league_id, home_team_id, away_team_id, home_goals, away_goals
               from fixtures
               where status_short = any(%s) and home_goals is not null and kickoff >= %s""",
            [list(config.FINISHED_STATUSES), since]):
        hf, af = _form(fid, hg, ag, xg)
        home_rec[home].append((hf, af))
        away_rec[away].append((af, hf))
        comp_goals[league_id].append((hg, ag))

    upcoming = conn.execute(
        """select fixture_id, kickoff, league_id, home_team_id, away_team_id from fixtures
           where status_short = any(%s) and kickoff >= %s order by kickoff""",
        [list(UPCOMING_STATUSES), now - timedelta(hours=3)]).fetchall()
    missing = missing_strengths(conn, [f[0] for f in upcoming])

    rows = []
    for fid, kickoff, league_id, home, away in upcoming:
        games = comp_goals.get(league_id)
        lg_home = sum(g[0] for g in games) / len(games) if games else DEFAULT_HOME_GOALS
        lg_away = sum(g[1] for g in games) / len(games) if games else DEFAULT_AWAY_GOALS
        default_rank = starting.get(league_id, DEFAULT_STARTING_RANK)
        days_ahead = (kickoff - now).total_seconds() / 86400
        h_cur, h_lt, h_rel = ranks.get(home, (default_rank, None, 0.0))
        a_cur, a_lt, a_rel = ranks.get(away, (default_rank, None, 0.0))
        h_rank = match_rank(h_cur, h_lt, days_ahead)
        a_rank = match_rank(a_cur, a_lt, days_ahead)

        h_miss, a_miss = missing.get((fid, home)), missing.get((fid, away))
        rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                     *predict_match(h_rank, a_rank, home_rec[home], away_rec[away], lg_home, lg_away,
                                    league_id, h_miss or 0.0, a_miss or 0.0),
                     h_rel, a_rel, h_miss, a_miss))

    # Upsert only upcoming fixtures: once a match kicks off its row is left alone, so it
    # keeps the last pre-kickoff projection for comparing with the result.
    with conn.cursor() as cur:
        cur.executemany(
            """insert into fixture_predictions (fixture_id, kickoff, league_id, home_team_id,
               away_team_id, home_rank, away_rank, exp_diff, home_xg, away_xg, p_home, p_draw,
               p_away, likely_score, p_over25, p_btts, home_reliability, away_reliability,
               home_missing, away_missing)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (fixture_id) do update set
                 kickoff = excluded.kickoff, league_id = excluded.league_id,
                 home_team_id = excluded.home_team_id, away_team_id = excluded.away_team_id,
                 home_rank = excluded.home_rank, away_rank = excluded.away_rank,
                 exp_diff = excluded.exp_diff, home_xg = excluded.home_xg,
                 away_xg = excluded.away_xg, p_home = excluded.p_home, p_draw = excluded.p_draw,
                 p_away = excluded.p_away, likely_score = excluded.likely_score,
                 p_over25 = excluded.p_over25, p_btts = excluded.p_btts,
                 home_reliability = excluded.home_reliability,
                 away_reliability = excluded.away_reliability,
                 home_missing = excluded.home_missing, away_missing = excluded.away_missing,
                 updated_at = now()""", rows)
    conn.commit()
    log.info("Predictions: %d upcoming fixtures", len(rows))


BACKFILL_FROM = datetime(2021, 7, 1, tzinfo=timezone.utc)


def backfill_predictions(conn):
    """Reconstruct pre-match projections for finished fixtures that have none (source='backfill').

    Uses each team's rank before the match (team_rank_history.rank_before) and goal averages
    from the 12 months before kickoff, so it's what the current model would have said at the
    time. Live snapshots (source='live', made the night before) are never overwritten.
    """
    have = {r[0] for r in conn.execute("select fixture_id from fixture_predictions")}
    # Match rank going into each fixture: Now and LT ALGO as they stood before it
    history, ranks_before = {}, {}
    for fid, team, is_home, before, after in conn.execute(
            """select fixture_id, team_id, is_home, rank_before, rank_after from team_rank_history
               order by team_id, match_no"""):
        hist = history.setdefault(team, [before])
        s = summarise(hist)
        ranks_before[(fid, is_home)] = match_rank(before, s["lt_algo"] if s else before)
        hist.append(after)

    fixtures = conn.execute(
        """select fixture_id, kickoff, league_id, home_team_id, away_team_id, home_goals, away_goals
           from fixtures where status_short = any(%s) and home_goals is not null
           order by kickoff, fixture_id""", [list(config.FINISHED_STATUSES)]).fetchall()

    window = timedelta(days=365)
    xg = _load_xg(conn)
    missing = missing_strengths(conn, [f[0] for f in fixtures
                                       if f[1] >= BACKFILL_FROM and f[0] not in have])
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
            h_miss, a_miss = missing.get((fid, home)), missing.get((fid, away))
            rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                         *predict_match(h_rank, a_rank, [r[1:] for r in home_rec[home]],
                                        [r[1:] for r in away_rec[away]], lg_home, lg_away,
                                        league_id, h_miss or 0.0, a_miss or 0.0),
                         h_miss, a_miss))
        hf, af = _form(fid, hg, ag, xg)
        home_rec[home].append((kickoff, hf, af))
        away_rec[away].append((kickoff, af, hf))
        comp[league_id].append((kickoff, hg, ag))

    with conn.cursor() as cur:
        cur.executemany(
            """insert into fixture_predictions (fixture_id, kickoff, league_id, home_team_id,
               away_team_id, home_rank, away_rank, exp_diff, home_xg, away_xg, p_home, p_draw,
               p_away, likely_score, p_over25, p_btts, home_missing, away_missing, source)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'backfill')
               on conflict (fixture_id) do nothing""", rows)
    conn.commit()
    log.info("Backfilled predictions for %d finished fixtures", len(rows))
