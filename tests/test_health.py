import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from thecornerfc import config, health, usage
from thecornerfc.export import _publish_export, ExportValidationError
from test_export_safety import write_valid_export


render_health = health.publish


class HealthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in {'API_LEDGER_PATH': str(self.root/'ledger.db'),
                            'HEALTH_MIN_BASELINE':100, 'HEALTH_COLLAPSE_RATIO':0.2,
                            'HEALTH_STALE_HOURS':72}.items():
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('thecornerfc.health.publish')
        p.start()
        self.addCleanup(p.stop)

    def test_failure_records_stage_and_keeps_success_baseline(self):
        health.save_dataset('players', 'HEALTHY', 1000)
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (10, None)
        @health.monitored('players')
        def stage(conn):
            pass
        with self.assertRaises(health.HealthFailure):
            with health.pipeline('sync players'):
                stage(conn)
        with health.store() as db:
            self.assertEqual(db.execute('SELECT status,failed_stage FROM pipeline_runs').fetchone(), ('FAIL','players'))
            row = db.execute('SELECT status,row_count,successful_row_count,last_success,message FROM dataset_status').fetchone()
            self.assertEqual(row[:3], ('FAIL',10,1000))
            self.assertIsNotNone(row[3])
            self.assertIn('collapsed',row[4])

    def test_later_success_cannot_hide_warning_or_failure(self):
        with health.pipeline('nightly'):
            health.save_dataset('injuries','WARNING',5,message='Empty responses')
            health.save_dataset('injuries','RUNNING')
            health.save_dataset('injuries','HEALTHY',50)
            health.save_dataset('fixtures','FAIL',message='Failed league')
            health.save_dataset('fixtures','HEALTHY',100)
        with health.store() as db:
            self.assertEqual(dict(db.execute('SELECT dataset,status FROM dataset_status')),
                             {'injuries':'WARNING','fixtures':'FAIL'})
        with health.pipeline('nightly'):
            health.save_dataset('fixtures','HEALTHY',100)
        with health.store() as db:
            self.assertEqual(db.execute("SELECT status FROM dataset_status WHERE dataset='fixtures'").fetchone()[0],'HEALTHY')

    def test_zero_injuries_warns_without_failing(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (1000,None)
        @health.monitored('injuries')
        def stage(conn):
            usage.record('injuries',{},200,0,900,0.1,True,None)
        with health.pipeline('sync injuries'):
            stage(conn)
        with health.store() as db:
            self.assertEqual(db.execute('SELECT status,api_calls,quota_remaining FROM pipeline_runs').fetchone(), ('WARNING',1,900))

    def test_offseason_predictions_and_odds_are_not_fatal(self):
        for dataset, coverage in [('predictions',(0,)),('odds',(0,0))]:
            conn = Mock()
            conn.execute.side_effect = [Mock(fetchone=Mock(return_value=(0,None))), Mock(fetchone=Mock(return_value=coverage))]
            health.inspect_dataset(dataset, conn)
        with health.store() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM dataset_status')], ['HEALTHY','HEALTHY'])

    def test_missing_predictions_and_odds_warn(self):
        for dataset, coverage in [('predictions',(5,)),('odds',(10,0))]:
            conn = Mock()
            conn.execute.side_effect = [Mock(fetchone=Mock(return_value=(100,None))), Mock(fetchone=Mock(return_value=coverage))]
            health.inspect_dataset(dataset, conn)
        with health.store() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM dataset_status')], ['WARNING','WARNING'])

    def test_invalid_export_records_failure_and_preserves_live(self):
        live = self.root/'data'
        write_valid_export(live,'old')
        @health.monitored('exports')
        def stage(conn, out_dir):
            def build(staged):
                write_valid_export(staged,'new')
                (staged/'rankings.json').write_text('{')
            _publish_export(build,out_dir)
        with self.assertRaises(ExportValidationError):
            with health.pipeline('export'):
                stage(None, live)
        self.assertIn('old',(live/'rankings.json').read_text())
        with health.store() as db:
            self.assertEqual(db.execute('SELECT status FROM dataset_status').fetchone()[0], 'FAIL')

    def test_error_details_do_not_store_credentials(self):
        with self.assertRaises(RuntimeError):
            with health.pipeline('export'):
                raise RuntimeError('postgres://secret@example')
        with health.store() as db:
            self.assertEqual(db.execute('SELECT error FROM pipeline_runs').fetchone()[0],'RuntimeError')

    def test_first_run_and_small_population_are_not_collapse(self):
        self.assertEqual(health.population_status(0,None)[0],'HEALTHY')
        self.assertEqual(health.population_status(0,5)[0],'HEALTHY')

    def test_active_league_gaps_warn(self):
        for dataset in ('teams','fixtures','standings'):
            conn = Mock()
            conn.execute.side_effect = [Mock(fetchone=Mock(return_value=(1000,None))),
                                       Mock(fetchall=Mock(return_value=[(39,2025)]))]
            health.inspect_dataset(dataset,conn)
            sql = conn.execute.call_args[0][0]
            self.assertIn('ls.start_date + 7',sql)
            if dataset == 'standings':
                self.assertIn("coverage->>'standings'",sql)
        with health.store() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM dataset_status')], ['WARNING']*3)

    def test_stale_success_and_unknown_are_warnings(self):
        health.save_dataset('injuries','HEALTHY',0)
        with health.store() as db:
            db.execute("UPDATE dataset_status SET last_success='2000-01-01T00:00:00+00:00'")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(render_health(),0)
        self.assertIn('Overall: WARNING',output.getvalue())
        self.assertIn('injuries STALE',output.getvalue())
        self.assertIn('fixtures UNKNOWN',output.getvalue())

    def test_export_collapse_is_blocked_before_publication(self):
        live = self.root/'data'
        write_valid_export(live,'old')
        def build(staged):
            write_valid_export(staged,'new')
            import json
            path = staged/'rankings.json'
            payload = json.loads(path.read_text())
            payload['rankings'] = [[1]]
            path.write_text(json.dumps(payload))
        with self.assertRaises(ExportValidationError):
            _publish_export(build,live)
        self.assertIn('old',(live/'rankings.json').read_text())
