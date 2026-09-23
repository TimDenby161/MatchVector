"""Club ranking: a port of the "Club Ranking" Google Sheet.

Per match (Individual Results tab):
    exp_diff    = (home_rank * 1.09 - away_rank) / 100
    act_diff    = home_goals - away_goals
    rank_change = (act_diff - exp_diff) * 10
    home_rank  += rank_change;  away_rank -= rank_change

Every team starts from a Starting Rank: leagues.starting_rank of the first
league (type 'League') it plays in, else that of the first competition it
appears in (cups), else DEFAULT_STARTING_RANK.

Summary figures (Ranking tab), where history = [starting rank, rank after each match]:
    rank_30  = mean of the last 30 history values
    rank_100 = mean of the last 100 history values
    st_algo  = 0.6*current + 0.2*mean(last 3) + 0.1*rank_30 + 0.1*rank_100
    lt_algo  = 0.1*st_algo + 0.3*rank_30 + 0.6*rank_100
"""
import io
import logging
from dataclasses import dataclass
from datetime import date

from . import config

log = logging.getLogger(__name__)

HOME_ADVANTAGE = 1.09
K_FACTOR = 10
DEFAULT_STARTING_RANK = 650


@dataclass
class Match:
    key: object          # fixture id (or sheet row)
    home: object
    away: object
    home_goals: int
    away_goals: int


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
        change = (act_diff - exp_diff) * K_FACTOR
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
    return {"played": played, "current_rank": history[-1], "rank_30": rank_30,
            "rank_100": rank_100, "st_algo": st, "lt_algo": lt}


# --------------------------------------------------------------------------- database

def update_rankings(conn, full=False):
    """Bring team_rank_history up to date, strictly in kickoff order, then rebuild team_rankings.

    full=True replays every fixture from scratch (use after changing starting ranks).
    Otherwise only replays from the earliest new or changed fixture onwards, so a late
    result or a corrected score still lands in date order.
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
               f.home_goals, f.away_goals
        from fixtures f join leagues l using (league_id)
        where f.status_short = any(%s) and f.home_goals is not null and f.away_goals is not null
        order by f.kickoff, f.fixture_id
        """,
        [list(config.FINISHED_STATUSES)],
    ).fetchall()

    first_league, first_comp = {}, {}
    for _, _, league_id, ltype, home, away, _, _ in fixtures:
        for team in (home, away):
            first_comp.setdefault(team, league_id)
            if ltype == "League":
                first_league.setdefault(team, league_id)

    def starting_rank(team):
        league_id = first_league.get(team, first_comp.get(team))
        return float(levels.get(league_id, DEFAULT_STARTING_RANK))

    replay_from = None if full else _replay_point(conn, fixtures)
    if replay_from is False:
        log.info("Rankings: no new or changed fixtures")
    else:
        _replay(conn, fixtures, replay_from, starting_rank)
    _rebuild_summary(conn, fixtures, first_league, first_comp)
    conn.commit()


def _replay_point(conn, fixtures):
    """Kickoff to replay from: None = everything, False = nothing to do."""
    done = {r[0]: (r[1], r[2]) for r in conn.execute(
        "select fixture_id, kickoff, act_diff from team_rank_history where is_home")}
    if not done:
        return None
    current = {f[0]: f for f in fixtures}
    changed = [f[1] for f in fixtures
               if f[0] not in done or done[f[0]][1] != f[6] - f[7] or done[f[0]][0] != f[1]]
    # Fixtures ranked before but no longer finished (e.g. result annulled)
    changed += [kickoff for fid, (kickoff, _) in done.items() if fid not in current]
    # A moved kickoff also has to be undone from its old position
    changed += [done[f[0]][0] for f in fixtures if f[0] in done and done[f[0]][0] != f[1]]
    return min(changed) if changed else False


def _replay(conn, fixtures, replay_from, starting_rank):
    current, match_no = {}, {}
    if replay_from is not None:
        conn.execute("delete from team_rank_history where kickoff >= %s", [replay_from])
        for team, n, rank in conn.execute(
                "select distinct on (team_id) team_id, match_no, rank_after "
                "from team_rank_history order by team_id, match_no desc"):
            current[team], match_no[team] = rank, n
    else:
        conn.execute("truncate team_rank_history")

    todo = [f for f in fixtures if replay_from is None or f[1] >= replay_from]
    matches = [Match(f[0], f[4], f[5], f[6], f[7]) for f in todo]
    kickoffs = {f[0]: f[1] for f in todo}
    rows, _ = run(matches, lambda t: current[t] if t in current else starting_rank(t))

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
    log.info("Rankings: replayed %d fixtures from %s", len(rows), replay_from or "the start")


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
    for _, _, league_id, ltype, home, away, _, _ in fixtures:
        if ltype == "League":
            latest_league[home] = latest_league[away] = league_id

    # Goal averages over the last 12 months (HG/HA/AG/AA columns)
    today = date.today()
    one_year_ago = today.replace(year=today.year - 1, day=min(today.day, 28))
    goals = {}
    for _, kickoff, _, _, home, away, hg, ag in fixtures:
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
        ))) + "\n")
    with conn.cursor() as cur:
        cur.execute("truncate team_rankings")
        with cur.copy("copy team_rankings (team_id, league_id, starting_rank, played, last_match, "
                      "current_rank, st_algo, rank_30, rank_100, lt_algo, hg, ha, ag, aa) "
                      "from stdin") as cp:
            cp.write(buf.getvalue())
    log.info("Rankings: summary rebuilt for %d teams", len(history))
