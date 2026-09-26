from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import uuid

from thecornerfc import config
from thecornerfc.model_versions import (ModelType, current_code_sha, register_model_version,
                                       snapshot_times, version_metadata)

MIGRATION = Path(__file__).resolve().parents[1]/'db/migrations/20260926_model_versions.sql'


class ModelVersionTests(unittest.TestCase):
    def test_all_domains_and_stable_configuration_order(self):
        for domain in ModelType:
            a = version_metadata(domain,'baseline',configuration={'a':1,'b':{'c':2}})
            b = version_metadata(domain,'baseline',configuration={'b':{'c':2},'a':1})
            self.assertEqual(a,b)
        self.assertEqual(len(ModelType),6)

    def test_identity_changes_with_provenance(self):
        baseline = version_metadata('match','baseline')['model_version_id']
        for kwargs in ({'configuration':{'weight':0.2}}, {'code_sha':'a'*40}, {'notes':'reconstruction'},
                       {'training_window':(datetime(2020,1,1,tzinfo=timezone.utc),datetime(2021,1,1,tzinfo=timezone.utc))}):
            self.assertNotEqual(baseline,version_metadata('match','baseline',**kwargs)['model_version_id'])
        self.assertNotEqual(baseline,version_metadata('club','baseline')['model_version_id'])

    def test_rejects_invalid_metadata(self):
        for kwargs in ({'code_sha':'short'}, {'configuration':{'API_KEY':'secret'}},
                       {'configuration':{'nested':{'password':'secret'}}},
                       {'configuration':{'weight':float('nan')}},
                       {'configuration':[]}, {'training_window':(None,None)},
                       {'training_window':(datetime(2020,1,1),datetime(2021,1,1))}):
            with self.assertRaises(ValueError):
                version_metadata('match','baseline',**kwargs)
        with self.assertRaises(ValueError):
            version_metadata('unsupported','baseline')

    def test_timestamp_semantics_allow_labelled_backfills_and_normalize_utc(self):
        effective = datetime(2025,1,1,tzinfo=timezone.utc)
        captured = effective + timedelta(days=2)
        times = snapshot_times(captured_at=captured,effective_at=effective)
        self.assertNotIn('created_at',times)
        self.assertGreater(times['captured_at'],times['effective_at'])
        offset = timezone(timedelta(hours=2))
        self.assertEqual(times,snapshot_times(captured_at=captured.astimezone(offset),effective_at=effective))
        with self.assertRaises(ValueError):
            snapshot_times(captured_at=datetime(2025,1,1),effective_at=effective)

    def test_register_is_guarded_and_does_not_commit(self):
        conn = Mock()
        with patch.object(config,'READ_ONLY',True):
            with self.assertRaises(config.SafetyError):
                register_model_version(conn,'club','baseline')
        conn.execute.assert_not_called()
        with patch.object(config,'READ_ONLY',False), patch.object(config,'GITHUB_ACTIONS',True):
            identity = register_model_version(conn,'club','baseline',configuration={'weight':1})
        self.assertEqual(identity,version_metadata('club','baseline',configuration={'weight':1})['model_version_id'])
        sql, values = conn.execute.call_args.args
        self.assertIn('ON CONFLICT (model_version_id) DO NOTHING',sql)
        self.assertNotIn('created_at',values)
        conn.commit.assert_not_called()

    @patch('thecornerfc.model_versions.subprocess.check_output',return_value=' M changed.py')
    def test_dirty_tree_has_no_claimed_sha(self, command):
        self.assertIsNone(current_code_sha())

    def test_fresh_schema_contains_exact_migration(self):
        schema = MIGRATION.parent.parent/'schema.sql'
        self.assertIn(MIGRATION.read_text(), schema.read_text())


@unittest.skipUnless(os.getenv('MODEL_VERSION_TEST_DSN'), 'Requires an explicitly supplied disposable PostgreSQL test database')
class RegistryPostgresTests(unittest.TestCase):
    def test_migration_repeatability_immutability_and_history_preservation(self):
        import psycopg
        with psycopg.connect(os.environ['MODEL_VERSION_TEST_DSN']) as conn:
            try:
                schema = 'registry_test_' + uuid.uuid4().hex
                conn.execute(f'CREATE SCHEMA {schema}')
                conn.execute(f'SET LOCAL search_path TO {schema}')
                conn.execute('CREATE TABLE historical_output (value text)')
                conn.execute("INSERT INTO historical_output VALUES ('unchanged')")
                conn.execute(MIGRATION.read_text())
                conn.execute(MIGRATION.read_text())
                with patch.object(config,'READ_ONLY',False), patch.object(config,'GITHUB_ACTIONS',True):
                    first = register_model_version(conn,'club','baseline')
                    second = register_model_version(conn,'club','baseline')
                self.assertEqual(first,second)
                self.assertEqual(conn.execute('SELECT count(*) FROM model_versions').fetchone()[0],1)
                self.assertIsNotNone(conn.execute('SELECT created_at FROM model_versions').fetchone()[0])
                self.assertEqual(conn.execute('SELECT value FROM historical_output').fetchone()[0],'unchanged')
                from thecornerfc.match_snapshots import append_snapshots, make_snapshot
                conn.execute((MIGRATION.parent/'20260926_match_prediction_snapshots.sql').read_text())
                conn.execute((MIGRATION.parent/'20260926_match_prediction_snapshots.sql').read_text())
                with patch.object(config,'READ_ONLY',False), patch.object(config,'GITHUB_ACTIONS',True):
                    match_version=register_model_version(conn,'match','baseline')
                    captured=datetime.now(timezone.utc)
                    kickoff=captured+timedelta(days=1)
                    row=(1,kickoff,39,10,20,1000.,900.,0.5,1.5,1.,0.5,0.3,0.2,'1-0',0.5,0.5)
                    old=make_snapshot(row,{},version_id=match_version,captured_at=captured,reference_at=captured,source='prospective')
                    append_snapshots(conn,[old,old])
                    later=make_snapshot(row,{'home_current_rank':1100},version_id=match_version,
                                        captured_at=captured+timedelta(minutes=1),reference_at=captured,source='prospective')
                    append_snapshots(conn,[later])
                self.assertEqual(conn.execute('SELECT count(*) FROM match_prediction_snapshots').fetchone()[0],2)
                self.assertEqual(conn.execute('SELECT inputs FROM match_prediction_snapshots ORDER BY snapshot_id LIMIT 1').fetchone()[0]['home_match_rank'],1000.)
                for mutation in ("UPDATE match_prediction_snapshots SET p_home=0.6",'DELETE FROM match_prediction_snapshots','TRUNCATE match_prediction_snapshots'):
                    with self.assertRaises(psycopg.errors.RaiseException):
                        with conn.transaction():
                            conn.execute(mutation)
                for sql in ("UPDATE model_versions SET notes='changed'",'DELETE FROM model_versions'):
                    with self.assertRaises(psycopg.errors.RaiseException):
                        with conn.transaction():
                            conn.execute(sql)
            finally:
                conn.rollback()  # Includes the test schema; no persistent test data.
