"""Pull data from API-Football and upsert it into Postgres."""
import logging
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import config
from .api import QuotaExhausted
from .db import upsert
from .ranking import update_rankings

log = logging.getLogger(__name__)

STAT_COLUMNS = {
    "Shots on Goal": "shots_on_goal",
    "Shots off Goal": "shots_off_goal",
    "Total Shots": "total_shots",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_inside_box",
    "Shots outsidebox": "shots_outside_box",
    "Fouls": "fouls",
    "Corner Kicks": "corners",
    "Offsides": "offsides",
    "Ball Possession": "possession_pct",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Goalkeeper Saves": "goalkeeper_saves",
    "Total passes": "passes_total",
    "Passes accurate": "passes_accurate",
    "Passes %": "passes_pct",
    "expected_goals": "expected_goals",
    "goals_prevented": "goals_prevented",
}

# Finished fixtures with no stats yet are retried until this old, then marked as done.
STATS_RETRY_WINDOW = timedelta(days=3)


# --------------------------------------------------------------------------- leagues

def sync_leagues(api, conn, league_ids):
    for league_id in league_ids:
        resp = api.get("leagues", id=league_id)
        if not resp:
            log.warning("League %s not found", league_id)
            continue
        item = resp[0]
        lg, country = item["league"], item["country"]
        upsert(conn, "leagues", [{
            "league_id": lg["id"],
            "name": lg["name"],
            "type": lg.get("type"),
            "country": country.get("name"),
            "country_code": country.get("code"),
            "logo": lg.get("logo"),
        }], ["league_id"])
        upsert(conn, "league_seasons", [{
            "league_id": lg["id"],
            "season": s["year"],
            "start_date": s.get("start"),
            "end_date": s.get("end"),
            "is_current": s.get("current"),
            "coverage": Jsonb(s.get("coverage")),
        } for s in item.get("seasons", [])], ["league_id", "season"])
        conn.commit()
        log.info("League %s %s: %d seasons", lg["id"], lg["name"], len(item.get("seasons", [])))


# --------------------------------------------------------------------------- teams

def sync_teams(api, conn, league_ids, seasons):
    for league_id in league_ids:
        for season in seasons:
            resp = api.get("teams", league=league_id, season=season)
            venues = [_venue_row(r["venue"]) for r in resp if r.get("venue", {}).get("id")]
            upsert(conn, "venues", _dedupe(venues, "venue_id"), ["venue_id"])
            upsert(conn, "teams", [_team_row(r["team"], r.get("venue")) for r in resp], ["team_id"])
            upsert(conn, "team_seasons", [{
                "team_id": r["team"]["id"], "league_id": league_id, "season": season,
            } for r in resp], ["team_id", "league_id", "season"], update_cols=[])
            conn.commit()
            log.info("Teams league=%s season=%s: %d", league_id, season, len(resp))


def _venue_row(v):
    return {
        "venue_id": v["id"],
        "name": v.get("name"),
        "address": v.get("address"),
        "city": v.get("city"),
        "capacity": v.get("capacity"),
        "surface": v.get("surface"),
        "image": v.get("image"),
    }


def _team_row(t, venue=None):
    return {
        "team_id": t["id"],
        "name": t["name"],
        "code": t.get("code"),
        "country": t.get("country"),
        "founded": t.get("founded"),
        "national": t.get("national"),
        "logo": t.get("logo"),
        "venue_id": (venue or {}).get("id"),
    }


# --------------------------------------------------------------------------- fixtures

def sync_fixtures(api, conn, league_ids, seasons):
    for league_id in league_ids:
        for season in seasons:
            resp = api.get("fixtures", league=league_id, season=season)
            _store_fixtures(conn, resp)
            conn.commit()
            log.info("Fixtures league=%s season=%s: %d", league_id, season, len(resp))


def _store_fixtures(conn, items):
    # Make sure every referenced team exists, without overwriting richer /teams data.
    teams = {}
    for f in items:
        for side in ("home", "away"):
            t = f["teams"][side]
            teams[t["id"]] = {"team_id": t["id"], "name": t["name"], "logo": t.get("logo")}
    upsert(conn, "teams", list(teams.values()), ["team_id"], update_cols=[])
    upsert(conn, "fixtures", [_fixture_row(f) for f in items], ["fixture_id"])


