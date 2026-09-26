from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from thecornerfc import config, player_history


class PlayerHistoryTests(unittest.TestCase):
    def test_rank_ties_and_observed_evidence(self):
        current=[(1,80.,'CM',1000),(2,80.,'CB',500),(3,70.,'ST',100)]
        seasons=[(1,2026,80.,1200,10),(2,2026,80.,0,20)]
        rows=player_history.build_rows(current,seasons,2026,{1:30})
        self.assertEqual([r[2] for r in rows],[1,1,3])
        self.assertEqual(rows[0][5:9],(30,'latest_squad',1000,1200))
        self.assertEqual(rows[1][5:9],(20,'season_model',500,0))
        self.assertEqual(rows[2][-1],'window_fallback')
        self.assertEqual(rows[2][6],'unknown')

    def test_later_values_do_not_modify_previous_observation(self):
        old=player_history.build_rows([(1,60.,'CM',100)],[(1,2026,60.,100,10)],2026,{})
        new=player_history.build_rows([(1,70.,'CM',200)],[(1,2026,70.,200,20)],2026,{})
        self.assertEqual(old[0][1],60.)
        self.assertEqual(old[0][5],10)
        self.assertEqual(new[0][1],70.)

    def test_existing_day_is_not_written_again(self):
        conn=Mock()
        conn.execute.return_value.fetchone.return_value=(99,)
        with patch.object(config,'require_db_write'),patch('thecornerfc.player_history.register_version') as register:
            player_history.capture(conn,[(1,60.,'CM',100)],[(1,2026,60.,100,10)])
        register.assert_not_called()
        conn.cursor.assert_not_called()
        conn.commit.assert_not_called()

    def test_schema_contains_migration(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn((root/'db/migrations/20260926_player_rating_history.sql').read_text(),(root/'db/schema.sql').read_text())


@unittest.skipUnless(os.getenv('MODEL_VERSION_TEST_DSN'),'Requires disposable PostgreSQL test database')
class PlayerHistoryPostgresTests(unittest.TestCase):
    def test_stored_movement_and_immutability(self):
        import psycopg
        import uuid
        from thecornerfc.model_versions import register_model_version
        root=Path(__file__).resolve().parents[1]/'db/migrations'
        with psycopg.connect(os.environ['MODEL_VERSION_TEST_DSN']) as conn:
            try:
                schema='player_history_test_'+uuid.uuid4().hex
                conn.execute(f'CREATE SCHEMA {schema}')
                conn.execute(f'SET LOCAL search_path TO {schema}')
                conn.execute((root/'20260926_model_versions.sql').read_text())
                sql=(root/'20260926_player_rating_history.sql').read_text()
                conn.execute(sql);conn.execute(sql)
                with patch.object(config,'READ_ONLY',False),patch.object(config,'GITHUB_ACTIONS',True):
                    version=register_model_version(conn,'player','test')
                now=datetime.now(timezone.utc)
                for days,rating,rank in [(100,40,50),(90,50,40),(30,60,30),(7,70,20),(0,80,10)]:
                    captured=now-timedelta(days=days)
                    cid=conn.execute('''INSERT INTO player_rating_captures
                        (capture_date,captured_at,model_version_id,season,population)
                        VALUES (%s,%s,%s,2026,100) RETURNING capture_id''',[captured.date(),captured,version]).fetchone()[0]
                    conn.execute('''INSERT INTO player_rating_history
                        (capture_id,player_id,rating,world_rank,team_source,rating_source)
                        VALUES (%s,1,%s,%s,'unknown','season_model')''',[cid,rating,rank])
                actual={r[0]:(float(r[1]),r[2]) for r in conn.execute('SELECT horizon,rating_change,rank_movement FROM player_rating_movement')}
                self.assertEqual(actual,{'7d':(10.,10),'30d':(20.,20),'90d':(30.,30),'season':(40.,40)})
                for table in ('player_rating_history','player_rating_captures'):
                    for mutation in (f'DELETE FROM {table}',f'TRUNCATE {table} CASCADE'):
                        with self.assertRaises(psycopg.errors.RaiseException):
                            with conn.transaction():
                                conn.execute(mutation)
                with self.assertRaises(psycopg.errors.RaiseException):
                    with conn.transaction():
                        conn.execute('UPDATE player_rating_history SET rating=0')
                self.assertEqual(conn.execute('SELECT min(rating) FROM player_rating_history').fetchone()[0],40)
            finally:
                conn.rollback()
