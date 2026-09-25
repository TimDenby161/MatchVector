"""The Corner FC data sync CLI.

    python -m thecornerfc init-db
    python -m thecornerfc status
    python -m thecornerfc sync all
    python -m thecornerfc sync fixtures --leagues 39 40 --seasons 2024 2025
    python -m thecornerfc sync stats --limit 2000
    python -m thecornerfc sync odds
    python -m thecornerfc nightly        # refresh everything that changes (scheduled task)
    python -m thecornerfc rank           # recalculate club rankings from every fixture
    python -m thecornerfc predict        # projected scores / W-D-L for upcoming fixtures
    python -m thecornerfc export         # JSON for the website in docs/data
    python -m thecornerfc matchday       # pre-kickoff odds/injuries, late paper bets, settle
"""
import argparse
import logging
import os
import sys

from . import usage
from . import config
from .api import ApiFootball, QuotaExhausted

TARGETS = ["leagues", "teams", "fixtures", "standings", "stats", "odds", "players", "injuries",
           "player_minutes", "player_careers", "retired", "squads", "coaches", "lineup_coaches", "colors", "cup_lineups"]
DB_WRITE_COMMANDS = {"init-db", "rank", "predict", "player-ratings", "matchday", "nightly", "sync"}
API_COMMANDS = {"status", "preflight", "matchday", "nightly", "sync"}


def _guard_command(args):
    if args.command in API_COMMANDS:
        config.require_api_access(args.command)
    if args.command in DB_WRITE_COMMANDS:
        config.require_db_write(args.command)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="thecornerfc")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("usage", help="Report persistent API usage without network access")
    preflight = sub.add_parser("preflight", help="Check manual backfill budget against live daily quota")
    preflight.add_argument("--leagues", type=int, nargs="+", required=True)
    preflight.add_argument("--seasons", type=int, nargs="+", required=True)
    sub.add_parser("init-db", help="Create tables in the database")
    sub.add_parser("status", help="Show API-Football account quota")
    nightly = sub.add_parser("nightly", help="Refresh current seasons, new stats and odds")
    nightly.add_argument("--leagues", type=int, nargs="+", default=list(config.LEAGUES))

    sub.add_parser("rank", help="Recalculate club rankings from every finished fixture")
    sub.add_parser("predict", help="Project scores and W/D/L chances for upcoming fixtures")
    sub.add_parser("export", help="Write JSON for the website to docs/data")
    sub.add_parser("player-ratings", help="Recalculate player ranks and team XI ratings (backdated)")
    sub.add_parser("matchday", help="Pre-kickoff odds and injuries, late paper bets, settle bets")

    sync = sub.add_parser("sync", help="Pull data from API-Football")
    sync.add_argument("target", choices=TARGETS + ["all"])
    sync.add_argument("--leagues", type=int, nargs="+", default=list(config.LEAGUES))
    sync.add_argument("--seasons", type=int, nargs="+", default=config.DEFAULT_SEASONS)
    sync.add_argument("--limit", type=int, help="Max fixtures to fetch stats for this run")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ["API_PROCESS_LABEL"] = args.command + (f" {args.target}" if args.command == "sync" else "")
    _guard_command(args)

    if args.command == "usage":
        usage.publish()
        return 0

    if args.command == "preflight":
        minimum = len(set(args.leagues)) * (1 + 3 * len(set(args.seasons)))
        print(f"Backfill minimum: {minimum} calls for leagues/teams/fixtures/standings; "
              "stats, odds pagination and retries are additional and unknown.")
        if config.API_RUN_BUDGET <= minimum + 1:
            raise RuntimeError("Set API_RUN_BUDGET above the minimum plus one preflight call")
        api = ApiFootball()
        try:
            api.get("status")
            if api.daily_remaining is None:
                raise QuotaExhausted("No daily quota header available; cannot verify backfill budget")
            with usage.connect() as ledger:
                used = ledger.execute("SELECT count(*) FROM api_calls WHERE run_id=?", (usage.run_id(),)).fetchone()[0]
            available = api.daily_remaining - api.daily_reserve
            if config.API_RUN_BUDGET - used > available:
                raise QuotaExhausted(f"Remaining run budget exceeds {available} calls available after reserve")
            print(f"Backfill cap: {config.API_RUN_BUDGET} attempts including preflight; available: {available}")
        finally:
            usage.publish()
        return 0

    if args.command == "status":
        info = ApiFootball().get("status")
        print(f"Account: {info['account']['email']}  Plan: {info['subscription']['plan']} "
              f"(ends {info['subscription']['end']})")
        print(f"Requests today: {info['requests']['current']} / {info['requests']['limit_day']}")
        return 0

    from . import export, ingest, matchday, player_ratings, predictions, ranking
    from .db import connect, init_schema

    with connect() as conn:
        if args.command == "init-db":
            init_schema(conn)
            print("Schema created.")
            return 0
        if args.command == "rank":
            ranking.update_rankings(conn)
            return 0
        if args.command == "player-ratings":
            player_ratings.compute_player_ratings(conn)
            return 0
        if args.command == "predict":
            predictions.update_predictions(conn)
            return 0
        if args.command == "export":
            export.export_site_data(conn)
            return 0

        api = ApiFootball()
        try:
            if args.command == "matchday":
                matchday.run_matchday(api, conn)
                export.export_bets(conn)
                export.export_injuries(conn)
                return 0
            if args.command == "nightly":
                failures = ingest.sync_nightly(api, conn, args.leagues)
                logging.info("Nightly sync finished with %d failed step(s)", failures)
                return 1 if failures else 0

            targets = TARGETS if args.target == "all" else [args.target]
            for target in targets:
                logging.info("=== Syncing %s", target)
                if target == "leagues":
                    ingest.sync_leagues(api, conn, args.leagues)
                elif target == "teams":
                    ingest.sync_teams(api, conn, args.leagues, args.seasons)
                elif target == "fixtures":
                    ingest.sync_fixtures(api, conn, args.leagues, args.seasons)
                elif target == "standings":
                    ingest.sync_standings(api, conn, args.leagues, args.seasons)
                elif target == "stats":
                    ingest.sync_fixture_stats(api, conn, args.leagues, args.seasons, limit=args.limit)
                elif target == "player_minutes":
                    ingest.sync_fixture_players(
                        api, conn, [l for l in args.leagues if l in config.MATCH_PLAYER_LEAGUES])
                elif target in ("players", "injuries"):
                    leagues = [l for l in args.leagues if l in config.PLAYER_LEAGUES] or args.leagues
                    fn = ingest.sync_players if target == "players" else ingest.sync_injuries
                    fn(api, conn, leagues, args.seasons)
                elif target == "player_careers":
                    ingest.sync_player_careers(api, conn)
                elif target == "retired":
                    ingest.check_retired(api, conn)
                elif target == "squads":
                    ingest.sync_squads(api, conn)
                elif target == "coaches":
                    ingest.sync_coaches(api, conn)
                elif target == "lineup_coaches":
                    ingest.sync_lineup_coaches(api, conn)
                elif target == "colors":
                    ingest.sync_team_colors(api, conn)
                elif target == "cup_lineups":
                    ingest.sync_cup_lineups(api, conn)
                elif target == "odds":
                    ingest.sync_odds(api, conn, ingest.active_seasons(conn, args.leagues))
        except QuotaExhausted as exc:
            conn.rollback()
            logging.warning("Stopping: %s. Re-run later to resume.", exc)
            return 2
        finally:
            usage.publish()
            logging.info("API calls this run: %d, daily quota left: %s",
                         api.calls_made, api.daily_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
