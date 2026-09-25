import importlib
import os
import unittest
from unittest.mock import patch


def load_config(env):
    with patch.dict(os.environ, env, clear=True):
        import thecornerfc.config as config
        return importlib.reload(config)


class SafetyGuardTests(unittest.TestCase):
    def test_local_defaults_block_db_writes_and_api_access(self):
        config = load_config({
            "THECORNERFC_MODE": "local",
            "THECORNERFC_READ_ONLY": "true",
            "THECORNERFC_NO_API": "true",
            "READ_ONLY_DATABASE_URL": "postgresql://readonly@example/db",
            "DATABASE_URL": "postgresql://writer@example/db",
        })

        self.assertTrue(config.READ_ONLY)
        self.assertTrue(config.NO_API)
        self.assertEqual(config.DATABASE_URL, "postgresql://readonly@example/db")
        with self.assertRaises(config.SafetyError):
            config.require_db_write("rank")
        with self.assertRaises(config.SafetyError):
            config.require_api_access("status")

    def test_local_override_allows_intentional_db_writes_and_api_access(self):
        config = load_config({
            "THECORNERFC_MODE": "local",
            "THECORNERFC_READ_ONLY": "false",
            "THECORNERFC_NO_API": "false",
            "THECORNERFC_LOCAL_OVERRIDE": "I_UNDERSTAND_THIS_CAN_WRITE_PRODUCTION_DATA_AND_USE_API_QUOTA",
            "DATABASE_URL": "postgresql://writer@example/db",
            "API_FOOTBALL_KEY": "example-key",
        })

        config.require_db_write("rank")
        config.require_api_access("status")

    def test_api_client_refuses_no_api_mode_before_network_setup(self):
        load_config({
            "THECORNERFC_MODE": "local",
            "THECORNERFC_NO_API": "true",
            "API_FOOTBALL_KEY": "example-key",
        })
        import thecornerfc.api as api
        api = importlib.reload(api)

        with self.assertRaises(api.config.SafetyError):
            api.ApiFootball()


if __name__ == "__main__":
    unittest.main()