def _fixture_row(f):
    fx, lg, teams, goals, score = f["fixture"], f["league"], f["teams"], f["goals"], f["score"]
    venue, status = fx.get("venue") or {}, fx.get("status") or {}
    return {
        "fixture_id": fx["id"],
        "league_id": lg["id"],
        "season": lg["season"],
        "round": lg.get("round"),
        "kickoff": fx.get("date"),
        "referee": fx.get("referee"),
        "venue_id": venue.get("id"),
        "venue_name": venue.get("name"),
        "venue_city": venue.get("city"),
        "status_short": status.get("short"),
        "status_long": status.get("long"),
        "elapsed": status.get("elapsed"),
        "home_team_id": teams["home"]["id"],
        "away_team_id": teams["away"]["id"],
        "home_goals": goals.get("home"),
        "away_goals": goals.get("away"),
        "ht_home": score["halftime"]["home"],
        "ht_away": score["halftime"]["away"],
        "ft_home": score["fulltime"]["home"],
        "ft_away": score["fulltime"]["away"],
        "et_home": score["extratime"]["home"],
        "et_away": score["extratime"]["away"],
        "pen_home": score["penalty"]["home"],
        "pen_away": score["penalty"]["away"],
        "home_winner": teams["home"].get("winner"),
        "away_winner": teams["away"].get("winner"),
    }


# --------------------------------------------------------------------------- match statistics

