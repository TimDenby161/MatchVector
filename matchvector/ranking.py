"""Club ranking, based on the "Club Ranking" Google Sheet.

Per match:
    exp_diff    = (home_rank - away_rank + HOME_ADVANTAGE_POINTS) / 100
    act_diff    = home_goals - away_goals, capped at +/- MAX_GOAL_DIFF
    rank_change = (act_diff - exp_diff) * K_FACTOR
    home_rank  += rank_change;  away_rank -= rank_change

In European and Club World Cup matches each team's rank also gets leagues.europe_bonus for
its domestic league that season (manual; e.g. Premier League sides beat their domestic rank in
Europe). Within a league everyone shares the bonus, so league and domestic cup games ignore it.
team_rankings adds the bonus of each team's current league to its rank figures, so teams
compare fairly across leagues; team_rank_history keeps the raw ranks.

Differences from the sheet (exp_diff = (home*1.09 - away)/100, K = 10, no cap), chosen by
backtesting 2023-26 predictions: the x1.09 multiplier gave 0.4-1.1 goals of home advantage
(real: ~0.3 for everyone), and a smaller K plus a goal cap stops one freak result or cup
thrashing from swinging a rank. Prediction error fell from 1.77 to 1.68 goals per match.

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
    rank_volatility  = standard deviation of the last 30 per-match rank changes
    stability_factor = min(1, (10.5 / rank_volatility) ** 3)   (1 if under 10 games)
    reliability      = 100 * games_factor * stability_factor
"""
import io
import logging
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date

from . import config

log = logging.getLogger(__name__)

HOME_ADVANTAGE_POINTS = 30   # = 0.3 goals, the same for every team
K_FACTOR = 6
MAX_GOAL_DIFF = 3            # a 7-0 counts as 3-0
DEFAULT_STARTING_RANK = 650

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
    home_bonus: float = 0.0   # leagues.europe_bonus, European / CWC matches only
    away_bonus: float = 0.0


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
        exp_diff = (h + m.home_bonus - a - m.away_bonus + HOME_ADVANTAGE_POINTS) / 100
        act_diff = m.home_goals - m.away_goals
        capped = max(-MAX_GOAL_DIFF, min(MAX_GOAL_DIFF, act_diff))
        change = (capped - exp_diff) * K_FACTOR
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
    changes = [b - a for a, b in zip(history, history[1:])][-VOLATILITY_WINDOW:]
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
    bonus = {k: float(v) for k, v in conn.execute(
        "select league_id, europe_bonus from leagues where coalesce(europe_bonus, 0) <> 0")}
    missing = conn.execute(
        "select name || ' (' || country || ')' from leagues where starting_rank is null").fetchall()
    if missing:
        log.warning("Leagues without a starting_rank (using %s): %s", DEFAULT_STARTING_RANK,
                    ", ".join(r[0] for r in missing))

    # Oldest first; fixture_id breaks ties between matches with the same kickoff.
    fixtures = conn.execute(
        """
        select f.fixture_id, f.kickoff, f.league_id, l.type, f.home_team_id, f.away_team_id,
               f.home_goals, f.away_goals, l.country, f.season
        from fixtures f join leagues l using (league_id)
        where f.status_short = any(%s) and f.home_goals is not null and f.away_goals is not null
        order by f.kickoff, f.fixture_id
        """,
        [list(config.FINISHED_STATUSES)],
    ).fetchall()

    first_league, first_comp, league_games = {}, {}, Counter()
    for _, _, league_id, ltype, home, away, _, _, _, season in fixtures:
        for team in (home, away):
            first_comp.setdefault(team, league_id)
            if ltype == "League":
                first_league.setdefault(team, league_id)
                league_games[(team, season, league_id)] += 1
    # Domestic league per (team, season): the league it played most games in
    domestic = {}
    for (team, season, league_id), n in sorted(league_games.items(), key=lambda kv: kv[1]):
        domestic[(team, season)] = league_id

    def europe_bonus(team, season):
        league_id = domestic.get((team, season)) or domestic.get((team, season + 1))
        return bonus.get(league_id, 0.0)

    def starting_rank(team):
        league_id = first_league.get(team, first_comp.get(team))
        return float(levels.get(league_id, DEFAULT_STARTING_RANK))

    _replay(conn, fixtures, starting_rank, europe_bonus if bonus else None)
    _rebuild_summary(conn, fixtures, first_comp, bonus)
    conn.commit()


def _replay(conn, fixtures, starting_rank, europe_bonus=None):
    conn.execute("truncate team_rank_history")
    matches = []
    for f in fixtures:
        m = Match(f[0], f[4], f[5], f[6], f[7])
        if europe_bonus and f[8] == "World":
            m.home_bonus, m.away_bonus = europe_bonus(f[4], f[9]), europe_bonus(f[5], f[9])
        matches.append(m)
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


def _rebuild_summary(conn, fixtures, first_comp, bonus):
    """Recreate team_rankings (the Ranking tab) from the full history.

    Rank figures include the europe_bonus of the team's current league, so teams from
    different leagues compare fairly.
    """
    history, last_match = {}, {}
    for team, rank_before, rank_after, kickoff in conn.execute(
            "select team_id, rank_before, rank_after, kickoff from team_rank_history "
            "order by team_id, match_no"):
        if team not in history:
            history[team] = [rank_before]          # starting rank
        history[team].append(rank_after)
        last_match[team] = kickoff

    latest_league = {}
    for _, _, league_id, ltype, home, away, _, _, _, _ in fixtures:
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
    for _, kickoff, _, _, home, away, hg, ag, _, _ in fixtures:
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
        league_id = latest_league.get(team, first_comp.get(team))
        b = bonus.get(league_id, 0.0)
        buf.write("\t".join(map(str, (
            team, league_id, hist[0], s["played"],
            last_match[team].isoformat(), s["current_rank"] + b, s["st_algo"] + b, s["rank_30"] + b,
            s["rank_100"] + b, s["lt_algo"] + b, avg(g["hg"]), avg(g["ha"]), avg(g["ag"]), avg(g["aa"]),
            r"\N" if s["rank_volatility"] is None else s["rank_volatility"], s["reliability"], b,
        ))) + "\n")
    with conn.cursor() as cur:
        cur.execute("truncate team_rankings")
        with cur.copy("copy team_rankings (team_id, league_id, starting_rank, played, last_match, "
                      "current_rank, st_algo, rank_30, rank_100, lt_algo, hg, ha, ag, aa, "
                      "rank_volatility, reliability, europe_bonus) from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: summary rebuilt for %d teams", len(history))
