"""Pull data from API-Football and upsert it into Postgres."""
import logging
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import betting, config, positions
from .api import QuotaExhausted
from .db import upsert
from .player_ratings import compute_player_ratings
from .predictions import backfill_predictions, update_predictions
from .ranking import update_rankings
from .rating import rate_fixtures

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
    for league_id, season in league_seasons:
        resp = api.get_all_pages("odds", league=league_id, season=season)
        n = _store_odds(conn, resp, bet_ids)
        log.info("Odds league=%s season=%s: %d fixtures, %d prices", league_id, season, len(resp), n)


def sync_odds_fixtures(api, conn, fixture_ids, bet_ids=None):
    """Refresh odds for specific fixtures (one call each) - used close to kickoff."""
    total = 0
    for fid in fixture_ids:
        total += _store_odds(conn, api.get("odds", fixture=fid), bet_ids)
    log.info("Odds for %d fixtures: %d prices", len(fixture_ids), total)


def _store_odds(conn, resp, bet_ids=None):
    """Upsert odds items. Only fixtures that haven't kicked off are written, so after kickoff
    the stored price is the closing price; first_odd/first_seen_at keep the opening price."""
    bet_ids = set(bet_ids or config.ODDS_BET_IDS)
    now = datetime.now(timezone.utc)
    bookmakers, bets, rows = {}, {}, []
    for item in resp:
        fixture_id = item["fixture"]["id"]
        kickoff = item["fixture"].get("date")
        if kickoff and datetime.fromisoformat(kickoff) <= now:
            continue
        updated = item.get("update")
        for bm in item.get("bookmakers", []):
            bookmakers[bm["id"]] = {"bookmaker_id": bm["id"], "name": bm["name"]}
            for bet in bm.get("bets", []):
                if bet["id"] not in bet_ids:
                    continue
                bets[bet["id"]] = {"bet_id": bet["id"], "name": bet["name"]}
                for v in bet.get("values", []):
                    odd = _parse_stat(v.get("odd"))
                    rows.append({
                        "fixture_id": fixture_id,
                        "bookmaker_id": bm["id"],
                        "bet_id": bet["id"],
                        "selection": str(v["value"]),
                        "odd": odd,
                        "api_updated_at": updated,
                        "first_odd": odd,
                        "first_seen_at": now,
                    })
    upsert(conn, "bookmakers", list(bookmakers.values()), ["bookmaker_id"], touch_updated_at=False)
    upsert(conn, "bet_types", list(bets.values()), ["bet_id"], touch_updated_at=False)
    upsert(conn, "odds", _dedupe(rows, ("fixture_id", "bookmaker_id", "bet_id", "selection")),
           ["fixture_id", "bookmaker_id", "bet_id", "selection"],
           update_cols=["odd", "api_updated_at"])       # opening price is never overwritten
    conn.commit()
    return len(rows)


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
    # A newly added league has no fixtures yet: pull every season once, not just the current one
    have = {r[0] for r in conn.execute(
        "select distinct league_id from fixtures where league_id = any(%s)", [list(league_ids)])}
    new = [l for l in league_ids if l not in have]
    if new:
        log.info("New leagues, pulling all seasons: %s", new)
        pairs = sorted(set(pairs) | {(l, s) for l in new for s in config.DEFAULT_SEASONS})

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
    step("player minutes", sync_fixture_players,
         [l for l in league_ids if l in config.INJURY_MODEL_LEAGUES])
    for pair in pairs:
        step("odds", sync_odds, [pair])
    for league_id, season in pairs:
        if league_id in config.PLAYER_LEAGUES:
            step("players", sync_players, [league_id], [season])
            step("injuries", sync_injuries, [league_id], [season])
    step("rankings", lambda api, conn: update_rankings(conn))
    step("player ratings", lambda api, conn: compute_player_ratings(conn))
    step("predictions", lambda api, conn: update_predictions(conn))
    step("prediction backfill", lambda api, conn: backfill_predictions(conn))
    step("prediction ratings", lambda api, conn: rate_fixtures(conn))
    step("settle paper bets", lambda api, conn: betting.settle_bets(conn))
    step("early paper bets", lambda api, conn: betting.place_early(conn))
    return failures


# --------------------------------------------------------------------------- helpers

def _dedupe(rows, key):
    """Postgres rejects an upsert batch that touches the same key twice; keep the last."""
    keys = key if isinstance(key, tuple) else (key,)
    return list({tuple(r[k] for k in keys): r for r in rows}.values())


# --------------------------------------------------------------------------- players

