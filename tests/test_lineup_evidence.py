from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from thecornerfc import availability, config, lineup_snapshots
from thecornerfc.player_ratings import _select_lineup


class EvidenceConnection:
    def __init__(self):
        self.rows={}
    def execute(self,sql,values):
        assert 'ON CONFLICT DO NOTHING' in sql
        assert 'UPDATE' not in sql
        key=(sql.split()[2],values['content_hash'])
        self.rows.setdefault(key,dict(values))


class LineupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.kickoff=self.now+timedelta(days=2)
        self.conn=EvidenceConnection()
        for name,val in [('READ_ONLY',False),('GITHUB_ACTIONS',True)]:
            p=patch.object(config,name,val);p.start();self.addCleanup(p.stop)

    def merge(self,api=(),manual=(),**kwargs):
        return availability.merge(api,manual,kickoff=kwargs.get('kickoff',self.kickoff),
                                   observed_at=self.now,upcoming=kwargs.get('upcoming',True))

    def test_merge_keeps_both_sources_and_preserves_doubtful_exclusion(self):
        merged=self.merge([{'player':1,'type':'Doubtful','reason':'Knock'},
                           {'player':2,'type':'Missing Fixture','reason':'Suspended'}],
                          [{'player':1,'reason':'Manual injury'}])
        self.assertEqual(len(merged[1]['evidence']),2)
        self.assertTrue(merged[1]['excluded'])
        self.assertEqual(merged[2]['state'],'suspended')
        self.assertEqual(self.merge([{'player':1,'type':'Doubtful'}])[1]['state'],'doubtful')

    def test_manual_dates_are_event_scoped_and_do_not_rewrite_history(self):
        self.assertFalse(self.merge(manual=[{'player':1,'until':self.now.date().isoformat()}]))
        self.assertFalse(self.merge(manual=[{'player':1}],upcoming=False))
        self.assertFalse(self.merge(manual=[{'player':1}],kickoff=self.now-timedelta(days=1)))
        self.assertIn(1,self.merge(manual=[{'player':1,'until':self.kickoff.date().isoformat()}]))

    def test_manual_absence_changes_exclusion_not_selection_algorithm(self):
        minutes=Counter({p:1000-p for p in range(1,15)})
        def score(p,kickoff):
            return (1.,900.), 'GK' if p in (1,2) else 'CM',100
        baseline=_select_lineup(minutes,set(),score,self.kickoff)[2]
        resolved=self.merge(manual=[{'player':1,'reason':'Injury'},{'player':3,'reason':'Suspended'}])
        selected=_select_lineup(minutes,set(resolved),score,self.kickoff)[2]
        self.assertEqual(len(baseline),11)
        self.assertEqual(len(selected),11)
        self.assertEqual(selected[0][0],2)
        self.assertNotIn(3,[row[0] for row in selected])
        self.assertEqual([row[0] for row in baseline],[1]+list(range(3,13)))

    @patch('thecornerfc.lineup_snapshots.register_version',return_value='mv_'+'a'*64)
    def test_old_prediction_and_availability_survive_new_capture(self,version):
        fixtures=[(1,self.kickoff,10,20,True)]
        states={(1,10):self.merge(manual=[{'player':2,'reason':'Injury'}])}
        lineup_snapshots.capture_predictions(self.conn,fixtures,[(1,10,1,'CM',65.)],{},states)
        old=list(self.conn.rows.values())[0].copy()
        lineup_snapshots.capture_predictions(self.conn,fixtures,[(1,10,1,'CM',65.)],{},states)
        self.assertEqual(len(self.conn.rows),2) # both teams, including empty prediction
        lineup_snapshots.capture_predictions(self.conn,fixtures,[(1,10,2,'CM',70.)],{}, {})
        self.assertEqual(len(self.conn.rows),3)
        self.assertEqual(list(self.conn.rows.values())[0],old)
        player=json.loads(old['players'])[0]
        self.assertTrue(player['predicted_starter'])
        self.assertIsNone(player['start_probability'])
        self.assertEqual(player['availability_state'],'not_reported')
        self.assertEqual(json.loads(old['availability'])['2']['state'],'unavailable')

    def test_official_corrections_append_and_empty_responses_do_not_invent_xi(self):
        fixture={'fixture':{'id':1,'date':self.kickoff.isoformat()},'lineups':[
            {'team':{'id':10},'formation':'4-4-2','startXI':[{'player':{'id':1,'grid':'1:1','pos':'G'}}]}]}
        lineup_snapshots.capture_official(self.conn,fixture)
        first=list(self.conn.rows.values())[0].copy()
        lineup_snapshots.capture_official(self.conn,fixture)
        self.assertEqual(len(self.conn.rows),1)
        fixture['lineups'][0]['startXI'][0]['player']['id']=2
        lineup_snapshots.capture_official(self.conn,fixture)
        self.assertEqual(len(self.conn.rows),2)
        self.assertEqual(list(self.conn.rows.values())[0],first)
        self.assertEqual(first['source'],'api_football/fixtures')
        fixture['lineups'][0]['startXI']=[]
        lineup_snapshots.capture_official(self.conn,fixture)
        self.assertEqual(len(self.conn.rows),2)

    def test_migration_in_schema(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn((root/'db/migrations/20260926_lineup_snapshots.sql').read_text(),
                      (root/'db/schema.sql').read_text())


# Run only against an explicitly supplied disposable PostgreSQL test database.
import os
import uuid


@unittest.skipUnless(os.getenv('MODEL_VERSION_TEST_DSN'), 'Requires disposable PostgreSQL test database')
class LineupPostgresTests(unittest.TestCase):
    def test_append_only_tables_preserve_old_evidence(self):
        import psycopg
        from thecornerfc.model_versions import register_model_version
        root=Path(__file__).resolve().parents[1]/'db/migrations'
        with psycopg.connect(os.environ['MODEL_VERSION_TEST_DSN']) as conn:
            try:
                schema='lineup_test_'+uuid.uuid4().hex
                conn.execute(f'CREATE SCHEMA {schema}')
                conn.execute(f'SET LOCAL search_path TO {schema}')
                conn.execute((root/'20260926_model_versions.sql').read_text())
                migration=(root/'20260926_lineup_snapshots.sql').read_text()
                conn.execute(migration)
                conn.execute(migration)
                now=datetime.now(timezone.utc)
                kickoff=now+timedelta(days=1)
                with patch.object(config,'READ_ONLY',False),patch.object(config,'GITHUB_ACTIONS',True):
                    version=register_model_version(conn,'lineup','test')
                    with patch('thecornerfc.lineup_snapshots.register_version',return_value=version):
                        fixtures=[(1,kickoff,10,20,True)]
                        lineup_snapshots.capture_predictions(conn,fixtures,[(1,10,1,'CM',60.)],{}, {})
                        lineup_snapshots.capture_predictions(conn,fixtures,[(1,10,2,'CM',65.)],{}, {})
                    fixture={'fixture':{'id':1,'date':kickoff.isoformat()},'lineups':[
                        {'team':{'id':10},'formation':'4-4-2','startXI':[{'player':{'id':1,'grid':'1:1'}}]}]}
                    lineup_snapshots.capture_official(conn,fixture)
                    fixture['lineups'][0]['startXI'][0]['player']['id']=2
                    lineup_snapshots.capture_official(conn,fixture)
                self.assertEqual(conn.execute('SELECT count(*) FROM lineup_prediction_snapshots').fetchone()[0],3)
                self.assertEqual(conn.execute('SELECT count(*) FROM official_lineup_snapshots').fetchone()[0],2)
                self.assertEqual(conn.execute('SELECT players FROM lineup_prediction_snapshots ORDER BY snapshot_id LIMIT 1').fetchone()[0][0]['player'],1)
                for table in ('lineup_prediction_snapshots','official_lineup_snapshots'):
                    for sql in (f'UPDATE {table} SET team_id=99',f'DELETE FROM {table}',f'TRUNCATE {table}'):
                        with self.assertRaises(psycopg.errors.RaiseException):
                            with conn.transaction():
                                conn.execute(sql)
            finally:
                conn.rollback()
