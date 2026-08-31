import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main


class DeploymentTests(unittest.TestCase):
    def test_cleanup_removes_expired_tree_and_protects_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_root = root / "staging"
            expired = stage_root / "owner" / "project" / "expired"
            active = stage_root / "owner" / "project" / "active"
            expired.mkdir(parents=True)
            active.mkdir(parents=True)
            (expired / "old.fits").write_bytes(b"old")
            (active / "running.fits").write_bytes(b"active")
            old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
            for tree in (expired, active):
                for path in [*tree.rglob("*"), tree]:
                    os.utime(path, (old_timestamp, old_timestamp), follow_symlinks=False)

            database = root / "service.sqlite3"
            with (
                patch.object(main.SETTINGS, "local_stage_root", stage_root),
                patch.object(main.SETTINGS, "database_file", database),
                patch.object(main.SETTINGS, "stage_cleanup_enabled", True),
                patch.object(main.SETTINGS, "stage_retention_days", 1),
            ):
                main.init_database()
                request = {"local_platform": {"stage_galaxy_dir": str(active)}}
                with main._connect() as conn:
                    conn.execute(
                        "INSERT INTO galfit_tasks "
                        "(task_id, state, request_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("active-task", "running", json.dumps(request), main.now(), main.now()),
                    )
                result = main.cleanup_staging_once()

            self.assertFalse(expired.exists())
            self.assertTrue(active.exists())
            self.assertEqual(result["removed"], [str(expired)])
            self.assertEqual(result["protected"], [str(active)])

    def test_cleanup_can_be_disabled(self) -> None:
        with patch.object(main.SETTINGS, "stage_cleanup_enabled", False):
            self.assertEqual(
                main.cleanup_staging_once(),
                {"enabled": False, "removed": [], "protected": []},
            )

    def test_deployment_validation_reports_all_missing_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(main.SETTINGS, "data_root", root / "missing-data"),
                patch.object(main.SETTINGS, "mcp_root", root / "missing-mcp"),
                patch.object(main.SETTINGS, "workflow_file", root / "missing-galfit.md"),
                patch.object(main.SETTINGS, "workflow_galfits_file", root / "missing-galfits.md"),
                patch.object(main.SETTINGS, "local_stage_root", root / "outside-stage"),
                patch.object(main.SETTINGS, "database_file", root / "db" / "service.sqlite3"),
                patch.object(main.SETTINGS, "log_dir", root / "logs"),
                patch.object(main.SETTINGS, "codex_bin", "definitely-missing-codex"),
                patch.object(main.SETTINGS, "direct_fit_python", "definitely-missing-python-1"),
                patch.object(main.SETTINGS, "direct_galfits_python", "definitely-missing-python-2"),
            ):
                with self.assertRaisesRegex(RuntimeError, "deployment validation failed") as raised:
                    main.validate_deployment()
            message = str(raised.exception)
            self.assertIn("GALAXY_DATA_ROOT", message)
            self.assertIn("GALAXY_MCP_ROOT", message)
            self.assertIn("selected Agent executable", message)
            self.assertIn("DIRECT_FIT_PYTHON", message)
            self.assertIn("DIRECT_GALFITS_PYTHON", message)


if __name__ == "__main__":
    unittest.main()
