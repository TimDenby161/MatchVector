"""Match-day run (GitHub Actions every 30 minutes).

For matches kicking off in the next WINDOW_HOURS: refresh odds (so the last price before
kickoff is kept as the closing price) and injury lists, re-project, place 'late' paper bets,
refresh results of matches that kicked off recently, and settle finished bets.
"""
import logging
from datetime import datetime, timedelta, timezone

from . import betting, config, predictions
from .ingest import _store_fixtures, sync_injuries_fixtures, sync_odds_fixtures

log = logging.getLogger(__name__)

WINDOW_HOURS = 3
MAX_ODDS_CALLS = 250


def run_matchday(api, conn):
    now = datetime.now(timezone.utc)

    # Results for matches that kicked off in the last few hours
    recent = [r[0] for r in conn.execute(
        """select fixture_id from fixtures
           where kickoff between %s and %s and status_short not in ('FT', 'AET', 'PEN', 'CANC')""",
        [now - timedelta(hours=5), now])]
    for i in range(0, len(recent), 20):
        _store_fixtures(conn, api.get("fixtures", ids="-".join(map(str, recent[i:i + 20]))))
        conn.commit()

    # Leagues where bookmaker odds are actually available (seen in the last 30 days)
    odds_leagues = [r[0] for r in conn.execute(
        """select distinct f.league_id from odds o join fixtures f using (fixture_id)
           where f.kickoff > %s""", [now - timedelta(days=30)])]
    upcoming = conn.execute(
        """select fixture_id, league_id from fixtures
           where status_short in ('NS', 'TBD') and kickoff > %s and kickoff <= %s
           order by kickoff""", [now, now + timedelta(hours=WINDOW_HOURS)]).fetchall()
    with_odds = [f for f, l in upcoming if l in odds_leagues][:MAX_ODDS_CALLS]
    injury_fixtures = [f for f, l in upcoming if l in config.INJURY_MODEL_LEAGUES]
    log.info("Match day: %d upcoming in %dh (%d with odds, %d with injury lists), %d recent results",
             len(upcoming), WINDOW_HOURS, len(with_odds), len(injury_fixtures), len(recent))

    if with_odds:
        sync_odds_fixtures(api, conn, with_odds)
    if injury_fixtures:
        sync_injuries_fixtures(api, conn, injury_fixtures)
    if upcoming:
        predictions.update_predictions(conn)
        betting.place_late(conn)
    betting.settle_bets(conn)
