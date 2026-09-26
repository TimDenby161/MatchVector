from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from thecornerfc import betting, config, paper_evidence


class PaperEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.kickoff=self.now+timedelta(hours=2)
        for name,val in [('READ_ONLY',False),('GITHUB_ACTIONS',True)]:
            p=patch.object(config,name,val);p.start();self.addCleanup(p.stop)

    def test_both_movement_definitions_and_missing_data(self):
        price,prob=paper_evidence.movement(2.5,2.,.4,.45)
        self.assertEqual(price,.25)
        self.assertAlmostEqual(prob,.05)
        self.assertEqual(paper_evidence.movement(2.,None,None,.5),(None,None))
        self.assertEqual(paper_evidence.movement(2.,2.,0.,0.),(0.,0.))

    def test_fair_market_and_overround_match_current_policy(self):
        books={8:{'Home':2.,'Draw':3.,'Away':4.},9:{'Home':2.2,'Draw':3.2,'Away':3.8},10:{'Home':2.}}
        fair,margin=paper_evidence.market_evidence(books,['Home','Draw','Away'],8)
        total=1/2+1/3+1/4
        self.assertAlmostEqual(margin,total-1)
        self.assertAlmostEqual(sum(fair.values()),1)
        self.assertAlmostEqual(fair['Home'],((.5/total)+(1/2.2)/(1/2.2+1/3.2+1/3.8))/2)

    def test_decision_saves_inputs_and_exact_snapshot_reference(self):
        conn=Mock()
        conn.execute.return_value.fetchone.return_value=(44,'mv_match')
        pred=(1,39,self.kickoff,.6,.2,.2,.5,.5,1.7,.9)
        row=['early',1,39,self.kickoff,'1X2','Home',.6,.45,2.,8,.2,['big5']]
        market={'books':{8:{'Home':2.,'Draw':3.,'Away':4.}},'fair':{'Home':.45}}
        with patch('thecornerfc.paper_evidence.latest_quotes',return_value=[(9,8,'Home',2.,self.now)]):
            paper_evidence.record_decision(conn,5,row,market,pred,self.now,'mv_strategy',
                selection_context={'candidates':[row], 'market_states':{'1X2':market}})
        sql,values=conn.execute.call_args.args
        self.assertIn('INSERT INTO paper_decisions',sql)
        self.assertNotIn('UPDATE',sql)
        self.assertEqual(values[4:6],['mv_match',44])
        self.assertEqual(values[13],.5)
        self.assertEqual(values[19],9)
        evidence=json.loads(values[-1])
        self.assertEqual(evidence['books']['8']['Away'],4.)
        self.assertEqual(evidence['quote_references']['8']['Home']['observation_id'],9)
        self.assertEqual(evidence['selection_context']['candidates'][0][3],self.kickoff.isoformat())
        self.assertEqual(evidence['selection_context']['candidates'][0][6],.6)

    def test_evidence_serialization_rejects_unknown_types_and_naive_times(self):
        with self.assertRaises(TypeError):
            json.dumps({'value':object()},default=paper_evidence._evidence_json_default)
        with self.assertRaises(ValueError):
            json.dumps({'captured_at':datetime(2026,1,1)},default=paper_evidence._evidence_json_default)

    def test_missing_exact_prediction_fails_instead_of_claiming_provenance(self):
        conn=Mock()
        conn.execute.return_value.fetchone.return_value=None
        row=['early',1,39,self.kickoff,'1X2','Home',.6,.45,2.,8,.2,[]]
        with self.assertRaises(RuntimeError):
            paper_evidence.record_decision(conn,1,row,{},(1,39,self.kickoff,.6,.2,.2,.5,.5,1.7,.9),self.now,'version')
        self.assertEqual(conn.execute.call_count,1)

    def test_closing_attaches_separate_evidence_and_uses_pre_event_cutoff(self):
        conn=Mock()
        conn.execute.return_value.fetchone.return_value=(4,1,1,'Home',8,2.5,.4,self.kickoff,
                                                       {'selections':['Home','Draw','Away']})
        quotes=[(1,8,'Home',2.,self.now),(2,8,'Draw',3.,self.now),(3,8,'Away',4.,self.now)]
        with patch('thecornerfc.paper_evidence.latest_quotes',return_value=quotes) as loader:
            paper_evidence.attach_outcome(conn,5,'win',1.5,'FT',2,0)
        loader.assert_called_once_with(conn,1,1,self.kickoff,strict=True)
        sql,args=conn.execute.call_args.args
        self.assertIn('INSERT INTO paper_outcomes',sql)
        self.assertNotIn('UPDATE',sql)
        self.assertEqual(args[4],.25)
        self.assertAlmostEqual(args[5],(.5/(.5+1/3+.25))-.4)
        self.assertEqual(args[6:8],['win',1.5])

    def test_place_bets_keeps_rules_and_records_only_inserted_bets(self):
        conn=Mock()
        prediction=(1,39,self.kickoff,.6,.2,.2,.5,.5,1.7,.9)
        # predictions, already-taken groups, actual INSERT RETURNING
        conn.execute.side_effect=[Mock(fetchall=Mock(return_value=[prediction])),[],Mock(fetchone=Mock(return_value=(99,)))]
        market={'best':{'Home':(2.,8),'Draw':(3.,8),'Away':(4.,8)},
                'fair':{'Home':.45,'Draw':.3,'Away':.25},'books':{8:{'Home':2.,'Draw':3.,'Away':4.}}}
        with patch('thecornerfc.betting.load_prices',return_value={1:{'1X2':market}}), \
             patch('thecornerfc.betting.match_tags',return_value={}), \
             patch('thecornerfc.paper_evidence.strategy_version',return_value='strategy'), \
             patch('thecornerfc.paper_evidence.record_decision') as record:
            self.assertEqual(betting.place_bets(conn,'early',timedelta(hours=36)),1)
        self.assertEqual(record.call_args.args[2][5],'Home')
        self.assertIn('selection_context',record.call_args.kwargs)
        conn.commit.assert_called_once()
        self.assertTrue(conn.execute.call_args_list[0].kwargs['binary'])

    def test_schema_contains_migration(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn((root/'db/migrations/20260926_odds_paper_evidence.sql').read_text(),(root/'db/schema.sql').read_text())


@unittest.skipUnless(os.getenv('MODEL_VERSION_TEST_DSN'),'Requires disposable PostgreSQL test database')
class OddsPostgresTests(unittest.TestCase):
    def test_price_reversions_dedup_and_immutable_decisions(self):
        import psycopg
        import uuid
        from thecornerfc.model_versions import register_model_version
        from thecornerfc.match_snapshots import make_snapshot, append_snapshots
        root=Path(__file__).resolve().parents[1]/'db/migrations'
        with psycopg.connect(os.environ['MODEL_VERSION_TEST_DSN']) as conn:
            try:
                schema='odds_test_'+uuid.uuid4().hex
                conn.execute(f'CREATE SCHEMA {schema}')
                conn.execute(f'SET LOCAL search_path TO {schema}')
                conn.execute('CREATE TABLE paper_bets (bet_id bigint PRIMARY KEY)')
                conn.execute('INSERT INTO paper_bets VALUES (1)')
                for name in ('20260926_model_versions.sql','20260926_match_prediction_snapshots.sql','20260926_odds_paper_evidence.sql'):
                    conn.execute((root/name).read_text())
                conn.execute((root/'20260926_odds_paper_evidence.sql').read_text())
                now=datetime.now(timezone.utc)
                kickoff=now+timedelta(hours=2)
                quote=dict(fixture_id=1,bookmaker_id=8,bet_id=1,selection='Home',odd=2.,api_updated_at=now)
                with patch.object(config,'READ_ONLY',False),patch.object(config,'GITHUB_ACTIONS',True):
                    for price in (2.,2.,2.2,2.):
                        paper_evidence.record_odds(conn,[{**quote,'odd':price}],now,{1:kickoff})
                    self.assertEqual(conn.execute('SELECT count(*) FROM odds_observations').fetchone()[0],3)
                    for price in (3.,4.):
                        paper_evidence.record_odds(conn,[{**quote,'selection':'Draw' if price==3. else 'Away','odd':price}],now,{1:kickoff})
                    model=register_model_version(conn,'match','test')
                    strategy=register_model_version(conn,'betting','test')
                    row=(1,kickoff,39,10,20,1000.,900.,.5,1.7,.9,.6,.2,.2,'1-0',.5,.5)
                    append_snapshots(conn,[make_snapshot(row,{},version_id=model,captured_at=now,reference_at=now,source='prospective')])
                    market={'books':{8:{'Home':2.,'Draw':3.,'Away':4.}},'fair':{'Home':.45}}
                    decision=['early',1,39,kickoff,'1X2','Home',.6,.45,2.,8,.2,[]]
                    paper_evidence.record_decision(conn,1,decision,market,(1,39,kickoff,.6,.2,.2,.5,.5,1.7,.9),datetime.now(timezone.utc),strategy)
                    original=conn.execute('SELECT evidence FROM paper_decisions').fetchone()[0]
                    paper_evidence.attach_outcome(conn,1,'win',1.,'FT',2,0)
                    paper_evidence.attach_outcome(conn,1,'win',1.,'FT',2,0)
                    self.assertEqual(conn.execute('SELECT count(*) FROM paper_outcomes').fetchone()[0],1)
                    self.assertEqual(conn.execute('SELECT evidence FROM paper_decisions').fetchone()[0],original)
                for table in ('odds_observations','paper_decisions','paper_outcomes'):
                    for sql in (f'UPDATE {table} SET captured_at=now()',f'DELETE FROM {table}',f'TRUNCATE {table} CASCADE'):
                        with self.assertRaises(psycopg.errors.RaiseException):
                            with conn.transaction():
                                conn.execute(sql)
            finally:
                conn.rollback()
