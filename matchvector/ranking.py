"""Club ranking: a port of the "Club Ranking" Google Sheet.

Per match (Individual Results tab):
    exp_diff    = (home_rank * 1.09 - away_rank) / 100
    act_diff    = home_goals - away_goals
    rank_change = (act_diff - exp_diff) * K
    home_rank  += rank_change;  away_rank -= rank_change

K is 10 (as in the sheet), except domestic cups (FA Cup, EFL Cup, Scottish cups...)
use 5: rotated squads and giant-killings otherwise drain points from top leagues.

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
    rank_volatility  = standard deviation of the rank around its own linear trend over the
                       last 30 games (a steady rise or fall isn't volatility, and neither
                       are big per-match changes that cancel out)
    stability_factor = min(1, (27 / rank_volatility) ** 1.5)   (1 if under 10 games)
    reliability      = 100 * games_factor * stability_factor
"""
import io
import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date

from . import config

log = logging.getLogger(__name__)

HOME_ADVANTAGE = 1.09
K_FACTOR = 10
DOMESTIC_CUP_K_FACTOR = 5     # competitions of type 'Cup' outside country 'World'
DEFAULT_STARTING_RANK = 650

# Reliability score tuning
GAMES_SCALE = 35            # games for the games factor to reach ~63%
VOLATILITY_WINDOW = 30      # recent matches used to measure rank swings
VOLATILITY_THRESHOLD = 27   # ~75th percentile; only teams swinging more than this lose points
MIN_VOLATILITY_GAMES = 10


@dataclass
class Match:
    key: object          # fixture id (or sheet row)
    home: object
    away: object
    home_goals: int
    away_goals: int
    k: float = K_FACTOR


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
        exp_diff = (h * HOME_ADVANTAGE - a) / 100
        act_diff = m.home_goals - m.away_goals
        change = (act_diff - exp_diff) * m.k
        current[m.home], current[m.away] = h + change, a - change
        history[m.home].append(current[m.home])
        history[m.away].append(current[m.away])
        rows.append((m, h, a, exp_diff, act_diff, change))
    return rows, history


def summarise(history):
    """Ranking-tab figures for one team's history. None if no matches played."""
    played = len(history) - 1
    if played == 0:
        return None
    mean = lambda xs: sum(xs) / len(xs)
    rank_30, rank_100 = mean(history[-30:]), mean(history[-100:])
    st = 0.6 * history[-1] + 0.2 * mean(history[-3:]) + 0.1 * rank_30 + 0.1 * rank_100
    lt = 0.1 * st + 0.3 * rank_30 + 0.6 * rank_100
    window = history[-(VOLATILITY_WINDOW + 1):]
    volatility = _detrended_sd(window) if len(window) >= 3 else None
    games_factor = 1 - math.exp(-played / GAMES_SCALE)
    stability = 1.0
    if played >= MIN_VOLATILITY_GAMES and volatility:
        stability = min(1.0, (VOLATILITY_THRESHOLD / volatility) ** 1.5)
    return {"played": played, "current_rank": history[-1], "rank_30": rank_30,
            "rank_100": rank_100, "st_algo": st, "lt_algo": lt,
            "rank_volatility": volatility, "reliability": 100 * games_factor * stability}


def _detrended_sd(values):
    """Standard deviation of values around their least-squares straight line."""
    n = len(values)
    x_mean, y_mean = (n - 1) / 2, sum(values) / n
    slope = (sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
             / sum((i - x_mean) ** 2 for i in range(n)))
    return statistics.pstdev([y - (y_mean + slope * (i - x_mean)) for i, y in enumerate(values)])


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
    fixtures = conn.execute(
        """
        select f.fixture_id, f.kickoff, f.league_id, l.type, f.home_team_id, f.away_team_id,
               f.home_goals, f.away_goals, l.country
        from fixtures f join leagues l using (league_id)
        where f.status_short = any(%s) and f.home_goals is not null and f.away_goals is not null
        order by f.kickoff, f.fixture_id
        """,
        [list(config.FINISHED_STATUSES)],
    ).fetchall()

    first_league, first_comp = {}, {}
    for _, _, league_id, ltype, home, away, _, _, _ in fixtures:
        for team in (home, away):
            first_comp.setdefault(team, league_id)
            if ltype == "League":
                first_league.setdefault(team, league_id)

    def starting_rank(team):
        league_id = first_league.get(team, first_comp.get(team))
        return float(levels.get(league_id, DEFAULT_STARTING_RANK))

    _replay(conn, fixtures, starting_rank)
    _rebuild_summary(conn, fixtures, first_league, first_comp)
    conn.commit()


def _replay(conn, fixtures, starting_rank):
    conn.execute("truncate team_rank_history")
    matches = [Match(f[0], f[4], f[5], f[6], f[7],
                     DOMESTIC_CUP_K_FACTOR if f[3] == "Cup" and f[8] != "World" else K_FACTOR)
               for f in fixtures]
    kickoffs = {f[0]: f[1] for f in fixtures}
    rows, _ = run(matches, starting_rank)

    match_no = {}

    buf = io.StringIO()
    for m, h, a, exp_diff, act_diff, change in rows:
        for team, opp, is_home, before, delta in (
            (m.home, m.away, True, h, change),
            (m.away, m.home, False, a, -change),
        ):
            match_no[team] = match_no.get(team, 0) + 1
            buf.write("\t".join(map(str, (
                m.key, team, match_no[team], kickoffs[m.key].isoformat(),
                "t" if is_home else "f", opp, before, before + delta, exp_diff, act_diff, delta,
            ))) + "\n")
    with conn.cursor() as cur:
        with cur.copy("copy team_rank_history (fixture_id, team_id, match_no, kickoff, is_home, "
                      "opponent_id, rank_before, rank_after, exp_diff, act_diff, rank_change) "
                      "from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: replayed %d fixtures", len(rows))


def _rebuild_summary(conn, fixtures, first_league, first_comp):
    """Recreate team_rankings (the Ranking tab) from the full history."""
    history, last_match = {}, {}
    for team, rank_before, rank_after, kickoff in conn.execute(
            "select team_id, rank_before, rank_after, kickoff from team_rank_history "
            "order by team_id, match_no"):
        if team not in history:
            history[team] = [rank_before]          # starting rank
        history[team].append(rank_after)
        last_match[team] = kickoff

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
        s = summarise(hist)
        g = goals.get(team, {"hg": [], "ha": [], "ag": [], "aa": []})
        buf.write("\t".join(map(str, (
            team, latest_league.get(team, first_comp.get(team)), hist[0], s["played"],
            last_match[team].isoformat(), s["current_rank"], s["st_algo"], s["rank_30"],
            s["rank_100"], s["lt_algo"], avg(g["hg"]), avg(g["ha"]), avg(g["ag"]), avg(g["aa"]),
            r"\N" if s["rank_volatility"] is None else s["rank_volatility"], s["reliability"],
        ))) + "\n")
    with conn.cursor() as cur:
        cur.execute("truncate team_rankings")
        with cur.copy("copy team_rankings (team_id, league_id, starting_rank, played, last_match, "
                      "current_rank, st_algo, rank_30, rank_100, lt_algo, hg, ha, ag, aa, "
                      "rank_volatility, reliability) from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: summary rebuilt for %d teams", len(history))
