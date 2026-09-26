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

Attack/defence, home edge and line-ups (ranking.side_ratings, player_ratings), on top of the above:
    exp_diff  += HOME_EDGE_WEIGHT x (home side's home edge + away side's edge) / 100
    exp_diff  += XI_LINE_WEIGHTS . (home predicted XI - away predicted XI), average rank by line
                 (GK, DEF, MID, FWD; only when both sides have all four lines)
    base goals: the total is a blend, AD_GOALS_WEIGHT of the attack/defence model's (the
                competition goal base + (split_home + split_away) / 100 for each side) and the rest
                the 12-month averages'; the home/away shape stays the 12-month one
    Tuned on 2023/24, tested on 2024/25 onwards (44,339 matches, injuries left out of both):
                   W/D/L log loss  over 2.5   BTTS     goals RMSE
    before         0.99847         0.67923    0.68747  1.1739
    with these     0.99734         0.67715    0.68678  1.1712
    Each part helped on its own (W/D/L: attack/defence 0.99822, home edge 0.99812, line-ups
    0.99795). The goalkeeper line made it worse, so its weight is 0.

The sheet averaged this with a "36% x strength ratio" rule; that made predictions worse, so
it's dropped. Log loss: sheet method 1.016, first version 1.0053, this 1.0035
(base-rate guessing ~1.07).
"""
import logging
import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from .health import monitored
from . import config, match_snapshots
from .cache import finished_fixtures, rank_history
from .injuries import BETA as INJURY_BETA, missing_strengths
from .ranking import DEFAULT_STARTING_RANK, HOME_ADVANTAGE_POINTS

log = logging.getLogger(__name__)

SHRINK_GAMES = 6          # weight of the league average, in matches
DRAW_INFLATION = 1.1      # max draw boost (close games); independent Poisson under-predicts draws
DRAW_FADE_MARGIN = 1.5    # no draw boost once the expected margin reaches this many goals
EUROPE_HOME_BONUS = 0.2   # extra expected home margin in the Champions/Europa/Conference League
EUROPE_COMPS = {2, 3, 848}
MATCH_RANK_NOW_TODAY = 0.6  # weight of the current rank for a match today; the rest is LT ALGO
MATCH_RANK_NOW_YEAR = 0.2   # ... for a match a year or more away (straight line in between)
AD_GOALS_WEIGHT = 0.75      # share of the base goal total from the attack/defence model
HOME_EDGE_WEIGHT = 1.0      # clubs' own home edges, in full
XI_LINE_WEIGHTS = (0.0, 0.005, 0.005, 0.005)   # goals of margin per point, GK / DEF / MID / FWD


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


# The other over/under lines, calibrated the same way: (base, shrink), fitted on 2023/24
# with the current projections (2.5 keeps OVER25_* above)
GOAL_LINE_CALIBRATION = {1.5: (0.76, 0.9), 3.5: (0.36, 0.9), 4.5: (0.04, 1.0)}
GOAL_LINES = (1.5, 2.5, 3.5, 4.5)


def goal_lines(home_xg, away_xg):
    """{line: P(over line)} for GOAL_LINES, calibrated. Worked out from the projected goals, so
    stored projections (fixture_predictions.home_xg / away_xg) give them back exactly."""
    ph, pa = _pmf(home_xg), _pmf(away_xg)
    total = sum(ph) * sum(pa)
    out = {2.5: goal_markets(home_xg, away_xg)[0]}
    for line, (base, shrink) in GOAL_LINE_CALIBRATION.items():
        under = sum(ph[i] * pa[j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1)
                    if i + j < line) / total
        out[line] = min(max(base + shrink * ((1 - under) - base), 0.005), 0.995)
    return out


def _shrunk(records, idx, league_avg):
    """Mean of records[*][idx], pulled toward league_avg by SHRINK_GAMES pseudo-matches."""
    total = sum(r[idx] for r in records)
    return (total + league_avg * SHRINK_GAMES) / (len(records) + SHRINK_GAMES)


def predict_match(h_rank, a_rank, home_records, away_records, lg_home, lg_away, league_id=None,
                  home_missing=0.0, away_missing=0.0, sides=None, lines=None):
    """(exp_diff, home_xg, away_xg, p_home, p_draw, p_away, likely_score, p_over25, p_btts).

    home_records: the home side's home games as (scored, conceded); away_records: the away
    side's away games as (scored, conceded); lg_*: competition average goals.
    sides: (split home, split away, home edge home, home edge away, goal base home, goal base
    away) going into the match, or None; lines: (home, away) predicted XI average rank by line
    [GK, DEF, MID, FWD], or None.
    """
    exp_diff = (h_rank - a_rank + HOME_ADVANTAGE_POINTS) / 100
    if league_id in EUROPE_COMPS:
        exp_diff += EUROPE_HOME_BONUS
    exp_diff += INJURY_BETA * (away_missing - home_missing)
    if sides:
        exp_diff += HOME_EDGE_WEIGHT * (sides[2] + sides[3]) / 100
    if lines and all(v is not None for side in lines for v in side):
        exp_diff += sum(w * (h - a) for w, h, a in zip(XI_LINE_WEIGHTS, *lines))
    base_home = (_shrunk(home_records, 0, lg_home) + _shrunk(away_records, 1, lg_home)) / 2
    base_away = (_shrunk(away_records, 0, lg_away) + _shrunk(home_records, 1, lg_away)) / 2
    if sides:
        # how open a game between these two is, from the attack/defence model: both sides'
        # expected goals at level ranks (the margin comes from exp_diff)
        level = (sides[0] + sides[1]) / 100
        ad_total = max(0.2, sides[4] + level) + max(0.2, sides[5] + level)
        total = base_home + base_away
        scale = ((1 - AD_GOALS_WEIGHT) * total + AD_GOALS_WEIGHT * ad_total) / total
        base_home, base_away = base_home * scale, base_away * scale
    home_xg, away_xg = project(base_home, base_away, exp_diff)
    return (exp_diff, home_xg, away_xg, *outcome_probabilities(home_xg, away_xg),
            *goal_markets(home_xg, away_xg))


def _predicted_lines(conn, fixture_ids):
    """{(fixture, team): [GK, DEF, MID, FWD] predicted XI average rank} for these fixtures."""
    return {(f, t): list(v) for f, t, *v in conn.execute(
        """select fixture_id, team_id, predicted_gk::float8, predicted_def::float8,
                  predicted_mid::float8, predicted_fwd::float8
           from fixture_team_ratings where fixture_id = any(%s)""", [list(fixture_ids)])}


def _pair(lines, fid, home, away):
    h, a = lines.get((fid, home)), lines.get((fid, away))
    return (h, a) if h and a else None


def _form(home_goals, away_goals, home_xg, away_xg):
    """(home, away) form values for one match: xG when recorded, otherwise goals."""
    return (home_goals if home_xg is None else home_xg, away_goals if away_xg is None else away_xg)


@monitored("predictions", conn_index=0)
def update_predictions(conn, fixture_ids=None):
    """Project every upcoming fixture, or only those in fixture_ids (the match-day job, which
    then downloads only the involved teams' and competitions' results)."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=365)
    upcoming = conn.execute(
        """select fixture_id, kickoff, league_id, home_team_id, away_team_id from fixtures
           where status_short = any(%s) and kickoff >= %s
             and (%s::int[] is null or fixture_id = any(%s::int[])) order by kickoff""",
        [list(UPCOMING_STATUSES), now - timedelta(hours=3), fixture_ids, fixture_ids]).fetchall()
    if not upcoming:
        log.info("Predictions: no upcoming fixtures to project")
        return
    teams = leagues = None
    if fixture_ids is not None:
        teams = list({t for f in upcoming for t in f[3:5]})
        leagues = list({f[2] for f in upcoming})

    rank_rows = conn.execute(
        "select team_id, current_rank, lt_algo, reliability, attack, home_rating from team_rankings").fetchall()
    ranks = {t: (cur, lt, rel) for t, cur, lt, rel, _, _ in rank_rows}
    # attack/defence split and home edge now (team_rankings stores them as attack and home)
    split = {t: ((att - cur) / 2, home_r - cur) for t, cur, _, _, att, home_r in rank_rows
             if att is not None and home_r is not None}
    bases = {lg: (bh, ba) for lg, bh, ba in conn.execute(
        "select league_id, goal_base_home, goal_base_away from leagues where goal_base_home is not null")}
    lines = _predicted_lines(conn, [f[0] for f in upcoming])
    starting = {k: float(v) for k, v in conn.execute(
        "select league_id, starting_rank from leagues where starting_rank is not null")}

    # Last 12 months of results: per-team home/away records (xG where available) and
    # per-competition average goals
    home_rec, away_rec = defaultdict(list), defaultdict(list)
    comp_goals = defaultdict(list)
    for league_id, home, away, hg, ag, hx, ax in conn.execute(
            """select f.league_id, f.home_team_id, f.away_team_id, f.home_goals, f.away_goals,
                      hx.expected_goals::float8, ax.expected_goals::float8
               from fixtures f
               left join fixture_team_stats hx on hx.fixture_id = f.fixture_id and hx.is_home
               left join fixture_team_stats ax on ax.fixture_id = f.fixture_id and not ax.is_home
               where f.status_short = any(%s) and f.home_goals is not null and f.kickoff >= %s
                 and (%s::int[] is null or f.league_id = any(%s::int[])
                      or f.home_team_id = any(%s::int[]) or f.away_team_id = any(%s::int[]))""",
            [list(config.FINISHED_STATUSES), since, leagues, leagues, teams, teams]):
        hf, af = _form(hg, ag, hx, ax)
        home_rec[home].append((hf, af))
        away_rec[away].append((af, hf))
        comp_goals[league_id].append((hg, ag))

    missing = missing_strengths(conn, [f[0] for f in upcoming])

    version_id = match_snapshots.register_version(conn)
    snapshots = []
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
        (sh, eh), (sa, ea) = split.get(home, (0.0, 0.0)), split.get(away, (0.0, 0.0))
        bh, ba = bases.get(league_id, (lg_home, lg_away))
        rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                     *predict_match(h_rank, a_rank, home_rec[home], away_rec[away], lg_home, lg_away,
                                    league_id, h_miss or 0.0, a_miss or 0.0, (sh, sa, eh, ea, bh, ba),
                                    _pair(lines, fid, home, away)),
                     h_rel, a_rel, h_miss, a_miss))
        snapshots.append(match_snapshots.make_snapshot(rows[-1], {
            'home_current_rank': h_cur, 'away_current_rank': a_cur,
            'home_lt_algo': h_lt, 'away_lt_algo': a_lt,
            'home_reliability': h_rel, 'away_reliability': a_rel,
            'fallback_starting_rank': default_rank,
            'home_rank_fallback': home not in ranks, 'away_rank_fallback': away not in ranks,
            'home_records': home_rec[home], 'away_records': away_rec[away],
            'league_home_goals': lg_home, 'league_away_goals': lg_away,
            'sides': [sh, sa, eh, ea, bh, ba],
            'predicted_lines': _pair(lines, fid, home, away),
            'home_missing': h_miss, 'away_missing': a_miss,
        }, version_id=version_id, captured_at=datetime.now(timezone.utc),
            reference_at=now, source='prospective'))


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
    match_snapshots.append_snapshots(conn, snapshots)
    conn.commit()
    log.info("Predictions: %d upcoming fixtures", len(rows))


