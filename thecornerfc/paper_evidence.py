"""Append-only price observations, paper decisions and later outcome attachments."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import config
from .model_versions import ModelType, current_code_sha, register_model_version


def record_odds(conn, rows, captured_at, kickoffs):
    if not rows:
        return
    config.require_db_write('record odds history')
    # Workflows are already serialized; fixture locks also make adjacent-state dedup safe
    # for concurrent local ingestion. A -> B -> A is three observations, not two.
    for fixture in sorted({r['fixture_id'] for r in rows}):
        conn.execute('SELECT pg_advisory_xact_lock(%s,%s)', [73191,fixture])
    with conn.cursor() as cur:
        cur.executemany('''INSERT INTO odds_observations
            (fixture_id,bookmaker_id,market_id,selection,odds,captured_at,effective_at,provider_updated_at,source)
            SELECT %(fixture_id)s,%(bookmaker_id)s,%(bet_id)s,%(selection)s,%(odd)s,
                   %(captured_at)s,%(effective_at)s,%(api_updated_at)s,'api_football/odds'
            WHERE NOT EXISTS (
              SELECT 1 FROM (SELECT odds,provider_updated_at,source,effective_at FROM odds_observations
                WHERE fixture_id=%(fixture_id)s AND bookmaker_id=%(bookmaker_id)s
                  AND market_id=%(bet_id)s AND selection=%(selection)s
                ORDER BY captured_at DESC,observation_id DESC LIMIT 1) previous
              WHERE previous.odds=%(odd)s AND previous.provider_updated_at IS NOT DISTINCT FROM %(api_updated_at)s::timestamptz
                AND previous.effective_at IS NOT DISTINCT FROM %(effective_at)s::timestamptz
                AND previous.source='api_football/odds')''',
            [{**r,'captured_at':captured_at,'effective_at':kickoffs.get(r['fixture_id'])}
             for r in rows if r['odd'] is not None and r['odd'] > 1])


def strategy_version(conn, strategy):
    from . import betting as b, predictions as p
    settings={k:getattr(b,k) for k in ('MIN_EDGE','MAX_ODDS','BOOKMAKER','EARLY_HOURS','LATE_MINUTES',
                                     'STREAK_POINTS','THIN_DATA_GAMES','BIG_GAP')}
    settings.update(strategy=strategy,stake_units=1,group=b.GROUP,
        big5=sorted(b.BIG5),europe=sorted(b.EUROPE),
        goal_grid={name:getattr(p,name) for name in ('MAX_GOALS','OVER25_BASE','OVER25_SHRINK','BTTS_BASE','BTTS_SHRINK')},
        markets={k:[v[0],list(v[1])] for k,v in b.MARKETS.items()},
        goal_line_calibration={str(k):list(v) for k,v in p.GOAL_LINE_CALIBRATION.items()},
        source_digests={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in ('betting.py','predictions.py','paper_evidence.py')})
    return register_model_version(conn,ModelType.BETTING,'paper-selection',code_sha=current_code_sha(),
                                  configuration=settings,notes='Existing selection rules; immutable decision capture.')


def latest_quotes(conn, fixture, market_id, cutoff, *, strict=False):
    # Captured time, never provider time, determines what was observable at the cutoff.
    op = '<' if strict else '<='
    return conn.execute(f'''SELECT DISTINCT ON (bookmaker_id,selection)
        observation_id,bookmaker_id,selection,odds,captured_at FROM odds_observations
        WHERE fixture_id=%s AND market_id=%s AND captured_at {op} %s
        ORDER BY bookmaker_id,selection,captured_at DESC,observation_id DESC''',
        [fixture,market_id,cutoff]).fetchall()


def market_evidence(books, selections, bookmaker):
    complete=[]
    margin=None
    for bm,prices in books.items():
        if all(sel in prices for sel in selections):
            total=sum(1/prices[s] for s in selections)
            complete.append({s:(1/prices[s])/total for s in selections})
            if bm==bookmaker:
                margin=total-1
    fair={s:sum(p[s] for p in complete)/len(complete) for s in selections} if complete else {}
    return fair,margin


def record_decision(conn, bet_id, row, market, pred, decision_at, version, selection_context=None):
    from .betting import MARKETS
    config.require_db_write('record paper decision')
    strategy,fid,league,kickoff,name,sel,prob,fair,odd,bm,edge,tags=row
    # Exact consumed probabilities/xG; never guess that the newest snapshot matches.
    snapshot=conn.execute('''SELECT snapshot_id,model_version_id FROM match_prediction_snapshots
        WHERE fixture_id=%s AND effective_at=%s AND source='prospective'
          AND captured_at<=%s AND created_at<=%s
          AND p_home=%s AND p_draw=%s AND p_away=%s AND p_over25=%s AND p_btts=%s
          AND home_xg=%s AND away_xg=%s ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1''',
        [fid,kickoff,decision_at,decision_at,*pred[3:10]]).fetchone()
    if snapshot is None:
        raise RuntimeError('No exact prospective prediction snapshot for paper decision; run predict before placement')
    market_id,selections,_=MARKETS[name]
    quotes=latest_quotes(conn,fid,market_id,decision_at)
    refs={}
    for oid,book,selection,price,captured in quotes:
        if market['books'].get(book,{}).get(selection)==float(price):
            refs.setdefault(str(book),{})[selection]={'observation_id':oid,'captured_at':captured.isoformat()}
    _,margin=market_evidence(market['books'],selections,bm)
    chosen=refs.get(str(bm),{}).get(sel,{}).get('observation_id')
    evidence={'books':market['books'],'fair':market['fair'],'selections':list(selections),
              'quote_references':refs,'prediction_values':list(pred[3:10]),'tags':tags,
              'selection_context':selection_context,'decided_before_known_kickoff':decision_at<kickoff,
              'odds_provenance':'recorded_observation' if chosen else 'legacy_current_price_without_history',
              'fair_method':'mean of complete bookmaker markets after proportional normalization'}
    conn.execute('''INSERT INTO paper_decisions
        (paper_bet_id,fixture_id,strategy,strategy_version_id,model_version_id,prediction_snapshot_id,
         captured_at,effective_at,market,market_id,selection,bookmaker_id,model_probability,
         raw_implied_probability,fair_probability,odds_taken,market_margin,edge,edge_formula,
         stake_units,odds_observation_id,evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s::jsonb)''',
        [bet_id,fid,strategy,version,snapshot[1],snapshot[0],decision_at,kickoff,name,market_id,sel,bm,prob,
         1/odd,fair,odd,margin,edge,'model_probability * odds_taken - 1',chosen,json.dumps(evidence,allow_nan=False)])


def movement(odds_taken, closing_odds, fair_at_decision, closing_fair):
    return (odds_taken/closing_odds-1 if closing_odds else None,
            closing_fair-fair_at_decision if closing_fair is not None and fair_at_decision is not None else None)


def attach_outcome(conn, bet_id, result, profit, status, home_goals, away_goals):
    row=conn.execute('''SELECT decision_id,fixture_id,market_id,selection,bookmaker_id,
        odds_taken,fair_probability,effective_at,evidence FROM paper_decisions WHERE paper_bet_id=%s''',[bet_id]).fetchone()
    if row is None:
        return # Legacy decisions cannot be reconstructed as captured evidence.
    config.require_db_write('attach paper outcome')
    did,fid,mid,sel,bm,taken,decision_fair,kickoff,evidence=row
    quotes=latest_quotes(conn,fid,mid,kickoff,strict=True)
    books={}
    for oid,book,selection,odd,captured in quotes:
        if selection in evidence['selections']:
            books.setdefault(book,{})[selection]=float(odd)
    fair,margin=market_evidence(books,evidence['selections'],bm)
    closing=books.get(bm,{}).get(sel)
    closing_fair=fair.get(sel)
    price_clv,prob_move=movement(float(taken),closing,decision_fair,closing_fair)
    details={'quotes':[{'observation_id':oid,'bookmaker':book,'selection':selection,
                       'odds':float(odd),'captured_at':captured.isoformat()} for oid,book,selection,odd,captured in quotes],
             'books':books,'market_margin':margin,'cutoff':kickoff.isoformat(),
             'status':status,'home_goals':home_goals,'away_goals':away_goals,
             'closing_policy':'latest observation strictly before decision-time kickoff; no freshness guarantee'}
    digest=hashlib.sha256(json.dumps([details,result,profit],sort_keys=True,allow_nan=False).encode()).hexdigest()
    conn.execute('''INSERT INTO paper_outcomes (decision_id,captured_at,source,closing_odds,
        closing_fair_probability,price_clv,probability_movement,result,profit_units,evidence,content_hash)
        VALUES (%s,%s,'last_observed_pre_kickoff',%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        ON CONFLICT (decision_id,content_hash) DO NOTHING''',
        [did,datetime.now(timezone.utc),closing,closing_fair,price_clv,prob_move,result,profit,json.dumps(details),digest])