def sync_players(api, conn, league_ids, seasons):
    """Player profiles and per-season stats (/players, 20 per page)."""
    for league_id in league_ids:
        for season in seasons:
            resp = api.get_all_pages("players", league=league_id, season=season)
            players, stats = {}, {}
            for item in resp:
                p = item["player"]
                players[p["id"]] = _player_row(p)
                for st in item.get("statistics", []):
                    if (st.get("league") or {}).get("id") != league_id or not (st.get("team") or {}).get("id"):
                        continue
                    row = _player_season_row(p["id"], league_id, season, st)
                    stats[(row["player_id"], row["team_id"])] = row
            upsert(conn, "players", list(players.values()), ["player_id"])
            upsert(conn, "player_seasons", list(stats.values()),
                   ["player_id", "team_id", "league_id", "season"])
            conn.commit()
            log.info("Players league=%s season=%s: %d players, %d season rows",
                     league_id, season, len(players), len(stats))


def _int(v):
    v = _parse_stat(v)
    return None if v is None else int(v)


def _player_row(p):
    birth = p.get("birth") or {}
    return {
        "player_id": p["id"],
        "name": p.get("name") or f"Player {p['id']}",
        "firstname": p.get("firstname"),
        "lastname": p.get("lastname"),
        "birth_date": birth.get("date"),
        "nationality": p.get("nationality"),
        "height_cm": _int((p.get("height") or "").replace("cm", "").strip() or None),
        "weight_kg": _int((p.get("weight") or "").replace("kg", "").strip() or None),
        "photo": p.get("photo"),
    }


def _player_season_row(player_id, league_id, season, st):
    g = lambda section, key: (st.get(section) or {}).get(key)
    return {
        "player_id": player_id, "team_id": st["team"]["id"], "league_id": league_id, "season": season,
        "position": g("games", "position"), "shirt_number": _int(g("games", "number")),
        "appearances": _int(g("games", "appearences")), "starts": _int(g("games", "lineups")),
        "minutes": _int(g("games", "minutes")), "rating": _parse_stat(g("games", "rating")),
        "captain": g("games", "captain"),
        "subbed_in": _int(g("substitutes", "in")), "subbed_out": _int(g("substitutes", "out")),
        "bench": _int(g("substitutes", "bench")),
        "goals": _int(g("goals", "total")), "assists": _int(g("goals", "assists")),
        "goals_conceded": _int(g("goals", "conceded")), "saves": _int(g("goals", "saves")),
        "shots": _int(g("shots", "total")), "shots_on": _int(g("shots", "on")),
        "passes": _int(g("passes", "total")), "key_passes": _int(g("passes", "key")),
        "pass_accuracy": _int(g("passes", "accuracy")),
        "tackles": _int(g("tackles", "total")), "blocks": _int(g("tackles", "blocks")),
        "interceptions": _int(g("tackles", "interceptions")),
        "duels": _int(g("duels", "total")), "duels_won": _int(g("duels", "won")),
        "dribbles": _int(g("dribbles", "attempts")), "dribbles_won": _int(g("dribbles", "success")),
        "dribbled_past": _int(g("dribbles", "past")),
        "fouls_drawn": _int(g("fouls", "drawn")), "fouls_committed": _int(g("fouls", "committed")),
        "yellow_cards": _int(g("cards", "yellow")), "yellow_red_cards": _int(g("cards", "yellowred")),
        "red_cards": _int(g("cards", "red")),
        "penalties_won": _int(g("penalty", "won")), "penalties_committed": _int(g("penalty", "commited")),
        "penalties_scored": _int(g("penalty", "scored")), "penalties_missed": _int(g("penalty", "missed")),
        "penalties_saved": _int(g("penalty", "saved")),
    }


def sync_injuries(api, conn, league_ids, seasons):
    """Players listed as missing/doubtful per fixture (/injuries)."""
    for league_id in league_ids:
        for season in seasons:
            resp = api.get("injuries", league=league_id, season=season)
            rows = {}
            for item in resp:
                p, fx = item.get("player") or {}, item.get("fixture") or {}
                if not p.get("id") or not fx.get("id"):
                    continue
                rows[(fx["id"], p["id"])] = {
                    "fixture_id": fx["id"], "player_id": p["id"], "team_id": item["team"]["id"],
                    "league_id": league_id, "season": season,
                    "type": p.get("type"), "reason": p.get("reason"),
                }
            upsert(conn, "injuries", list(rows.values()), ["fixture_id", "player_id"])
            conn.commit()
            log.info("Injuries league=%s season=%s: %d", league_id, season, len(rows))


