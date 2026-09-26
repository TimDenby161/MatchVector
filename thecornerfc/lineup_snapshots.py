"""Immutable lineup selections and API official XI observations; no probability model."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import config, positions
from .model_versions import ModelType, current_code_sha, register_model_version, snapshot_times


def register_version(conn):
    from .player_ratings import PREDICT_MATCHES
    root = Path(__file__).parent
    return register_model_version(conn,ModelType.LINEUP,'recent-minutes-lineup',
        code_sha=current_code_sha(),configuration={
            'recent_matches':PREDICT_MATCHES,'keepers':1,'outfield':10,
            'availability_rule':'fixture-api-plus-active-manual',
            'source_digests':{n:hashlib.sha256((root/n).read_bytes()).hexdigest()
                              for n in ('player_ratings.py','availability.py','positions.py','lineup_snapshots.py')}},
        notes='Binary selection by recent minutes with score eligibility. No start probability model.')


def _identity(value):
    # First observation is retained; repeated fetch/observation clocks are not state changes.
    if isinstance(value,dict):
        return {k:_identity(v) for k,v in value.items() if k not in ('captured_at','observed_at','source_updated_at','seconds_to_kickoff')}
    if isinstance(value,list):
        return [_identity(v) for v in value]
    return value


def append(conn,table,payload):
    if table not in ('lineup_prediction_snapshots','official_lineup_snapshots'):
        raise ValueError('Unknown lineup snapshot table')
    config.require_db_write('append lineup evidence')
    payload = dict(payload)
    payload['content_hash']=hashlib.sha256(json.dumps(_identity(payload),sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()
    json_fields={'players','selection_inputs','availability'}
    values={k:json.dumps(v,allow_nan=False) if k in json_fields else v for k,v in payload.items()}
    placeholders=','.join(f'%({k})s'+('::jsonb' if k in json_fields else '') for k in values)
    conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",values)


def capture_predictions(conn,fixtures,lineups,selection_inputs,availability):
    from .player_ratings import line_of
    fixtures={f[0]:f for f in fixtures if f[4]}
    if not fixtures:
        return
    version=register_version(conn)
    grouped={}
    for fid,team,player,role,rating in lineups:
        grouped.setdefault((fid,team),[]).append({'player':player,'predicted_starter':True,
            'role':role,'line':line_of(role),'player_rating':rating,
            'availability_state':availability.get((fid,team),{}).get(player,{}).get('state','not_reported'),
            'start_probability':None})
    captured=datetime.now(timezone.utc)
    for fid,f in fixtures.items():
        for team in f[2:4]:
            append(conn,'lineup_prediction_snapshots',dict(fixture_id=fid,team_id=team,
                model_version_id=version,source='prospective' if captured<f[1] else 'late_observation',
                **snapshot_times(captured_at=captured,effective_at=f[1]),
                seconds_to_kickoff=(f[1]-captured).total_seconds(),
                players=grouped.get((fid,team),[]),selection_inputs=selection_inputs.get((fid,team),{}),
                availability=availability.get((fid,team),{})))


def capture_official(conn,fixture):
    captured=datetime.now(timezone.utc)
    kickoff=datetime.fromisoformat(fixture['fixture']['date'])
    for lineup in fixture.get('lineups') or []:
        team=(lineup.get('team') or {}).get('id')
        if not team or not lineup.get('startXI'):
            continue  # An omitted/empty response is not an official empty XI.
        players=[]
        for key,starter in [('startXI',True),('substitutes',False)]:
            for item in lineup.get(key) or []:
                p=item.get('player') or {}
                if p.get('id'):
                    role=positions.role(lineup.get('formation'),p.get('grid')) if starter else None
                    players.append({'player':p['id'],'starter':starter,'position':p.get('pos'),
                                    'grid':p.get('grid'),'role':role})
        append(conn,'official_lineup_snapshots',dict(fixture_id=fixture['fixture']['id'],team_id=team,
            source='api_football/fixtures',**snapshot_times(captured_at=captured,effective_at=kickoff),
            formation=lineup.get('formation'),players=sorted(players,key=lambda p:p['player'])))
