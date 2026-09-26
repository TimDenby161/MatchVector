import unittest
from unittest.mock import patch

from thecornerfc import db


class ConnectionTests(unittest.TestCase):
    def test_copied_url_whitespace_is_removed_and_read_only_preserved(self):
        url = "postgresql://reader:example%20password@example.test:5432/postgres"
        with patch.object(db.config, "DATABASE_URL", " \t" + url + "\r\n"), \
             patch.object(db.config, "READ_ONLY", True), \
             patch.object(db.psycopg, "connect") as connect:
            conn = db.connect()
            connect.assert_called_once_with(url, prepare_threshold=None)
            self.assertTrue(conn.read_only)

    def test_blank_url_does_not_attempt_connection(self):
        with patch.object(db.config, "DATABASE_URL", " \r\n"), \
             patch.object(db.psycopg, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "not set"):
                db.connect()
            connect.assert_not_called()
