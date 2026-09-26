"""One availability merge for upcoming lineup selection and display.

API fixture reports and active manual absences are additive exclusions. No evidence
is not confirmation of fitness. Manual entries never alter historical fixtures.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

ABSENCES_FILE = Path(__file__).with_name('absences.json')


def manual_absences():
    payload = json.loads(ABSENCES_FILE.read_text(encoding='utf-8'))
    return {int(team): rows for team, rows in payload.items() if team.isdigit()}


def merge(api_rows, manual_rows, *, kickoff, observed_at, upcoming):
    """Return player -> resolved state plus unchanged source evidence."""
    evidence = {}
    for row in api_rows:
        evidence.setdefault(row['player'], []).append({**row, 'source': 'api_football'})
    if upcoming and kickoff > observed_at:
        day = kickoff.date().isoformat()
        for row in manual_rows:
            if (row.get('from') or observed_at.date().isoformat()) <= day <= (row.get('until') or '9999-12-31'):
                evidence.setdefault(row['player'], []).append({**row, 'source': 'manual',
                    'type': 'Missing Fixture', 'observed_at': observed_at.isoformat()})
    resolved = {}
    for player, items in evidence.items():
        words = ' '.join(str(r.get('reason','')) + ' ' + str(r.get('type','')) for r in items).lower()
        state = 'suspended' if any(w in words for w in ('suspend','suspension','red card','yellow card')) else (
            'doubtful' if all('doubt' in str(r.get('type','')).lower() for r in items) else 'unavailable')
        resolved[player] = {'state': state, 'excluded': True, 'evidence': items}
    return resolved


def load(conn, fixtures, *, observed_at=None):
    """fixtures: (fixture, kickoff, home, away, upcoming, ...). SELECT-only."""
    observed_at = observed_at or datetime.now(timezone.utc)
    fixtures = list(fixtures)
    if not fixtures:
        return {}
    api = {}
    for fid, team, player, kind, reason, updated in conn.execute(
        'SELECT fixture_id,team_id,player_id,type,reason,updated_at FROM injuries WHERE fixture_id=any(%s)',
        [[f[0] for f in fixtures]]):
        api.setdefault((fid,team), []).append({'player':player,'type':kind,'reason':reason,
            'source_updated_at':updated.isoformat() if updated else None,
            'observed_at':observed_at.isoformat()})
    manual = manual_absences()
    return {(fid,team): merge(api.get((fid,team), []),manual.get(team, []),
                             kickoff=kickoff,observed_at=observed_at,upcoming=upcoming)
            for fid,kickoff,home,away,upcoming,*_ in fixtures for team in (home,away)}


def next_fixtures(conn):
    rows = conn.execute("""SELECT DISTINCT ON (t) fixture_id,kickoff,home_team_id,away_team_id,true
        FROM fixtures, unnest(array[home_team_id,away_team_id]) t
        WHERE status_short IN ('NS','TBD') AND kickoff>now() ORDER BY t,kickoff,fixture_id""")
    return sorted({r[0]:tuple(r) for r in rows}.values(),key=lambda r:(r[1],r[0]))
