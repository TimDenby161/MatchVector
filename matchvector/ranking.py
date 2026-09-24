"""Club ranking, based on the "Club Ranking" Google Sheet.

Per match:
    exp_diff    = (home_rank - away_rank + HOME_ADVANTAGE_POINTS) / 100
    act_diff    = home_goals - away_goals, capped at +/- MAX_GOAL_DIFF
                  when both sides have xG: GOALS_WEIGHT * that + (1 - GOALS_WEIGHT) * (home xG - away xG)
    rank_change = (act_diff - exp_diff) * K * weight
                  K: K_FACTOR, or K_FACTOR_XG for matches with xG
                  weight: COMPETITION_WEIGHT (1/3 for the Community Shield and UEFA Super Cup, else 1)
    home_rank  += rank_change;  away_rank -= rank_change

Differences from the sheet (exp_diff = (home*1.09 - away)/100, K = 10, no cap), chosen by
backtesting 2023-26 predictions: the x1.09 multiplier gave 0.4-1.1 goals of home advantage
(real: ~0.3 for everyone), and a smaller K plus a goal cap stops one freak result or cup
thrashing from swinging a rank. Prediction error fell from 1.77 to 1.68 goals per match.

Blending in xG (backtest of the full prediction pipeline, 2024/25 onwards): log loss 1.00046 ->
0.99834 overall and 1.00446 -> 0.99993 on matches with xG; better in each season from 2023/24.
30% goals / 70% xG beat xG alone (0.99889) and goals alone. xG differences are far less noisy
than goal differences, so those matches take a bigger K. Capping the xG difference made it worse.

Every team starts from a Starting Rank: leagues.starting_rank of the first
league (type 'League') it plays in, else that of the first competition it
appears in (cups), else DEFAULT_STARTING_RANK.

Summary figures (Ranking tab), where history = [starting rank, rank after each match]:
    rank_30  = mean of the last 30 history values
    rank_100 = mean of the last 100 history values
    st_algo  = 0.6*current + 0.2*mean(last 3) + 0.1*rank_30 + 0.1*rank_100
    lt_algo  = 0.1*st_algo + 0.3*rank_30 + 0.6*rank_100

Reliability (0-100, not in the sheet):
    games_factor     = 1 - exp(-played / 35)       (66% after 38 games, 89% after 76)
    rank_volatility  = standard deviation of the last 30 per-match rank changes, in K_FACTOR units
                       (a match with xG counts its change x K_FACTOR / K_FACTOR_XG)
    stability_factor = min(1, (10.5 / rank_volatility) ** 3)   (1 if under 10 games)
    reliability      = 100 * games_factor * stability_factor

Attack / defence and home / away (side_ratings, alongside the rank; they don't change it):
    attack  = rank + 2s,  defence = rank - 2s   (rank = their average; same scale as the rank)
    s is how much of a club's strength is scoring rather than stopping goals. Expected goals:
        home = base_home + ((home attack - away defence)/2 + HOME_ADVANTAGE_POINTS/2) / 100
        away = base_away + ((away attack - home defence)/2 - HOME_ADVANTAGE_POINTS/2) / 100
    i.e. 200 points of attack over the opponent's defence is one more goal
    (base: the competition's running average, shrunk to 1.45 / 1.15 over LEAGUE_GOALS_PRIOR games),
    so two attack-minded sides mean more goals. After the match both sides'
    s += ATTACK_K * (actual total goals - expected total) / 2, with goals capped at GOAL_CAP a side
    and blended with xG like the rank. Backtest, 2024/25 onwards: total-goals error 1.4205 -> 1.4083.
    home = rank + e,  away = rank - e: e is a club's own home edge on top of HOME_ADVANTAGE_POINTS.
    Both sides' e += HOME_EDGE_K * (result - exp_diff), so a club doing better at home than away
    builds a positive edge. Backtest: goal-difference error 1.3220 -> 1.3205 (small).
"""
import io
import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date

from . import config
from .cache import finished_fixtures

log = logging.getLogger(__name__)

