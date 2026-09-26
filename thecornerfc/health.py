"""Local pipeline metadata and inexpensive, read-only dataset checks."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import os
import subprocess
from pathlib import Path
import uuid

from . import config, usage

CURRENT = ContextVar('pipeline_run', default=None)
DATASETS = ('teams', 'fixtures', 'standings', 'players', 'club_ratings',
            'player_ratings', 'predictions', 'injuries', 'odds', 'exports')
TABLES = {'teams': ('teams', 'updated_at'), 'fixtures': ('fixtures', 'updated_at'),
          'standings': ('standings', 'api_updated_at'), 'players': ('players', 'updated_at'),
          'club_ratings': ('team_rankings', 'last_match'),
          'player_ratings': ('player_position_ranks', None),
          'predictions': ('fixture_predictions', 'updated_at'),
          'injuries': ('injuries', 'updated_at'), 'odds': ('odds', 'api_updated_at')}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def store():
    with usage.connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS pipeline_runs (
          id TEXT PRIMARY KEY, run_id TEXT, workflow TEXT, command TEXT, code_sha TEXT,
          started TEXT, completed TEXT, status TEXT, failed_stage TEXT,
          api_calls INTEGER DEFAULT 0, quota_remaining INTEGER, warnings TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS dataset_status (
          dataset TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT, status TEXT,
          row_count INTEGER, latest_data_timestamp TEXT, message TEXT, run_id TEXT,
          successful_row_count INTEGER);
        ''')
        yield conn


def save_dataset(dataset, status, count=None, latest=None, message=''):
    state = CURRENT.get()
    stamp = now()
    with store() as conn:
        previous = conn.execute('SELECT last_success,successful_row_count,status,run_id FROM dataset_status WHERE dataset=?', (dataset,)).fetchone()
        # A later successful league must not hide an earlier failure in this command.
        if previous and state and previous[3] == state['id'] and (
                (previous[2] == 'FAIL' and status != 'FAIL') or
                (previous[2] == 'WARNING' and status in ('RUNNING', 'HEALTHY'))):
            return
        success = status == 'HEALTHY'
        conn.execute('''INSERT OR REPLACE INTO dataset_status VALUES (?,?,?,?,?,?,?,?,?)''', (
            dataset, stamp, stamp if success else (previous[0] if previous else None), status,
            count, str(latest) if latest else None, message[:500], state['id'] if state else None,
            count if success else (previous[1] if previous else None)))
    if state and status in ('FAIL', 'WARNING'):
        state['warnings'].append(f'{dataset}: {message}'[:500])
        if status == 'FAIL':
            state['failed'].append(dataset)


def baseline(dataset):
    with store() as conn:
        row = conn.execute('SELECT successful_row_count FROM dataset_status WHERE dataset=?', (dataset,)).fetchone()
    return row[0] if row else None


def population_status(count, previous):
    if previous is not None and previous >= config.HEALTH_MIN_BASELINE and count < previous * config.HEALTH_COLLAPSE_RATIO:
        return 'FAIL', f'Population collapsed from {previous} to {count}'
    return 'HEALTHY', 'Population checked' if previous is not None else 'INFO: first observation; no successful baseline yet'


def inspect_dataset(dataset, conn, call_start=0):
    table, timestamp = TABLES[dataset]
    count, latest = conn.execute(f'SELECT count(*), {"max(" + timestamp + ")" if timestamp else "NULL"} FROM {table}').fetchone()
    status, message = 'HEALTHY', 'INFO: dataset checked'
    if dataset in ('players', 'club_ratings', 'player_ratings'):
        status, message = population_status(count, baseline(dataset))
    if dataset in ('teams', 'fixtures', 'standings'):
        source = 'team_seasons' if dataset == 'teams' else table
        extra = "AND coalesce((ls.coverage->>'standings')::boolean,false)" if dataset == 'standings' else ''
        missing = conn.execute(f'''SELECT ls.league_id,ls.season FROM league_seasons ls
            WHERE current_date BETWEEN ls.start_date + 7 AND ls.end_date
            {extra} AND NOT EXISTS (SELECT 1 FROM {source} d
              WHERE d.league_id=ls.league_id AND d.season=ls.season)''').fetchall()
        if missing:
            status, message = 'WARNING', f'Active league seasons have no {dataset}: {missing[:10]}'
    if dataset == 'predictions':
        missing = conn.execute('''SELECT count(*) FROM fixtures f
            WHERE f.status_short IN ('NS','TBD') AND f.kickoff BETWEEN now() AND now()+interval '7 days'
            AND NOT EXISTS (SELECT 1 FROM fixture_predictions p WHERE p.fixture_id=f.fixture_id)''').fetchone()[0]
        if missing:
            status, message = 'WARNING', f'{missing} upcoming fixtures lack predictions (next 7 days)'
    if dataset == 'odds':
        expected, covered = conn.execute('''SELECT count(*), count(*) FILTER (WHERE EXISTS
            (SELECT 1 FROM odds o WHERE o.fixture_id=f.fixture_id)) FROM fixtures f
            WHERE f.status_short='NS' AND f.kickoff BETWEEN now() AND now()+interval '3 days'
            AND EXISTS (SELECT 1 FROM odds o JOIN fixtures old USING(fixture_id)
              WHERE old.league_id=f.league_id AND old.kickoff BETWEEN now()-interval '30 days' AND now())''').fetchone()
        if expected and not covered:
            status, message = 'WARNING', f'No odds for {expected} upcoming fixtures in leagues with recent odds coverage'
        elif not expected:
            message = 'INFO: no upcoming fixtures with established recent odds coverage'
    # Upserts preserve old rows; DB counts alone cannot detect empty API responses.
    if dataset in ('teams', 'fixtures', 'standings', 'players', 'injuries', 'odds'):
        endpoint = dataset
        with usage.connect() as ledger:
            attempts, returned = ledger.execute('''SELECT count(*),coalesce(sum(records_returned),0)
                FROM api_calls WHERE process_id=? AND id>? AND endpoint=? AND success=1''',
                (usage.PROCESS_ID, call_start, endpoint)).fetchone()
        if attempts and returned == 0 and status != 'FAIL':
            status, message = 'WARNING', f'All {attempts} successful {endpoint} responses were empty; verify coverage/season (not automatically fatal)'
    save_dataset(dataset, status, count, latest, message)


class HealthFailure(RuntimeError):
    pass


def monitored(dataset, conn_index=0):
    """Track existing stage boundaries without changing model calculations."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if CURRENT.get() is None:
                return fn(*args, **kwargs)
            with usage.connect() as ledger:
                start = ledger.execute('SELECT coalesce(max(id),0) FROM api_calls').fetchone()[0]
            save_dataset(dataset, 'RUNNING', message=f'INFO: {fn.__name__} started')
            try:
                result = fn(*args, **kwargs)
                if dataset == 'exports':
                    from .export import OUT_DIR, validate_export
                    root = Path(kwargs.get('out_dir', args[1] if len(args)>1 else OUT_DIR))
                    validate_export(root)
                    payload = json.loads((root/'rankings.json').read_text())
                    save_dataset(dataset, 'HEALTHY', len(payload['rankings']), payload.get('generated_at'), 'INFO: JSON and publication checks passed')
                else:
                    conn = args[conn_index] if len(args)>conn_index else kwargs['conn']
                    inspect_dataset(dataset, conn, start)
                if dataset in CURRENT.get()['failed']:
                    raise HealthFailure(f'{dataset} sanity check failed')
                return result
            except HealthFailure:
                raise
            except Exception as exc:
                # Exception messages may contain credentials/SQL parameters. Store type only.
                save_dataset(dataset, 'FAIL', message=f'{fn.__name__}: {type(exc).__name__}; see stage logs')
                raise
        return wrapped
    return decorate


