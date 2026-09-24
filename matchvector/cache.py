"""Local cache of big, mostly historical query results, to cut database egress.

cached_rows(conn, name, sql, params) runs a query whose first column is `part`, a partition
key (the week of kickoff). The server returns one fingerprint per partition (row count and a
sum of row hashes, a few hundred rows in all), and only partitions whose fingerprint changed
since the last run are downloaded again; the rest come from CACHE_DIR. Inserts, edits and
deletes anywhere are all picked up, including old matches added by a league backfill. A change
to the query text starts the cache again from scratch.

In GitHub Actions CACHE_DIR is kept between runs with actions/cache (see nightly.yml). With no
cache (a first run, or a machine without one) everything is downloaded once, as before.
"""
import hashlib
import logging
import os
import pickle
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("MATCHVECTOR_CACHE_DIR",
                           Path(__file__).resolve().parent.parent / ".cache"))
WEEK = "(date_trunc('week', {} at time zone 'UTC'))::date"


def cached_rows(conn, name, sql, params=(), order_by=None):
    """All rows of sql (without its `part` column), in partition order, and within each
    partition in order_by (column names of sql) order."""
    path = CACHE_DIR / f"{name}.pickle"
    key = hashlib.sha1(f"{sql}|{params}|{order_by}".encode()).hexdigest()
    parts = {}
    try:
        with open(path, "rb") as fh:
            saved = pickle.load(fh)
        if saved["key"] == key:
            parts = saved["parts"]
    except (OSError, EOFError, pickle.UnpicklingError, KeyError):
        pass

    fingerprints = {p: (n, h) for p, n, h in conn.execute(
        f"select part, count(*), sum(hashtext(x::text)) from ({sql}) x group by part", params)}
    stale = [p for p, fp in fingerprints.items() if p not in parts or parts[p][0] != fp]
    for p in [p for p in parts if p not in fingerprints]:
        del parts[p]
    if stale:
        fresh = {p: [] for p in stale}
        order = f" order by part, {order_by}" if order_by else ""
        for row in conn.execute(f"select * from ({sql}) x where part = any(%s){order}",
                                [*params, stale]):
            fresh[row[0]].append(row[1:])
        for p, rows in fresh.items():
            parts[p] = (fingerprints[p], rows)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump({"key": key, "parts": parts}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    log.info("Cache %s: %d/%d weeks downloaded (%d rows)", name, len(stale), len(parts),
             sum(len(fresh[p]) for p in stale) if stale else 0)
    return [row for p in sorted(parts) for row in parts[p][1]]


def finished_fixtures(conn):
    """Finished fixtures with a score, oldest first (kickoff, fixture_id):
    (fixture_id, kickoff, league_id, league type, home, away, home goals, away goals, country,
    home xG, away xG), xG None when not recorded."""
    return cached_rows(conn, "finished_fixtures", f"""
        select {WEEK.format('f.kickoff')} as part, f.fixture_id, f.kickoff, f.league_id, l.type,
               f.home_team_id, f.away_team_id, f.home_goals, f.away_goals, l.country,
               hx.expected_goals::float8 as home_xg, ax.expected_goals::float8 as away_xg
        from fixtures f join leagues l using (league_id)
        left join fixture_team_stats hx on hx.fixture_id = f.fixture_id and hx.is_home
        left join fixture_team_stats ax on ax.fixture_id = f.fixture_id and not ax.is_home
        where f.status_short = any(%s) and f.home_goals is not null and f.away_goals is not null""",
        [list(config.FINISHED_STATUSES)], order_by="kickoff, fixture_id")


def rank_history(conn):
    """team_rank_history joined to the result, in partition (week) order - sort before use:
    (fixture_id, team_id, match_no, kickoff, is_home, opponent_id, rank_before, rank_after,
    lt_before, home goals, away goals, league_id)."""
    return cached_rows(conn, "rank_history", f"""
        select {WEEK.format('h.kickoff')} as part, h.fixture_id, h.team_id, h.match_no, h.kickoff,
               h.is_home, h.opponent_id, h.rank_before, h.rank_after, h.lt_before,
               f.home_goals, f.away_goals, f.league_id
        from team_rank_history h join fixtures f using (fixture_id)""",
        order_by="team_id, match_no")
