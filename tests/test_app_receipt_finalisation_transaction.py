from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

import app  # noqa: E402
from installer import InstallManager  # noqa: E402


def tree_snapshot(root: Path):
    if not root.exists():
        return None
    rows = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows[relative] = {
                "type": "symlink",
                "target": str(path.readlink()),
            }
        elif path.is_file():
            rows[relative] = {
                "type": "file",
                "mode": path.stat().st_mode & 0o777,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        elif path.is_dir():
            rows[relative] = {
                "type": "directory",
                "mode": path.stat().st_mode & 0o777,
            }
    return rows


class FakeReleaseClient:
    release = None
    archive = None

    def latest(self):
        return copy.deepcopy(self.release)

    def download(self, manifest, destination):
        shutil.copyfile(self.archive, destination)


class FakeHA:
    def __init__(self) -> None:
        self.rows = []
        self.restart_count = 0
        self.restore_prestate_count = 0
        self.service_calls = 0
        self.equipment_actions = 0

    def wait_ready(self):
        return {"version": "2026.9.4"}

    def snapshot(self):
        return {"entries": [], "resources": []}

    def storage_dashboard_inventory(self):
        return copy.deepcopy(self.rows)

    def dashboard_snapshot(self, path):
        row = next(
            (
                item
                for item in self.rows
                if item["registry"]["url_path"] == path
            ),
            None,
        )
        return {
            "url_path": path,
            "present": row is not None,
            "registry": copy.deepcopy(row["registry"]) if row else None,
            "config": copy.deepcopy(row["config"]) if row else None,
        }

    def save_dashboard(self, path, config, title):
        self.rows.append(
            {
                "registry": {
                    "id": path.replace("-", "_"),
                    "url_path": path,
                    "title": title,
                    "mode": "storage",
                },
                "config": copy.deepcopy(config),
            }
        )
        return {"readback_verified": True}

    def save_existing_dashboard(self, path, before, after):
        row = next(
            item
            for item in self.rows
            if item["registry"]["url_path"] == path
        )
        row["config"] = copy.deepcopy(after)
        return {"readback_verified": True}

    def restore_dashboard(self, snapshot):
        self.rows = [
            item
            for item in self.rows
            if item["registry"]["url_path"] != snapshot["url_path"]
        ]
        if snapshot["present"]:
            self.rows.append(
                {
                    "registry": copy.deepcopy(snapshot["registry"]),
                    "config": copy.deepcopy(snapshot["config"]),
                }
            )
        return {"readback_verified": True}

    def restart_and_wait_ready(self, progress=None):
        self.restart_count += 1
        if progress:
            progress("synthetic_restart_complete")
        return {"synthetic": True}

    def finish_setup(self, version):
        return {
            "integration": {"state": "loaded"},
            "resource": {"count": 1},
            "module": {"status": 200},
        }

    def restore_prestate(self, prestate):
        self.restore_prestate_count += 1
        return {"readback_verified": True}


class AppReceiptFinalisationTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = json.loads(
            (ROOT / "dist" / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )
        cls.archive = ROOT / "dist" / cls.release["archive"]
        cls.site_source = ROOT / "tests" / "fixtures" / "site-manifest.example.json"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.original_paths = {
            "DATA_ROOT": app.DATA_ROOT,
            "CONFIG_ROOT": app.CONFIG_ROOT,
            "SETTINGS": app.SETTINGS,
            "OPERATION_RECEIPT": app.OPERATION_RECEIPT,
            "SITE_MANIFEST": app.SITE_MANIFEST,
        }
        app.DATA_ROOT = self.base / "data"
        app.CONFIG_ROOT = self.base / "config"
        app.SETTINGS = app.DATA_ROOT / "settings.json"
        app.OPERATION_RECEIPT = app.DATA_ROOT / "operation.json"
        app.SITE_MANIFEST = app.CONFIG_ROOT / "van_gogh2-site.json"
        app.SITE_MANIFEST.parent.mkdir(parents=True)
        app.SITE_MANIFEST.write_bytes(self.site_source.read_bytes())
        app.OPERATION_STATE.clear()
        app.OPERATION_STATE.update(
            {
                "id": 0,
                "operation": None,
                "stage": "idle",
                "state": "idle",
                "message": None,
                "error": None,
            }
        )
        FakeReleaseClient.release = self.release
        FakeReleaseClient.archive = self.archive
        self.before = tree_snapshot(app.CONFIG_ROOT)

    def tearDown(self) -> None:
        for name, value in self.original_paths.items():
            setattr(app, name, value)
        self.temporary.cleanup()

    def run_install(self, failing_field=None):
        fake_ha = FakeHA()
        real_annotate = app.InstallManager.annotate_current

        def injected(manager, fields):
            if failing_field is not None and failing_field in fields:
                raise OSError(
                    f"synthetic {failing_field} receipt finalisation failure"
                )
            return real_annotate(manager, fields)

        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/install",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        try:
            with patch(
                "app._release_client",
                return_value=FakeReleaseClient(),
            ), patch(
                "app.HAClient.from_environment",
                return_value=fake_ha,
            ), patch(
                "app.InstallManager.annotate_current",
                new=injected,
            ):
                try:
                    with urllib.request.urlopen(request, timeout=120) as response:
                        status = response.status
                        payload = json.loads(response.read())
                except urllib.error.HTTPError as error:
                    status = error.code
                    payload = json.loads(error.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        manager = InstallManager(app.CONFIG_ROOT)
        operation = json.loads(app.OPERATION_RECEIPT.read_text(encoding="utf-8"))
        return {
            "status": status,
            "payload": payload,
            "ha": fake_ha,
            "manager": manager,
            "operation": operation,
            "after": tree_snapshot(app.CONFIG_ROOT),
        }

    def assert_failed_install_restored(
        self,
        result,
        *,
        expected_restart_count: int,
    ) -> None:
        self.assertGreaterEqual(result["status"], 400)
        self.assertEqual(result["after"], self.before)
        self.assertIsNone(result["manager"].installed_version())
        self.assertFalse(result["manager"].current_receipt.exists())
        self.assertEqual(result["ha"].rows, [])
        self.assertEqual(result["ha"].restart_count, expected_restart_count)
        self.assertEqual(result["ha"].restore_prestate_count, 1)
        self.assertEqual(result["ha"].service_calls, 0)
        self.assertEqual(result["ha"].equipment_actions, 0)
        self.assertEqual(result["operation"]["state"], "error")
        self.assertTrue(result["operation"]["error"])

    def test_ha_prestate_receipt_failure_restores_exact_pre_call_state(self) -> None:
        result = self.run_install("ha_prestate")

        self.assert_failed_install_restored(result, expected_restart_count=0)

    def test_readiness_receipt_failure_restores_exact_pre_call_state(self) -> None:
        result = self.run_install("readiness")

        self.assert_failed_install_restored(result, expected_restart_count=2)

    def test_successful_receipt_finalisation_commits_install(self) -> None:
        result = self.run_install()

        self.assertEqual(result["status"], 200)
        self.assertNotEqual(result["after"], self.before)
        self.assertEqual(
            result["manager"].installed_version(),
            self.release["version"],
        )
        self.assertTrue(result["manager"].current_receipt.is_file())
        self.assertEqual(len(result["ha"].rows), 1)
        self.assertEqual(result["ha"].restart_count, 1)
        self.assertEqual(result["ha"].restore_prestate_count, 0)
        self.assertEqual(result["ha"].service_calls, 0)
        self.assertEqual(result["ha"].equipment_actions, 0)
        self.assertEqual(result["operation"]["state"], "complete")
        receipt = json.loads(
            result["manager"].current_receipt.read_text(encoding="utf-8")
        )
        self.assertIn("ha_prestate", receipt)
        self.assertIn("readiness", receipt)


if __name__ == "__main__":
    unittest.main()
