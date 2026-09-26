"""Offline weight counterfactuals. Never modifies production constants or database rows."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import gzip
import json
import math
from pathlib import Path
import random
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from thecornerfc import config, predictions
from thecornerfc.evaluation import probability_metrics

WEIGHTS=(0.,.2,.4,.5,.6,.7,.8,1.)
VALIDATION_START='2023-07-01'
TEST_START='2024-07-01'
RECENT_START='2025-07-01'
# Explicit known domestic tier relationships; never infer promotion from missing coverage.
TIERS={39:('England',1),40:('England',2),41:('England',3),42:('England',4),
       140:('Spain',1),141:('Spain',2),135:('Italy',1),136:('Italy',2),
       78:('Germany',1),79:('Germany',2),61:('France',1),62:('France',2),
       179:('Scotland',1),180:('Scotland',2)}


def counterfactual(row,weight):
    """Preserve the goal-rate product and every non-rank margin contribution.

    project() preserves base_home*base_away; thus stored final xG are sufficient
    to vary the margin without estimating or changing any other component.
    """
    h=weight*row['h_cur']+(1-weight)*row['h_lt']
    a=weight*row['a_cur']+(1-weight)*row['a_lt']
    margin=row['exp_diff']+((h-a)-(row['home_rank']-row['away_rank']))/100
    hx,ax=predictions.project(row['home_xg'],row['away_xg'],margin)
    return predictions.outcome_probabilities(hx,ax)[:3],hx-ax


def paired_ci(rows,weight,metric='loss',repeats=2000):
    blocks=defaultdict(list)
    for row in rows:
        blocks[row['week']].append(row['scores'][str(weight)][metric]-row['scores']['0.6'][metric])
    values=[(sum(v),len(v)) for v in blocks.values()]
    if len(values)<8:
        return {'weeks':len(values),'ci95':None,'reason':'fewer than 8 weekly blocks'}
    rng=random.Random(20260926)
    draws=[]
    for _ in range(repeats):
        sample=rng.choices(values,k=len(values))
        draws.append(sum(x for x,n in sample)/sum(n for x,n in sample))
    draws.sort()
    return {'weeks':len(values),'ci95':[draws[int(.025*repeats)],draws[int(.975*repeats)]],
            'method':'paired UTC ISO-week cluster bootstrap, 2000 draws; pointwise, not multiplicity-adjusted'}


def metrics(rows,weight):
    ps=[r['scores'][str(weight)]['p'] for r in rows]
    result=probability_metrics(ps,[r['y'] for r in rows])
    result['classwise_ece']=sum(sum(b['n']*abs(b['mean_probability']-b['observed_rate']) for b in bins.values()) for bins in result['calibration'].values())/(3*len(rows)) if rows else None
    result['goal_difference_mae']=sum(abs(r['scores'][str(weight)]['margin']-r['goal_difference']) for r in rows)/len(rows) if rows else None
    if rows:
        result['delta_log_loss_vs_0.6']=sum(r['scores'][str(weight)]['loss']-r['scores']['0.6']['loss'] for r in rows)/len(rows)
        result['delta_brier_vs_0.6']=sum(r['scores'][str(weight)]['brier']-r['scores']['0.6']['brier'] for r in rows)/len(rows)
    return result


def annotate(rows,membership,strict=False):
    excluded=defaultdict(int);accepted=[]
    for row in sorted(rows,key=lambda r:(r['kickoff'],r['fixture_id'])):
        if any(row.get(k) is None for k in ('h_cur','h_lt','a_cur','a_lt','home_xg','away_xg','exp_diff','hg','ag')):
            excluded['missing_components']+=1;continue
        if row['home_xg']<=0 or row['away_xg']<=0:
            excluded['nonpositive_xg']+=1;continue
        # Reconstructed rows must reproduce today's documented 0.6/0.4 baseline.
        # This avoids silently treating another historical formula as the control.
        if any(abs(row[k]-(.6*row[c]+.4*row[b]))>1e-5 for k,c,b in
               [('home_rank','h_cur','h_lt'),('away_rank','a_cur','a_lt')]):
            excluded['stored_rank_history_mismatch']+=1
            if strict:
                continue
        baseline,_=counterfactual(row,.6)
        if any(row[k] is None or abs(row[k]-p)>1e-6 for k,p in zip(('p_home','p_draw','p_away'),baseline)):
            excluded['stored_probability_formula_mismatch']+=1
            if strict:
                continue
        row['y']=0 if row['hg']>row['ag'] else 1 if row['hg']==row['ag'] else 2
        row['goal_difference']=row['hg']-row['ag']
        row['week']=row['kickoff'].strftime('%G-%V')
        row['scores']={}
        for weight in WEIGHTS:
            p,margin=counterfactual(row,weight)
            row['scores'][str(weight)]={'p':p,'margin':margin,'loss':-math.log(max(p[row['y']],1e-15)),
                'brier':sum((v-int(i==row['y']))**2 for i,v in enumerate(p))}
        segments=['all',f"competition:{row['league_id']}"]
        if max(abs(row['h_cur']-row['h_lt']),abs(row['a_cur']-row['a_lt']))>=100:
            segments.append('large_divergence_100_points')
        tier=TIERS.get(row['league_id'])
        for team in (row['home_team_id'],row['away_team_id']):
            prior=membership.get((team,row['season']-1),set())
            older=membership.get((team,row['season']-2),set())
            if tier:
                if any(TIERS.get(l,(None,None))[0]==tier[0] and TIERS[l][1]>tier[1] for l in prior):
                    segments.append('promoted')
                if any(TIERS.get(l,(None,None))[0]==tier[0] and TIERS[l][1]<tier[1] for l in prior):
                    segments.append('recently_relegated')
                if tier[1]==1 and row['league_id'] in prior and row['league_id'] in older:
                    segments.append('established_top_flight')
        # Exact early-season cohort is assigned from pre-match finished-fixture counts in SQL.
        if row['prior_home_games']<5 or row['prior_away_games']<5:
            segments.append('early_season_first_5')
        row['segments']=sorted(set(segments));accepted.append(row)
    return accepted,dict(excluded)


def read(conn):
    cur=conn.execute('''SELECT f.fixture_id,f.kickoff,f.league_id,f.season,f.home_team_id,f.away_team_id,
        f.home_goals AS hg,f.away_goals AS ag,p.home_rank,p.away_rank,p.exp_diff,p.home_xg,p.away_xg,
        p.p_home,p.p_draw,p.p_away,h.rank_before AS h_cur,h.lt_before AS h_lt,a.rank_before AS a_cur,a.lt_before AS a_lt,
        (SELECT count(*) FROM fixtures old WHERE old.league_id=f.league_id AND old.season=f.season
          AND old.kickoff<f.kickoff AND old.status_short IN ('FT','AET','PEN')
          AND (old.home_team_id=f.home_team_id OR old.away_team_id=f.home_team_id)) AS prior_home_games,
        (SELECT count(*) FROM fixtures old WHERE old.league_id=f.league_id AND old.season=f.season
          AND old.kickoff<f.kickoff AND old.status_short IN ('FT','AET','PEN')
          AND (old.home_team_id=f.away_team_id OR old.away_team_id=f.away_team_id)) AS prior_away_games
        FROM fixtures f JOIN fixture_predictions p USING(fixture_id)
        JOIN team_rank_history h ON h.fixture_id=f.fixture_id AND h.is_home
        JOIN team_rank_history a ON a.fixture_id=f.fixture_id AND NOT a.is_home
        WHERE f.status_short='FT' AND p.source='backfill' AND f.kickoff>=%s AND f.kickoff<now()
        ORDER BY f.kickoff,f.fixture_id''',[VALIDATION_START])
    names=[x.name for x in cur.description]
    rows=[dict(zip(names,r)) for r in cur.fetchall()]
    memberships=defaultdict(set)
    for team,season,league in conn.execute('SELECT team_id,season,league_id FROM team_seasons'):
        memberships[(team,season)].add(league)
    return rows,memberships


def report(rows,excluded):
    validation=[r for r in rows if r['kickoff'].date().isoformat()<TEST_START]
    test=[r for r in rows if r['kickoff'].date().isoformat()>=TEST_START]
    grid={str(w):metrics(validation,w) for w in WEIGHTS}
    selected=min(WEIGHTS,key=lambda w:grid[str(w)]['log_loss']) if validation else None
    result={'status':'complete' if validation and test else 'insufficient_visible_evidence',
        'evidence':'historical reconstruction only, not genuine prospective evaluation',
        'validation_window':[VALIDATION_START,TEST_START],'test_start':TEST_START,
        'weights':list(WEIGHTS),'validation_n':len(validation),'test_n':len(test),'input_diagnostics':excluded,
        'validation':grid,'validation_selected_weight':selected,
        'test':{str(w):{**metrics(test,w),'log_loss_uncertainty':paired_ci(test,w),
                       'brier_uncertainty':paired_ci(test,w,'brier')} for w in WEIGHTS},
        'test_periods':{label:{str(w):metrics([r for r in test if (r['kickoff'].date().isoformat()<RECENT_START)==early],w)
                              for w in WEIGHTS} for label,early in [('2024-25',True),('2025-onwards',False)]},
        'segments':{},'market_comparison':{'n':0,'status':'Not reconstructed from current odds; requires timestamped pre-event observations'},
        'practical_threshold':'Predeclared review threshold: held-out log-loss reduction >=0.002, corroborating Brier/calibration, uncertainty and period consistency; not automatic deployment.',
        'limitations':['Historical ranks, lineup and injury effects may have been rebuilt using later revisions.',
                      'Current rebuilt historical rank inputs are combined with frozen non-rank residual margin and xG product from stored historical predictions; not a pristine full historical replay.',
                      'Weekly bootstrap does not fully capture club/season dependence; subgroup intervals are exploratory.',
                      'Promotion cohorts require observed known tier transitions; no individual club tuning.']}
    for segment in sorted({s for r in test for s in r['segments']}):
        subset=[r for r in test if segment in r['segments']]
        validation_subset=[r for r in validation if segment in r['segments']]
        val_grid={str(w):metrics(validation_subset,w) for w in WEIGHTS}
        choice=min(WEIGHTS,key=lambda w:val_grid[str(w)]['log_loss']) if validation_subset else None
        result['segments'][segment]={'n':len(subset),'validation_n':len(validation_subset),
            'validation_selected_weight':choice,'grid':{str(w):metrics(subset,w) for w in WEIGHTS},
            'validation_selected_log_loss_ci':paired_ci(subset,choice) if choice is not None else None}
    team_examples={}
    for r in test:
        for team,cur,base in [(r['home_team_id'],r['h_cur'],r['h_lt']),(r['away_team_id'],r['a_cur'],r['a_lt'])]:
            example={'team_id':team,'fixture_id':r['fixture_id'],'kickoff':r['kickoff'],'current':cur,'baseline':base,'divergence':cur-base}
            if team not in team_examples or abs(cur-base)>abs(team_examples[team]['divergence']):
                team_examples[team]=example
    ordered=sorted(team_examples.values(),key=lambda r:r['divergence'])
    result['descriptive_divergence_examples']={'decliners':ordered[:3],'improvers':ordered[-3:]}
    result['recommendation']='Keep production unchanged pending genuine prospective confirmation; review validation-selected coefficient on held-out periods.' if selected is not None and test else 'No coefficient recommendation: insufficient accessible historical evidence. Keep production unchanged.'
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--use-database-url',action='store_true',help='Explicit authorization to use DATABASE_URL with a read-only transaction')
    parser.add_argument('--input-cache',help='Replay frozen local JSON.gz inputs without database access')
    parser.add_argument('--save-input',help='Freeze read-only query inputs to JSON.gz')
    parser.add_argument('--strict-compatible',action='store_true',help='Sensitivity: require exact stored 0.6 control compatibility')
    parser.add_argument('--output',default='experiments/strength_weight/results.json')
    args=parser.parse_args()
    import os,psycopg
    url=os.getenv('DATABASE_URL') if args.use_database_url else config.DATABASE_URL
    try:
        if args.input_cache:
            frozen=json.loads(gzip.decompress(Path(args.input_cache).read_bytes()))
            rows=frozen['rows']
            for row in rows:
                row['kickoff']=datetime.fromisoformat(row['kickoff'])
            memberships={(p,y):set(lgs) for p,y,lgs in frozen['membership']}
        else:
            with psycopg.connect(url,connect_timeout=15) as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                conn.execute("SET LOCAL statement_timeout='180s'")
                rows,memberships=read(conn)
        serialized=json.dumps({'rows':rows,'membership':[(p,y,sorted(lgs)) for (p,y),lgs in sorted(memberships.items())]},default=str,sort_keys=True)
        fingerprint=hashlib.sha256(serialized.encode()).hexdigest()
        if args.save_input:
            path=Path(args.save_input);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(gzip.compress(serialized.encode()))
        rows,excluded=annotate(rows,memberships,args.strict_compatible)
        result=report(rows,excluded)
        result['input_sha256']=fingerprint
        result['strict_compatible_sensitivity']=args.strict_compatible
        result['experiment_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result['generated_at']=datetime.now(timezone.utc).isoformat()
        Path(args.output).write_text(json.dumps(result,default=str,indent=2)+'\n')
        print(json.dumps({k:result[k] for k in ('status','validation_n','test_n','validation_selected_weight','recommendation')}))
    except psycopg.Error as exc:
        print(json.dumps({'error_type':type(exc).__name__,'sqlstate':exc.sqlstate,'message':'Read-only experiment failed; credentials omitted'}))
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
