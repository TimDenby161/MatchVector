from pathlib import Path

import psycopg

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def connect():
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL or READ_ONLY_DATABASE_URL is not set (see .env.example)")
    # prepare_threshold=None keeps it compatible with Supabase's pgbouncer poolers.
    conn = psycopg.connect(config.DATABASE_URL, prepare_threshold=None)
    if config.READ_ONLY:
        try:
            conn.read_only = True
        except AttributeError:
            conn.execute("set session characteristics as transaction read only")
    return conn


def init_schema(conn):
    config.require_db_write("init-db")
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def upsert(conn, table, rows, key_cols, update_cols=None, touch_updated_at=True):
    """Insert rows (list of dicts) and update non-key columns on conflict.

    Pass update_cols=[] to do nothing on conflict.
    """
    if not rows:
        return 0
    config.require_db_write(f"upsert into {table}")
    cols = list(rows[0].keys())
    if update_cols is None:
        update_cols = [c for c in cols if c not in key_cols]

    placeholders = ", ".join(f"%({c})s" for c in cols)
    sql = f"insert into {table} ({', '.join(cols)}) values ({placeholders}) " \
          f"on conflict ({', '.join(key_cols)}) "
    if update_cols:
        sets = [f"{c} = excluded.{c}" for c in update_cols]
        if touch_updated_at:
            sets.append("updated_at = now()")
        sql += "do update set " + ", ".join(sets)
    else:
        sql += "do nothing"

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)
