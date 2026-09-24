"""Missing-player strength per team per fixture, for the injury adjustment in predictions.py.

missing(team, fixture) = sum over players listed out or doubtful for the fixture of the
player's share of the team's minutes over its previous RECENT_MATCHES matches that have
player data (fixture_players). 1.0 = one ever-present player; long-term absentees have no
recent minutes, so they add ~0 (their absence is already in the team's form).

Backtest (config.INJURY_MODEL_LEAGUES, trained 2021/22-2023/24, tested 2024/25+): shifting
the expected margin by BETA * (away_missing - home_missing) improved test log loss by
~0.0007, a small but consistent gain across every variant tried.
"""
from collections import defaultdict, deque

from . import config

RECENT_MATCHES = 10
BETA = 0.1            # goals of expected margin per unit of missing strength


def missing_strengths(conn, fixture_ids=None):
    """{(fixture_id, team_id): missing strength} for every fixture with an injury list, or
    only those in fixture_ids.

    Only the minutes of players who appear on an injury list for that team are downloaded
    (plus which matches each team has player data for), not every appearance: the full
    fixture_players table is ~35 MB of database egress per call.
    """
    where, params = "league_id = any(%s)", [config.INJURY_MODEL_LEAGUES]
    if fixture_ids is not None:
        where += " and fixture_id = any(%s)"
        params.append(list(fixture_ids))
    injured = defaultdict(list)
    for fid, team, player in conn.execute(
            f"select fixture_id, team_id, player_id from injuries where {where}", params):
        injured[(fid, team)].append(player)
    if not injured:
        return {}
    teams = list({t for _, t in injured})

    played = defaultdict(dict)                 # fixture -> {team: {injured player: minutes}}
    for fid, team in conn.execute(
            "select distinct fixture_id, team_id from fixture_players where team_id = any(%s)",
            [teams]):
        played[fid][team] = {}
    for fid, team, player, mins in conn.execute(
            f"""select fp.fixture_id, fp.team_id, fp.player_id, fp.minutes from fixture_players fp
                join (select distinct team_id, player_id from injuries where {where}) i
                  using (team_id, player_id)""", params):
        played[fid][team][player] = mins

    recent = defaultdict(lambda: deque(maxlen=RECENT_MATCHES))
    result = {}
    # Every fixture in time order: finished ones feed `recent`, any with an injury list
    # (finished or upcoming) gets a strength from the matches before it.
    for fid, home, away in conn.execute(
            """select fixture_id, home_team_id, away_team_id from fixtures
               where home_team_id = any(%s) or away_team_id = any(%s)
               order by kickoff, fixture_id""",
            [teams, teams]):
        for team in (home, away):
            players = injured.get((fid, team))
            if players:
                games = recent[team]
                result[(fid, team)] = (
                    sum(g.get(p, 0) for g in games for p in players) / (90 * len(games))
                    if games else 0.0)
        for team, minutes in played.get(fid, {}).items():
            recent[team].append(minutes)
    return result
