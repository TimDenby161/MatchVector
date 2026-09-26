from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from thecornerfc import config, predictions, match_snapshots


class PredictionConnection:
    """Small SQL-boundary double: retain inserts and emulate conflict-do-nothing."""
    def __init__(self, kickoff):
        self.kickoff = kickoff
        self.strength = 1000.0
        self.snapshots = {}
        self.current = None
        self.commits = 0

    def execute(self, sql, params=None):
        if 'select fixture_id, kickoff' in sql:
            rows = [(1,self.kickoff,39,10,20)]
        elif 'select team_id, current_rank' in sql:
            rows = [(10,self.strength,950.,90.,1050.,1010.),(20,900.,920.,80.,930.,905.)]
        elif 'goal_base_home' in sql:
            rows = [(39,1.5,1.1)]
        elif 'predicted_gk' in sql:
            rows = [(1,10,60.,61.,62.,63.),(1,20,55.,56.,57.,58.)]
        elif 'starting_rank' in sql:
            rows = [(39,900.)]
        else:
            rows = [(39,10,20,2,1,1.7,0.8)]
        result = Mock()
        result.fetchall.return_value = rows
        result.__iter__ = Mock(return_value=iter(rows))
        return result

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self,*args):
        pass

    def executemany(self,sql,rows):
        if 'match_prediction_snapshots' in sql:
            if 'DO NOTHING' not in sql or 'DO UPDATE' in sql:
                raise AssertionError('Snapshot writes must never update')
            for row in rows:
                self.snapshots.setdefault(row['content_hash'],dict(row))
        else:
            self.current = rows[-1]

    def commit(self):
        self.commits += 1


class MatchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2030,1,1,tzinfo=timezone.utc)
        self.conn = PredictionConnection(self.clock+timedelta(days=2))
        for target,value in [('READ_ONLY',False),('GITHUB_ACTIONS',True)]:
            p=patch.object(config,target,value);p.start();self.addCleanup(p.stop)
        p=patch('thecornerfc.match_snapshots.register_version',return_value='mv_'+'a'*64)
        p.start();self.addCleanup(p.stop)
        p=patch('thecornerfc.predictions.missing_strengths',return_value={(1,10):0.2})
        p.start();self.addCleanup(p.stop)
        clock=self.clock
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):
                return clock
        p=patch('thecornerfc.predictions.datetime',Frozen)
        p.start();self.addCleanup(p.stop)

    def test_later_model_run_preserves_original_and_deduplicates_retry(self):
        predictions.update_predictions(self.conn)
        original=dict(next(iter(self.conn.snapshots.values())))
        predictions.update_predictions(self.conn)
        self.assertEqual(len(self.conn.snapshots),1)
        self.conn.strength=1100.
        predictions.update_predictions(self.conn)
        self.assertEqual(len(self.conn.snapshots),2)
        self.assertEqual(self.conn.snapshots[original['content_hash']],original)
        self.assertNotEqual(self.conn.current[10],original['p_home'])
        self.assertEqual(original['source'],'prospective')
        self.assertEqual(original['seconds_to_kickoff'],172800)

    def test_stored_components_reproduce_original_prediction(self):
        predictions.update_predictions(self.conn)
        row=next(iter(self.conn.snapshots.values()))
        inputs=json.loads(row['inputs'])
        result=predictions.predict_match(inputs['home_match_rank'],inputs['away_match_rank'],
            inputs['home_records'],inputs['away_records'],inputs['league_home_goals'],inputs['league_away_goals'],
            row['league_id'],inputs['home_missing'] or 0.,inputs['away_missing'] or 0.,
            inputs['sides'],inputs['predicted_lines'])
        self.assertEqual(tuple(row[k] for k in ('exp_diff','home_xg','away_xg','p_home','p_draw','p_away',
                                               'likely_score','p_over25','p_btts')),result)

    def test_late_predictions_are_not_prospective(self):
        self.conn.kickoff=self.clock-timedelta(minutes=1)
        predictions.update_predictions(self.conn)
        self.assertEqual(next(iter(self.conn.snapshots.values()))['source'],'late_observation')

    def test_reconstruction_identity_separate_and_capture_clock_not_backdated(self):
        predictions.update_predictions(self.conn)
        first=next(iter(self.conn.snapshots.values()))
        rebuilt=match_snapshots.make_snapshot(self.conn.current,json.loads(first['inputs']),
            version_id=first['model_version_id'],captured_at=self.clock+timedelta(days=10),
            reference_at=self.conn.kickoff,source='reconstruction')
        self.assertEqual(rebuilt['source'],'reconstruction')
        self.assertNotEqual(first['content_hash'],rebuilt['content_hash'])
        self.assertGreater(rebuilt['captured_at'],rebuilt['effective_at'])

    def test_failed_snapshot_write_does_not_commit_current_predictions(self):
        with patch('thecornerfc.match_snapshots.append_snapshots',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                predictions.update_predictions(self.conn)
        self.assertEqual(self.conn.commits,0)

    def test_schema_contains_migration(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn((root/'db/migrations/20260926_match_prediction_snapshots.sql').read_text(),
                      (root/'db/schema.sql').read_text())

    def test_backfill_path_writes_only_reconstruction(self):
        conn=self.conn
        kickoff=self.clock-timedelta(days=10)
        history=[(1,None,None,None,home,None,1000.,None,950.,10.,5.,1.5) for home in (True,False)]
        with patch.object(conn,'execute',return_value=[(1,)]), \
             patch('thecornerfc.predictions.rank_history',return_value=history), \
             patch('thecornerfc.predictions._predicted_lines',return_value={}), \
             patch('thecornerfc.predictions.finished_fixtures',return_value=[(1,kickoff,39,None,10,20,2,1,None,1.7,0.8)]):
            predictions.backfill_predictions(conn)
        row=next(iter(conn.snapshots.values()))
        self.assertEqual(row['source'],'reconstruction')
        self.assertEqual(row['captured_at'],self.clock.isoformat())
        self.assertEqual(row['effective_at'],kickoff.isoformat())
