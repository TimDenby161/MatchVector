import json
import tempfile
import unittest
from pathlib import Path

from thecornerfc.export import ExportValidationError, _publish_export
from thecornerfc.workflow_inputs import parse_int_list


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_valid_export(root, marker):
    write_json(root / "matches.json", {
        "generated_at": "test",
        "fields": ["id"],
        "matches": [],
        "competitions": {},
        "teams": {},
    })
    write_json(root / "rankings.json", {
        "generated_at": "test",
        "fields": ["team"],
        "rankings": [[i] for i in range(100)],
        "marker": marker,
    })
    write_json(root / "stats.json", {"generated_at": "test", "ranges": {}})
    write_json(root / "bets.json", {"summary": {}, "bets": [], "rules": {}, "marker": marker})
    write_json(root / "injuries.json", {"generated_at": "test", "fields": [], "teams": {}})
    write_json(root / "players.json", {
        "generated_at": "test",
        "fields": ["id"],
        "players": [[i] for i in range(100)],
    })
    write_json(root / "player_seasons.json", {
        "generated_at": "test",
        "fields": ["id"],
        "players": [[i] for i in range(100)],
    })
    for dirname in ("players", "clubs", "leagues"):
        write_json(root / dirname / "1.json", {"marker": marker})


class ExportSafetyTests(unittest.TestCase):
    def test_export_failure_preserves_old_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "data"
            write_valid_export(live, "old")

            def build(staged):
                write_valid_export(staged, "new")
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                _publish_export(build, live)

            self.assertEqual(json.loads((live / "rankings.json").read_text())["marker"], "old")

    def test_invalid_generated_json_preserves_old_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "data"
            write_valid_export(live, "old")

            def build(staged):
                write_valid_export(staged, "new")
                (staged / "players" / "1.json").write_text("{", encoding="utf-8")

            with self.assertRaises(ExportValidationError):
                _publish_export(build, live)

            self.assertEqual(json.loads((live / "rankings.json").read_text())["marker"], "old")

    def test_successful_export_replaces_old_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "data"
            write_valid_export(live, "old")

            _publish_export(lambda staged: write_valid_export(staged, "new"), live)

            self.assertEqual(json.loads((live / "rankings.json").read_text())["marker"], "new")
            self.assertEqual(json.loads((live / "clubs" / "1.json").read_text())["marker"], "new")


class WorkflowInputTests(unittest.TestCase):
    def test_parse_int_list_accepts_space_separated_numbers(self):
        self.assertEqual(parse_int_list("233 188\t39", "leagues"), [233, 188, 39])

    def test_parse_int_list_rejects_shell_syntax(self):
        with self.assertRaises(ValueError):
            parse_int_list("233; echo hacked", "leagues")

    def test_parse_int_list_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            parse_int_list("   ", "seasons")


if __name__ == "__main__":
    unittest.main()