def sync_fixture_stats(api, conn, league_ids, seasons=None, limit=None, batch_size=20):
    """Fetch stats for finished fixtures that don't have them yet.

    Uses /fixtures?ids= (max 20 per call), which embeds statistics, so each
    request covers up to 20 matches. Progress is committed per batch, so the
    job can be stopped and resumed at any time. seasons=None means all seasons.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select fixture_id from fixtures
            join league_seasons ls using (league_id, season)
            where stats_fetched_at is null
              and status_short = any(%s)
              and league_id = any(%s)
              and (%s::int[] is null or season = any(%s::int[]))
              -- skip league seasons where API-Football has no match statistics
              and coalesce((ls.coverage->'fixtures'->>'statistics_fixtures')::boolean, true)
            order by kickoff
            """ + (" limit %s" if limit else ""),
            [list(config.FINISHED_STATUSES), list(league_ids), seasons, seasons]
            + ([limit] if limit else []),
        )
        pending = [r[0] for r in cur.fetchall()]

    log.info("Fixtures needing stats: %d (~%d API calls)",
             len(pending), -(-len(pending) // batch_size))
    now = datetime.now(timezone.utc)
    done = 0

    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        resp = api.get("fixtures", ids="-".join(map(str, batch)))
        _store_fixtures(conn, resp)

        stat_rows, fetched_ids = [], []
        for f in resp:
            fx_id = f["fixture"]["id"]
            home_id = f["teams"]["home"]["id"]
            for team_stats in f.get("statistics") or []:
                stat_rows.append(_stats_row(fx_id, home_id, team_stats))
            kickoff = datetime.fromisoformat(f["fixture"]["date"])
            if f.get("statistics") or now - kickoff > STATS_RETRY_WINDOW:
                fetched_ids.append(fx_id)

        upsert(conn, "fixture_team_stats", stat_rows, ["fixture_id", "team_id"])
        if fetched_ids:
            conn.execute("update fixtures set stats_fetched_at = now() where fixture_id = any(%s)",
                         [fetched_ids])
        conn.commit()
        done += len(batch)
        log.info("Stats %d/%d (daily quota left: %s)", done, len(pending), api.daily_remaining)


def _stats_row(fixture_id, home_id, team_stats):
    row = {col: None for col in STAT_COLUMNS.values()}
    unmapped = {}
    for s in team_stats.get("statistics") or []:
        col = STAT_COLUMNS.get(s["type"])
        if col:
            row[col] = _parse_stat(s["value"])
        else:
            unmapped[s["type"]] = s["value"]
    team_id = team_stats["team"]["id"]
    return {
        "fixture_id": fixture_id,
        "team_id": team_id,
        "is_home": team_id == home_id,
        **row,
        # Only stat types without a column, to keep the table small.
        "raw": Jsonb(unmapped) if unmapped else None,
    }


def _parse_stat(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    value = str(value).strip().rstrip("%")
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- standings

def sync_standings(api, conn, league_ids, seasons):
    for league_id in league_ids:
        for season in seasons:
            resp = api.get("standings", league=league_id, season=season)
            rows = []
            for item in resp:
                for group in item["league"]["standings"]:
                    rows.extend(_standing_row(league_id, season, s) for s in group)
            teams = [{"team_id": r["team"]["id"], "name": r["team"]["name"], "logo": r["team"].get("logo")}
                     for item in resp for g in item["league"]["standings"] for r in g]
            upsert(conn, "teams", _dedupe(teams, "team_id"), ["team_id"], update_cols=[])
            key = ("league_id", "season", "group_name", "team_id")
            upsert(conn, "standings", _dedupe(rows, key), list(key))
            conn.commit()
            log.info("Standings league=%s season=%s: %d rows", league_id, season, len(rows))


def _standing_row(league_id, season, s):
    row = {
        "league_id": league_id,
        "season": season,
        "group_name": s.get("group") or "",
        "team_id": s["team"]["id"],
        "rank": s.get("rank"),
        "points": s.get("points"),
        "goal_diff": s.get("goalsDiff"),
        "form": s.get("form"),
        "status": s.get("status"),
        "description": s.get("description"),
        "api_updated_at": s.get("update"),
    }
    for prefix, key in (("", "all"), ("home_", "home"), ("away_", "away")):
        rec = s.get(key) or {}
        goals = rec.get("goals") or {}
        row.update({
            f"{prefix}played": rec.get("played"),
            f"{prefix}win": rec.get("win"),
            f"{prefix}draw": rec.get("draw"),
            f"{prefix}lose": rec.get("lose"),
            f"{prefix}goals_for": goals.get("for"),
            f"{prefix}goals_against": goals.get("against"),
        })
    return row


# --------------------------------------------------------------------------- odds

def sync_odds(api, conn, league_seasons, bet_ids=None):
    """Pull pre-match odds for upcoming fixtures, for (league_id, season) pairs.

    API-Football only keeps odds from ~14 days before kickoff until shortly
    after, so this has to run regularly to build up history.
    """
    bet_ids = set(bet_ids or config.ODDS_BET_IDS)
    for league_id, season in league_seasons:
        resp = api.get_all_pages("odds", league=league_id, season=season)
        bookmakers, bets, rows = {}, {}, []
        for item in resp:
            fixture_id = item["fixture"]["id"]
            updated = item.get("update")
            for bm in item.get("bookmakers", []):
                bookmakers[bm["id"]] = {"bookmaker_id": bm["id"], "name": bm["name"]}
                for bet in bm.get("bets", []):
                    if bet["id"] not in bet_ids:
                        continue
                    bets[bet["id"]] = {"bet_id": bet["id"], "name": bet["name"]}
                    for v in bet.get("values", []):
                        rows.append({
                            "fixture_id": fixture_id,
                            "bookmaker_id": bm["id"],
                            "bet_id": bet["id"],
                            "selection": str(v["value"]),
                            "odd": _parse_stat(v.get("odd")),
                            "api_updated_at": updated,
                        })
        upsert(conn, "bookmakers", list(bookmakers.values()), ["bookmaker_id"], touch_updated_at=False)
        upsert(conn, "bet_types", list(bets.values()), ["bet_id"], touch_updated_at=False)
        upsert(conn, "odds", _dedupe(rows, ("fixture_id", "bookmaker_id", "bet_id", "selection")),
               ["fixture_id", "bookmaker_id", "bet_id", "selection"])
        conn.commit()
        log.info("Odds league=%s season=%s: %d fixtures, %d prices", league_id, season, len(resp), len(rows))


# --------------------------------------------------------------------------- nightly

# Keep refreshing a season for this long after it ends (late fixes, play-offs).
SEASON_GRACE_DAYS = 14


def active_seasons(conn, league_ids):
    """(league_id, season) pairs that are current or finished within the grace period.

    Relies on league_seasons being fresh, so run sync_leagues first.
    """
    rows = conn.execute(
        """
        select league_id, season from league_seasons
        where league_id = any(%s)
          and (is_current or end_date >= current_date - %s)
        order by league_id, season
        """,
        [list(league_ids), SEASON_GRACE_DAYS],
    ).fetchall()
    return [tuple(r) for r in rows]


def sync_nightly(api, conn, league_ids):
    """Refresh everything that changes day to day. Returns the number of failed steps."""
    sync_leagues(api, conn, league_ids)
    pairs = active_seasons(conn, league_ids)
    log.info("Active league seasons: %d", len(pairs))

    failures = 0

    def step(name, fn, *args):
        nonlocal failures
        try:
            fn(api, conn, *args)
        except QuotaExhausted:
            raise
        except Exception:
            # One bad league shouldn't stop the rest of the night's run.
            conn.rollback()
            failures += 1
            log.exception("Failed: %s %s", name, args)

    for league_id, season in pairs:
        step("teams", sync_teams, [league_id], [season])
        step("fixtures", sync_fixtures, [league_id], [season])
        step("standings", sync_standings, [league_id], [season])
    # All seasons, so earlier gaps and retries get picked up too.
    step("stats", sync_fixture_stats, league_ids)
    for pair in pairs:
        step("odds", sync_odds, [pair])
    step("rankings", lambda api, conn: update_rankings(conn))
    return failures


# --------------------------------------------------------------------------- helpers

def _dedupe(rows, key):
    """Postgres rejects an upsert batch that touches the same key twice; keep the last."""
    keys = key if isinstance(key, tuple) else (key,)
    return list({tuple(r[k] for k in keys): r for r in rows}.values())