def sync_fixture_players(api, conn, league_ids, batch_size=20):
    """Per-match player minutes for finished fixtures not fetched yet (/fixtures?ids=)."""
    pending = [r[0] for r in conn.execute(
        """select fixture_id from fixtures
           where players_fetched_at is null and status_short = any(%s) and league_id = any(%s)
           order by kickoff""", [list(config.FINISHED_STATUSES), list(league_ids)])]
    log.info("Fixtures needing player minutes: %d (~%d API calls)", len(pending), -(-len(pending) // batch_size))
    now = datetime.now(timezone.utc)
    for i in range(0, len(pending), batch_size):
        resp = api.get("fixtures", ids="-".join(map(str, pending[i:i + batch_size])))
        rows, done, formations = [], [], []
        for f in resp:
            fid = f["fixture"]["id"]
            grids = {}                               # player -> (grid, role) for starters
            for lu in f.get("lineups") or []:
                formation = lu.get("formation")
                if lu.get("team", {}).get("id"):
                    formations.append({"fixture_id": fid, "team_id": lu["team"]["id"], "formation": formation})
                for st in lu.get("startXI") or []:
                    pl = st.get("player") or {}
                    if pl.get("id"):
                        grids[pl["id"]] = (pl.get("grid"), positions.role(formation, pl.get("grid")))
            for team_block in f.get("players") or []:
                team_id = team_block["team"]["id"]
                for p in team_block.get("players") or []:
                    games = (p.get("statistics") or [{}])[0].get("games") or {}
                    minutes = _int(games.get("minutes"))
                    if not minutes or not p["player"].get("id"):
                        continue
                    st = (p.get("statistics") or [{}])[0]
                    g = lambda section, key: _int((st.get(section) or {}).get(key))
                    rows.append({"fixture_id": fid, "team_id": team_id, "player_id": p["player"]["id"],
                                 "minutes": minutes, "started": games.get("substitute") is False,
                                 "position": games.get("position"),
                                 "rating": _parse_stat(games.get("rating")),
                                 "goals": g("goals", "total"), "assists": g("goals", "assists"),
                                 "shots": g("shots", "total"), "shots_on": g("shots", "on"),
                                 "key_passes": g("passes", "key"), "passes": g("passes", "total"),
                                 # per-match "accuracy" is the number of accurate passes
                                 "passes_accurate": g("passes", "accuracy"),
                                 "tackles": g("tackles", "total"), "interceptions": g("tackles", "interceptions"),
                                 "blocks": g("tackles", "blocks"), "duels": g("duels", "total"),
                                 "duels_won": g("duels", "won"), "dribbles": g("dribbles", "attempts"),
                                 "dribbles_won": g("dribbles", "success"),
                                 "fouls_committed": g("fouls", "committed"), "fouls_drawn": g("fouls", "drawn"),
                                 "yellow_cards": g("cards", "yellow"), "red_cards": g("cards", "red"),
                                 "saves": g("goals", "saves"), "goals_conceded": g("goals", "conceded"),
                                 "penalties_saved": g("penalty", "saved"),
                                 "grid": grids.get(p["player"]["id"], (None, None))[0],
                                 "role": grids.get(p["player"]["id"], (None, None))[1]})
            kickoff = datetime.fromisoformat(f["fixture"]["date"])
            if f.get("players") or now - kickoff > STATS_RETRY_WINDOW:
                done.append(fid)
        upsert(conn, "fixture_players", _dedupe(rows, ("fixture_id", "player_id")),
               ["fixture_id", "player_id"], touch_updated_at=False)
        upsert(conn, "fixture_formations", _dedupe(formations, ("fixture_id", "team_id")),
               ["fixture_id", "team_id"], touch_updated_at=False)
        if done:
            conn.execute("update fixtures set players_fetched_at = now() where fixture_id = any(%s)", [done])
        conn.commit()
        if (i // batch_size) % 50 == 0:
            log.info("Player minutes %d/%d", i + len(resp), len(pending))


def sync_injuries_fixtures(api, conn, fixture_ids):
    """Refresh the injury list for specific fixtures (one call each) - used close to kickoff."""
    rows = {}
    for fid in fixture_ids:
        for item in api.get("injuries", fixture=fid):
            p, lg = item.get("player") or {}, item.get("league") or {}
            if p.get("id"):
                rows[(fid, p["id"])] = {
                    "fixture_id": fid, "player_id": p["id"], "team_id": item["team"]["id"],
                    "league_id": lg.get("id"), "season": lg.get("season"),
                    "type": p.get("type"), "reason": p.get("reason"),
                }
    upsert(conn, "injuries", list(rows.values()), ["fixture_id", "player_id"])
    conn.commit()
    log.info("Injuries for %d fixtures: %d players", len(fixture_ids), len(rows))