BACKFILL_FROM = datetime(2021, 7, 1, tzinfo=timezone.utc)


def backfill_predictions(conn):
    """Reconstruct pre-match projections for finished fixtures that have none (source='backfill').

    Uses each team's rank before the match (team_rank_history.rank_before) and goal averages
    from the 12 months before kickoff, so it's what the current model would have said at the
    time. Live snapshots (source='live', made the night before) are never overwritten.
    """
    targets = {r[0] for r in conn.execute(
        """select f.fixture_id from fixtures f
           where f.status_short = any(%s) and f.home_goals is not null and f.kickoff >= %s
             and not exists (select 1 from fixture_predictions p where p.fixture_id = f.fixture_id)""",
        [list(config.FINISHED_STATUSES), BACKFILL_FROM])}
    if not targets:
        log.info("Backfilled predictions for 0 finished fixtures (none missing)")
        return
    # Match rank going into each fixture: Now and LT ALGO (lt_before) as they stood before it
    version_id = match_snapshots.register_version(conn)
    snapshots = []
    raw_ranks = {}
    ranks_before, sides_before = {}, {}
    for fid, _, _, _, is_home, _, before, _, lt, *_, s0, e0, gb in rank_history(conn):
        raw_ranks[(fid, is_home)] = (before, lt)
        ranks_before[(fid, is_home)] = match_rank(before, lt)
        sides_before[(fid, is_home)] = (s0, e0, gb)
    lines = _predicted_lines(conn, list(targets))
    fixtures = finished_fixtures(conn)

    window = timedelta(days=365)
    missing = missing_strengths(conn, targets)
    home_rec, away_rec, comp = defaultdict(deque), defaultdict(deque), defaultdict(deque)

    def trim(dq, now):
        while dq and dq[0][0] < now - window:
            dq.popleft()

    rows = []
    for fid, kickoff, league_id, _, home, away, hg, ag, _, hx, ax in fixtures:
        for dq in (home_rec[home], away_rec[away], comp[league_id]):
            trim(dq, kickoff)
        if fid in targets and (fid, True) in ranks_before and (fid, False) in ranks_before:
            games = comp[league_id]
            lg_home = sum(g[1] for g in games) / len(games) if games else DEFAULT_HOME_GOALS
            lg_away = sum(g[2] for g in games) / len(games) if games else DEFAULT_AWAY_GOALS
            h_rank, a_rank = ranks_before[(fid, True)], ranks_before[(fid, False)]
            h_miss, a_miss = missing.get((fid, home)), missing.get((fid, away))
            (sh, eh, bh), (sa, ea, ba) = sides_before[(fid, True)], sides_before[(fid, False)]
            sides = (sh, sa, eh, ea, bh, ba) if None not in (sh, sa, eh, ea, bh, ba) else None
            rows.append((fid, kickoff, league_id, home, away, h_rank, a_rank,
                         *predict_match(h_rank, a_rank, [r[1:] for r in home_rec[home]],
                                        [r[1:] for r in away_rec[away]], lg_home, lg_away,
                                        league_id, h_miss or 0.0, a_miss or 0.0, sides,
                                        _pair(lines, fid, home, away)),
                         h_miss, a_miss))
            snapshots.append(match_snapshots.make_snapshot(rows[-1], {
                'home_current_rank': raw_ranks[(fid, True)][0],
                'home_lt_algo': raw_ranks[(fid, True)][1],
                'away_current_rank': raw_ranks[(fid, False)][0],
                'away_lt_algo': raw_ranks[(fid, False)][1],
                'home_records': [r[1:] for r in home_rec[home]],
                'away_records': [r[1:] for r in away_rec[away]],
                'league_home_goals': lg_home, 'league_away_goals': lg_away,
                'sides': sides, 'predicted_lines': _pair(lines, fid, home, away),
                'home_missing': h_miss, 'away_missing': a_miss,
            }, version_id=version_id, captured_at=datetime.now(timezone.utc),
                reference_at=kickoff, source='reconstruction'))

        hf, af = _form(hg, ag, hx, ax)
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
    match_snapshots.append_snapshots(conn, snapshots)
    conn.commit()
    log.info("Backfilled predictions for %d finished fixtures", len(rows))
