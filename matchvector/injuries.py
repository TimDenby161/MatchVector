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


def missing_strengths(conn):
    """{(fixture_id, team_id): missing strength} for every fixture with an injury list."""
    injured = defaultdict(list)
    for fid, team, player in conn.execute(
            "select fixture_id, team_id, player_id from injuries where league_id = any(%s)",
            [config.INJURY_MODEL_LEAGUES]):
        injured[(fid, team)].append(player)
    if not injured:
        return {}

    played = defaultdict(dict)                 # fixture -> {team: {player: minutes}}
    for fid, team, player, mins in conn.execute(
            "select fixture_id, team_id, player_id, minutes from fixture_players"):
        played[fid].setdefault(team, {})[player] = mins

    recent = defaultdict(lambda: deque(maxlen=RECENT_MATCHES))
    result = {}
    # Every fixture in time order: finished ones feed `recent`, any with an injury list
    # (finished or upcoming) gets a strength from the matches before it.
    for fid, home, away in conn.execute(
            """select fixture_id, home_team_id, away_team_id from fixtures
               where home_team_id = any(%s) or away_team_id = any(%s)
               order by kickoff, fixture_id""",
            [list({t for _, t in injured}), list({t for _, t in injured})]):
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