HOME_ADVANTAGE_POINTS = 30   # = 0.3 goals, the same for every team
K_FACTOR = 6
MAX_GOAL_DIFF = 3            # a 7-0 counts as 3-0
GOALS_WEIGHT = 0.3           # in matches with xG: 30% capped goal difference, 70% xG difference
K_FACTOR_XG = 10             # K for matches with xG (less noisy, so ranks can move further)
DEFAULT_STARTING_RANK = 650
ATTACK_K = 0.75              # attack/defence split learning rate (0.5-1 scored about the same)
GOAL_CAP = 5                 # goals a side counted for the split
LEAGUE_GOALS_PRIOR = 50      # games of the 1.45 / 1.15 prior in a competition's goal averages
HOME_EDGE_K = 0.2            # club home edge learning rate (0.1-0.3 scored about the same)
# One-off curtain-raisers count for less than a league or cup match: their rank change is
# scaled by this. Sides rest players and treat them as pre-season, so a result says less.
# 1/3 is World Football Elo's friendly-to-World-Cup ratio (K 20 against 60). Too few of
# these games are played (two or three a year) to backtest a value.
COMPETITION_WEIGHT = {
    528: 1 / 3,     # Community Shield
    531: 1 / 3,     # UEFA Super Cup
}

# Reliability score tuning
GAMES_SCALE = 35            # games for the games factor to reach ~63%
VOLATILITY_WINDOW = 30      # recent matches used to measure rank swings
VOLATILITY_THRESHOLD = 10.5 # ~78th percentile; only teams swinging more than this lose points
VOLATILITY_POWER = 3        # how steeply the score falls above the threshold
MIN_VOLATILITY_GAMES = 10


@dataclass
class Match:
    key: object          # fixture id (or sheet row)
    home: object
    away: object
    home_goals: int
    away_goals: int
    home_xg: float = None
    away_xg: float = None
    weight: float = 1.0  # COMPETITION_WEIGHT of its competition
    league: int = None


def actual_diff(m):
    """(result used for the rank change, K): capped goal difference, blended with the xG
    difference when both sides have xG."""
    capped = max(-MAX_GOAL_DIFF, min(MAX_GOAL_DIFF, m.home_goals - m.away_goals))
    if m.home_xg is None or m.away_xg is None:
        return capped, K_FACTOR
    return GOALS_WEIGHT * capped + (1 - GOALS_WEIGHT) * (m.home_xg - m.away_xg), K_FACTOR_XG


def run(matches, starting_rank):
    """Replay matches in the given order.

    starting_rank(team) -> float. Returns (per-match rows, {team: history list}).
    """
    current, history, rows = {}, {}, []
    for m in matches:
        for team in (m.home, m.away):
            if team not in current:
                current[team] = starting_rank(team)
                history[team] = [current[team]]
        h, a = current[m.home], current[m.away]
        exp_diff = (h - a + HOME_ADVANTAGE_POINTS) / 100
        act_diff = m.home_goals - m.away_goals
        result, k = actual_diff(m)
        change = (result - exp_diff) * k * m.weight
        current[m.home], current[m.away] = h + change, a - change
        history[m.home].append(current[m.home])
        history[m.away].append(current[m.away])
        rows.append((m, h, a, exp_diff, act_diff, change))
    return rows, history


def side_ratings(rows):
    """Attack/defence split (s) and home edge (e) after each match of run()'s rows (see the
    module docstring). Returns [(s_home, s_away, e_home, e_away) after each match]."""
    s, e, goals = {}, {}, {}
    out = []
    capped = lambda g, x: min(g, GOAL_CAP) if x is None else         GOALS_WEIGHT * min(g, GOAL_CAP) + (1 - GOALS_WEIGHT) * x
    for m, h, a, exp_diff, _, _ in rows:
        sh, sa = s.get(m.home, 0.0), s.get(m.away, 0.0)
        gh, ga, n = goals.get(m.league, (0.0, 0.0, 0))
        base_h = (gh + 1.45 * LEAGUE_GOALS_PRIOR) / (n + LEAGUE_GOALS_PRIOR)
        base_a = (ga + 1.15 * LEAGUE_GOALS_PRIOR) / (n + LEAGUE_GOALS_PRIOR)
        mu_h = max(0.1, base_h + ((h - a) / 2 + sh + sa + HOME_ADVANTAGE_POINTS / 2) / 100)
        mu_a = max(0.1, base_a + ((a - h) / 2 + sh + sa - HOME_ADVANTAGE_POINTS / 2) / 100)
        yh, ya = capped(m.home_goals, m.home_xg), capped(m.away_goals, m.away_xg)
        step = ATTACK_K * ((yh + ya) - (mu_h + mu_a)) / 2 * m.weight
        s[m.home], s[m.away] = sh + step, sa + step
        edge = HOME_EDGE_K * (actual_diff(m)[0] - exp_diff - (e.get(m.home, 0.0) + e.get(m.away, 0.0)) / 100) * m.weight
        e[m.home], e[m.away] = e.get(m.home, 0.0) + edge, e.get(m.away, 0.0) + edge
        goals[m.league] = (gh + yh, ga + ya, n + 1)
        out.append((s[m.home], s[m.away], e[m.home], e[m.away]))
    return out


