import os
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from thecornerfc import config
import psycopg
try:
    with psycopg.connect(os.getenv("DATABASE_URL") if "--use-database-url" in sys.argv else config.DATABASE_URL,connect_timeout=15) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='60s'")
        names=('match_prediction_snapshots','odds_observations','team_rank_history','fixtures')
        info={}
        for name in names:
            if conn.execute('SELECT to_regclass(%s)',[name]).fetchone()[0]:
                info[name]=conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
        if 'match_prediction_snapshots' in info:
            info['snapshot_sources']=conn.execute('SELECT source,count(*),min(captured_at),max(captured_at) FROM match_prediction_snapshots GROUP BY source').fetchall()
        print(json.dumps(info,default=str))
except psycopg.Error as exc:
    print(json.dumps({'error_type':type(exc).__name__,'sqlstate':exc.sqlstate,'message':'Read-only database probe failed; credentials omitted'}))
    sys.exit(1)
