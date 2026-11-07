"""Display caching must preserve counters and react to changed evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meter
from usage_log import read_usage_log
from test_headless_collection import THREAD, records


class ProjectionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        path = self.folder / "synthetic.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in records()) + "\n")
        self.result = read_usage_log(path, THREAD)
        self.subject = meter.Meter(self.folder)
        self.saved = {"result": self.result, "contentHash": "synthetic-initial"}

    def test_repeated_summary_reuses_projection_and_returns_independent_values(self):
        original = deepcopy(self.result)
        with patch.object(meter, "reproject_usage", wraps=meter.reproject_usage) as project:
            first = self.subject._project_saved(self.saved)
            first["turns"][0]["tokens"]["total"] = 999
            second = self.subject._project_saved(self.saved)
            self.assertEqual(project.call_count, 1)
        self.assertEqual(second["turns"][0]["tokens"]["total"], 100)
        self.assertNotIn("usageSegments", second["turns"][0])
        self.assertEqual(self.result, original)

    def test_changed_hash_or_timezone_cannot_reuse_projection(self):
        with patch.object(meter, "reproject_usage", wraps=meter.reproject_usage) as project:
            self.subject._project_saved(self.saved)
            self.subject._project_saved({**self.saved, "contentHash": "synthetic-next"})
            with patch.dict(meter.os.environ, {"TZ": "synthetic-zone"}):
                self.subject._project_saved(self.saved)
            self.assertEqual(project.call_count, 3)

    def test_changed_declaration_cannot_reuse_projection(self):
        with patch.object(meter, "reproject_usage", wraps=meter.reproject_usage) as project:
            self.subject._project_saved(self.saved)
            self.subject.reader_override_windows = {"2026-10-16": {"startAt": 1792108800,
                "endAt": 1792195200, "timezone": "UTC"}}
            self.subject._project_saved(self.saved)
            self.assertEqual(project.call_count, 2)


if __name__ == "__main__":
    unittest.main()
