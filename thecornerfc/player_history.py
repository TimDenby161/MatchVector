"""First successful UTC daily observation of the current rated-player population."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from . import config, positions
from .model_versions import ModelType, current_code_sha, register_model_version


def register_version(conn):
    from . import player_ratings as p
    names=('WINDOW_DAYS','WINDOW_APPS','SHRINK_MINUTES','MINUTES_PRIOR','PREDICT_MATCHES')
    settings={name:getattr(p,name) for name in names}
    settings['leagues']=list(config.MATCH_PLAYER_LEAGUES)
    settings['reference_leagues']=list(config.RATING_REFERENCE_LEAGUES)
    settings['weights']=p.WEIGHTS
    settings['source_digests']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                               for name in ('player_ratings.py','positions.py','player_history.py')}
    return register_model_version(conn,ModelType.PLAYER,'current-player-rating',
        code_sha=current_code_sha(),configuration=settings,
        notes='Daily observed current ranks; world rank means this captured rated population, competition ties.')


def build_rows(current, season_rows, season, squads):
    seasonal={p:(rank,minutes,team) for p,y,rank,minutes,team in season_rows if y==season}
    ordered=sorted(current,key=lambda r:(-round(float(r[1]),1),r[0]))
    previous=None
    rank=0
    rows=[]
    for ordinal,(player,rating,role,minutes) in enumerate(ordered,1):
        rating=round(float(rating),1)
        if rating!=previous:
            rank=ordinal
        previous=rating
        record=seasonal.get(player)
        team=squads.get(player)
        source='latest_squad' if team is not None else 'season_model' if record and record[2] else 'unknown'
        if team is None:
            team=record[2] if record else None
        rows.append((player,rating,rank,role,positions.GROUPS.get(role),team,source,minutes,
                     record[1] if record else None,'season_model' if record else 'window_fallback'))
    return rows


def capture(conn,current,season_rows):
    if not current:
        return
    config.require_db_write('capture player rating history')
    observed=datetime.now(timezone.utc)
    # Serialize daily capture ownership. A failed transaction releases the day for retry.
    conn.execute('SELECT pg_advisory_xact_lock(%s,%s)',[73192,observed.date().toordinal()])
    if conn.execute('SELECT capture_id FROM player_rating_captures WHERE capture_date=%s',[observed.date()]).fetchone():
        return
    season=max(row[1] for row in season_rows)
    squads=dict(conn.execute('''SELECT DISTINCT ON(player_id) player_id,team_id FROM team_squads
        WHERE player_id=any(%s) ORDER BY player_id,fetched_at DESC,team_id''',[[r[0] for r in current]]))
    rows=build_rows(current,season_rows,season,squads)
    version=register_version(conn)
    capture_id=conn.execute('''INSERT INTO player_rating_captures
        (capture_date,captured_at,model_version_id,season,population) VALUES (%s,%s,%s,%s,%s)
        RETURNING capture_id''',[observed.date(),observed,version,season,len(rows)]).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany('''INSERT INTO player_rating_history
            (capture_id,player_id,rating,world_rank,position,rating_group,team_id,team_source,
             window_minutes,season_minutes,rating_source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            [(capture_id,*row) for row in rows])