def code_sha():
    if os.getenv('GITHUB_SHA'):
        return os.environ['GITHUB_SHA']
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL,
                                       timeout=2, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return None


@contextmanager
def pipeline(command):
    state = {'id': str(uuid.uuid4()), 'failed': [], 'warnings': [], 'exit_code': 0}
    token = CURRENT.set(state)
    with store() as conn:
        start = conn.execute('SELECT coalesce(max(id),0) FROM api_calls').fetchone()[0]
        conn.execute('INSERT INTO pipeline_runs (id,run_id,workflow,command,code_sha,started,status) VALUES (?,?,?,?,?,?,?)',
                     (state['id'], usage.run_id(), os.getenv('GITHUB_WORKFLOW','local'), command,
                      code_sha(), now(), 'RUNNING'))
    error = None
    try:
        yield state
    except BaseException as exc:
        error = type(exc).__name__
        if not state['failed']:
            state['failed'].append(command)
        raise
    finally:
        with store() as conn:
            calls = conn.execute('SELECT count(*) FROM api_calls WHERE process_id=? AND id>?', (usage.PROCESS_ID,start)).fetchone()[0]
            quota = conn.execute('SELECT quota_remaining FROM api_calls WHERE process_id=? AND id>? AND quota_remaining IS NOT NULL ORDER BY id DESC LIMIT 1', (usage.PROCESS_ID,start)).fetchone()
            status = 'FAIL' if error or state['failed'] or state['exit_code'] else ('WARNING' if state['warnings'] else 'HEALTHY')
            conn.execute('UPDATE pipeline_runs SET completed=?,status=?,failed_stage=?,api_calls=?,quota_remaining=?,warnings=?,error=? WHERE id=?',
                         (now(), status, ', '.join(dict.fromkeys(state['failed'])) or (command if state['exit_code'] else None), calls,
                          quota[0] if quota else None, json.dumps(state['warnings'][-30:]), error, state['id']))
        CURRENT.reset(token)
        publish()


def publish():
    with store() as conn:
        rows = {r[0]: r for r in conn.execute('SELECT dataset,status,last_success,message FROM dataset_status')}
        run = conn.execute('SELECT command,status,failed_stage FROM pipeline_runs ORDER BY started DESC LIMIT 1').fetchone()
    lines, severity = [], 'INFO'
    for dataset in DATASETS:
        row = rows.get(dataset)
        status, message = ('UNKNOWN', 'No recorded attempt') if not row else (row[1], row[3])
        if row and status == 'HEALTHY' and row[2] and (datetime.now(timezone.utc)-datetime.fromisoformat(row[2])).total_seconds()>config.HEALTH_STALE_HOURS*3600:
            status = 'STALE'
        if status == 'FAIL':
            severity = 'FAIL'
        elif status != 'HEALTHY' and severity != 'FAIL':
            severity = 'WARNING'
        lines.append(f'{dataset} {status} — {message}')
    if run and run[1] == 'FAIL':
        severity = 'FAIL'
    elif run and run[1] in ('WARNING','RUNNING') and severity != 'FAIL':
        severity = 'WARNING'
    output = f'Overall: {severity}\n' + '\n'.join(lines)
    if run:
        output += f'\nLatest command: {run[0]} {run[1]}; failed stage: {run[2] or "none"}'
    print(output)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as stream:
            stream.write('\n```text\n'+output+'\n```\n')
    return 1 if severity == 'FAIL' else 0
