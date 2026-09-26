"""Match-specific immutable capture; calculations remain in predictions.py."""
import hashlib
import json
from pathlib import Path

from . import config
from .model_versions import ModelType, current_code_sha, register_model_version, snapshot_times


def register_version(conn):
    from . import predictions as p
    names = ('SHRINK_GAMES','DRAW_INFLATION','DRAW_FADE_MARGIN','EUROPE_HOME_BONUS',
             'MATCH_RANK_NOW_TODAY','MATCH_RANK_NOW_YEAR','AD_GOALS_WEIGHT','HOME_EDGE_WEIGHT',
             'MAX_GOALS','DEFAULT_HOME_GOALS','DEFAULT_AWAY_GOALS','OVER25_BASE','OVER25_SHRINK',
             'BTTS_BASE','BTTS_SHRINK','HOME_ADVANTAGE_POINTS','DEFAULT_STARTING_RANK','INJURY_BETA')
    settings = {name: getattr(p,name) for name in names}
    settings.update(EUROPE_COMPS=sorted(p.EUROPE_COMPS), XI_LINE_WEIGHTS=list(p.XI_LINE_WEIGHTS),
                    INJURY_MODEL_LEAGUES=list(config.INJURY_MODEL_LEAGUES), history_days=365)
    # Retain identity even with a dirty/unknown Git SHA; no credentials or environment dump.
    root = Path(__file__).parent
    settings['source_digests'] = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                                 for name in ('predictions.py','injuries.py','ranking.py','player_ratings.py','match_snapshots.py')}
    return register_model_version(conn, ModelType.MATCH, 'match-prediction',
                                  code_sha=current_code_sha(), configuration=settings,
                                  notes='Actual prediction inputs preserved per snapshot; upstream version IDs unavailable.')


def make_snapshot(row, inputs, *, version_id, captured_at, reference_at, source):
    if source not in ('prospective','reconstruction','late_observation'):
        raise ValueError('Unknown snapshot source')
    fid,kickoff,league,home,away,h_rank,a_rank,*output = row
    exp_diff,hx,ax,ph,pd,pa,score,over,btts = output[:9]
    times = snapshot_times(captured_at=captured_at,effective_at=kickoff)
    if source == 'prospective' and captured_at >= kickoff:
        source = 'late_observation'
    values = dict(fixture_id=fid,league_id=league,home_team_id=home,away_team_id=away,
                  model_version_id=version_id,source=source,**times,
                  model_reference_at=reference_at.isoformat(),
                  seconds_to_kickoff=(kickoff-captured_at).total_seconds(),
                  exp_diff=exp_diff,home_xg=hx,away_xg=ax,p_home=ph,p_draw=pd,p_away=pa,
                  likely_score=score,p_over25=over,p_btts=btts,
                  inputs={**inputs,'home_match_rank':h_rank,'away_match_rank':a_rank})
    # Observation time alone does not create duplicates; actual changed inputs/output do.
    identity = {k:v for k,v in values.items() if k not in
                ('captured_at','model_reference_at','seconds_to_kickoff')}
    values['content_hash'] = hashlib.sha256(json.dumps(identity,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()
    values['inputs'] = json.dumps(values['inputs'],allow_nan=False)
    return values


def append_snapshots(conn, rows):
    if not rows:
        return
    config.require_db_write('append match prediction snapshots')
    columns = tuple(rows[0])
    placeholders = ','.join(f'%({key})s' + ('::jsonb' if key=='inputs' else '') for key in columns)
    with conn.cursor() as cur:
        cur.executemany(f'''INSERT INTO match_prediction_snapshots ({','.join(columns)})
            VALUES ({placeholders}) ON CONFLICT (fixture_id,model_version_id,source,content_hash)
            DO NOTHING''', rows)