def sides_of(rank, s, e):
    """(attack, defence, home, away) for a rank, its split s and home edge e."""
    return rank + 2 * s, rank - 2 * s, rank + e, rank - e


def summarise(history, changes=None):
    """Ranking-tab figures for one team's history. None if no matches played.

    changes: per-match rank changes for the volatility, in K_FACTOR units (see _replay);
    defaults to the raw differences of history."""
    played = len(history) - 1
    if played == 0:
        return None
    mean = lambda xs: sum(xs) / len(xs)
    rank_30, rank_100 = mean(history[-30:]), mean(history[-100:])
    st = 0.6 * history[-1] + 0.2 * mean(history[-3:]) + 0.1 * rank_30 + 0.1 * rank_100
    lt = 0.1 * st + 0.3 * rank_30 + 0.6 * rank_100
    if changes is None:
        changes = [b - a for a, b in zip(history, history[1:])]
    changes = changes[-VOLATILITY_WINDOW:]
    volatility = statistics.stdev(changes) if len(changes) >= 2 else None
    games_factor = 1 - math.exp(-played / GAMES_SCALE)
    stability = 1.0
    if played >= MIN_VOLATILITY_GAMES and volatility:
        stability = min(1.0, (VOLATILITY_THRESHOLD / volatility) ** VOLATILITY_POWER)
    return {"played": played, "current_rank": history[-1], "rank_30": rank_30,
            "rank_100": rank_100, "st_algo": st, "lt_algo": lt,
            "rank_volatility": volatility, "reliability": 100 * games_factor * stability}


# --------------------------------------------------------------------------- database

def update_rankings(conn):
    """Replay every finished fixture in kickoff order and rebuild both ranking tables.

    A full replay takes seconds, so it always starts from scratch: late results,
    corrected scores and starting_rank changes are all picked up automatically.
    """
    levels = dict(conn.execute(
        "select league_id, starting_rank from leagues where starting_rank is not null").fetchall())
    missing = conn.execute(
        "select name || ' (' || country || ')' from leagues where starting_rank is null").fetchall()
    if missing:
        log.warning("Leagues without a starting_rank (using %s): %s", DEFAULT_STARTING_RANK,
                    ", ".join(r[0] for r in missing))

    # Oldest first; fixture_id breaks ties between matches with the same kickoff.
    rows = finished_fixtures(conn)
    fixtures = [r[:9] for r in rows]
    xg = {r[0]: (r[9], r[10]) for r in rows}

    first_league, first_comp = {}, {}
    for _, _, league_id, ltype, home, away, _, _, _ in fixtures:
        for team in (home, away):
            first_comp.setdefault(team, league_id)
            if ltype == "League":
                first_league.setdefault(team, league_id)

    def starting_rank(team):
        league_id = first_league.get(team, first_comp.get(team))
        return float(levels.get(league_id, DEFAULT_STARTING_RANK))

    replayed = _replay(conn, fixtures, xg, starting_rank)
    _rebuild_summary(conn, fixtures, first_league, first_comp, *replayed)
    conn.commit()


