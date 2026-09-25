"""Small, response-free request ledger. Reporting never needs API or database access."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from . import config

PROCESS_ID = str(uuid.uuid4())


def run_id():
    return os.getenv("API_RUN_ID") or (
        f"{os.environ['GITHUB_RUN_ID']}/{os.getenv('GITHUB_RUN_ATTEMPT', '1')}"
        if os.getenv("GITHUB_RUN_ID") else PROCESS_ID
    )


@contextmanager
def connect():
    path = Path(config.API_LEDGER_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, endpoint TEXT NOT NULL,
        parameter_hash TEXT NOT NULL, workflow TEXT NOT NULL, run_id TEXT NOT NULL,
        process_id TEXT NOT NULL, command TEXT NOT NULL, http_status INTEGER,
        records_returned INTEGER, quota_remaining INTEGER, duration_ms INTEGER,
        success INTEGER NOT NULL, error_type TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS calls_time ON api_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS calls_run ON api_calls(run_id)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def record(endpoint, params, status, records, quota, duration, success, error):
    # Persist only a digest: no parameters, credentials, headers or response bodies.
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
    command = os.getenv('API_PROCESS_LABEL', 'python')
    with connect() as conn:
        conn.execute("INSERT INTO api_calls VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            datetime.now(timezone.utc).isoformat(), endpoint.split('?')[0], digest,
            os.getenv("GITHUB_WORKFLOW", "local"), run_id(), PROCESS_ID, command,
            status, records, quota, round(duration * 1000), int(success), error))


def report():
    today = datetime.now(timezone.utc).date().isoformat()
    with connect() as conn:
        calls = conn.execute("SELECT count(*), coalesce(sum(success),0) FROM api_calls WHERE timestamp >= ?", (today,)).fetchone()
        lines = ["Recorded usage only (cache loss or calls outside this client are not included).", f"API attempts today (UTC): {calls[0]}; successful: {calls[1]}"]
        for field in ('endpoint', 'workflow'):
            rows = conn.execute(f"SELECT {field}, count(*) FROM api_calls WHERE timestamp >= ? GROUP BY {field} ORDER BY count(*) DESC", (today,)).fetchall()
            lines.append(f"By {field}: " + (', '.join(f'{name}={count}' for name, count in rows) or 'none'))
        latest = conn.execute("SELECT run_id FROM api_calls ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            count = conn.execute("SELECT count(*) FROM api_calls WHERE run_id=?", latest).fetchone()[0]
            lines.append(f"Latest run {latest[0]}: {count} attempts")
        quota = conn.execute("SELECT quota_remaining,timestamp FROM api_calls WHERE quota_remaining IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        lines.append(f"Last observed daily quota: {quota[0]} (at {quota[1]})" if quota else "Last observed daily quota: unknown")
        lines.append("Subscription allowance period: unconfirmed; no monthly allowance assumed")
    return '\n'.join(lines)


def publish():
    output = report()
    print(output)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('\n```text\n' + output + '\n```\n')