def _replay(conn, fixtures, xg, starting_rank):
    """Rebuild team_rank_history. xg: {fixture_id: (home xG, away xG)}.

    Returns ({team: history}, {team: last kickoff}, {team: rank changes in K_FACTOR units}).
    The last is for the volatility: matches with xG move ranks by K_FACTOR_XG, and weighted
    competitions by less, so their changes are scaled back, and volatility measures how
    surprising a team's results are rather than the step size. Also returns
    {team: (split s, home edge e) after its latest match} (side_ratings)."""
    conn.execute("truncate team_rank_history")
    matches = [Match(f[0], f[4], f[5], f[6], f[7], *xg[f[0]], COMPETITION_WEIGHT.get(f[2], 1.0), f[2])
               for f in fixtures]
    kickoffs = {f[0]: f[1] for f in fixtures}
    rows, history = run(matches, starting_rank)
    sides = side_ratings(rows)
    last_match, split = {}, {}

    match_no = {}
    recent = {}    # last 101 history values per team, for LT ALGO going into each match
    vol_changes = {}

    buf = io.StringIO()
    for (m, h, a, exp_diff, act_diff, change), (sh, sa, eh, ea) in zip(rows, sides):
        scale = K_FACTOR / (actual_diff(m)[1] * m.weight)
        for team, opp, is_home, before, delta, s_t, e_t in (
            (m.home, m.away, True, h, change, sh, eh),
            (m.away, m.home, False, a, -change, sa, ea),
        ):
            match_no[team] = match_no.get(team, 0) + 1
            hist = recent.setdefault(team, [before])
            s = summarise(hist)
            lt_before = s["lt_algo"] if s else before
            hist.append(before + delta)
            del hist[:-101]            # LT ALGO only looks at the last 100 values
            vol_changes.setdefault(team, []).append(delta * scale)
            last_match[team] = kickoffs[m.key]
            split[team] = (s_t, e_t)
            buf.write("\t".join(map(str, (
                m.key, team, match_no[team], kickoffs[m.key].isoformat(),
                "t" if is_home else "f", opp, before, before + delta, exp_diff, act_diff, delta,
                lt_before, *sides_of(before + delta, s_t, e_t),
            ))) + "\n")
    with conn.cursor() as cur:
        with cur.copy("copy team_rank_history (fixture_id, team_id, match_no, kickoff, is_home, "
                      "opponent_id, rank_before, rank_after, exp_diff, act_diff, rank_change, "
                      "lt_before, attack_after, defence_after, home_after, away_after) from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: replayed %d fixtures", len(rows))
    return history, last_match, vol_changes, split


def _rebuild_summary(conn, fixtures, first_league, first_comp, history, last_match, vol_changes, split):
    """Recreate team_rankings (the Ranking tab) from the replayed history
    ({team: [starting rank, rank after each match]})."""
    latest_league = {}
    for _, _, league_id, ltype, home, away, _, _, _ in fixtures:
        if ltype == "League":
            latest_league[home] = latest_league[away] = league_id

    # Goal averages over the last 12 months (HG/HA/AG/AA columns)
    today = date.today()
    # Sheet: DATE(YEAR(NOW())-1, MONTH(NOW()), DAY(NOW())); 29 Feb rolls to 1 Mar like Sheets
    try:
        one_year_ago = today.replace(year=today.year - 1)
    except ValueError:
        one_year_ago = date(today.year - 1, 3, 1)
    goals = {}
    for _, kickoff, _, _, home, away, hg, ag, _ in fixtures:
        if kickoff.date() >= one_year_ago:
            for team in (home, away):
                goals.setdefault(team, {"hg": [], "ha": [], "ag": [], "aa": []})
            goals[home]["hg"].append(hg); goals[home]["ha"].append(ag)
            goals[away]["ag"].append(ag); goals[away]["aa"].append(hg)
    avg = lambda xs: sum(xs) / len(xs) if xs else 0

    buf = io.StringIO()
    for team, hist in history.items():
        s = summarise(hist, vol_changes[team])
        g = goals.get(team, {"hg": [], "ha": [], "ag": [], "aa": []})
        buf.write("\t".join(map(str, (
            team, latest_league.get(team, first_comp.get(team)), hist[0], s["played"],
            last_match[team].isoformat(), s["current_rank"], s["st_algo"], s["rank_30"],
            s["rank_100"], s["lt_algo"], avg(g["hg"]), avg(g["ha"]), avg(g["ag"]), avg(g["aa"]),
            r"\N" if s["rank_volatility"] is None else s["rank_volatility"], s["reliability"],
            *sides_of(s["current_rank"], *split[team]),
        ))) + "\n")
    with conn.cursor() as cur:
        cur.execute("truncate team_rankings")
        with cur.copy("copy team_rankings (team_id, league_id, starting_rank, played, last_match, "
                      "current_rank, st_algo, rank_30, rank_100, lt_algo, hg, ha, ag, aa, "
                      "rank_volatility, reliability, attack, defence, home_rating, away_rating) "
                      "from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: summary rebuilt for %d teams", len(history))
