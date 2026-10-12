import json
import stat
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from urllib import error, request
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))
import app  # noqa: E402
from installer import InstallError  # noqa: E402


def activation(profile: str = "wall-10") -> dict:
    return {
        "credential": "test-credential-not-for-evidence",
        "site_id": "a" * 32,
        "site_name": "Neutral Site",
        "channel": "staging",
        "profile": profile,
        "site_manifest": {
            "schema_version": 1,
            "site": {"id": "a" * 32, "title": "Neutral Site"},
            "panel": {
                "id": "neutral-panel",
                "type": "phone" if profile == "mobile/app" else "wall",
                "profile": profile,
                "display_mode": "companion-app" if profile == "mobile/app" else "ha-shell",
            },
        },
    }


class NeutralOnboardingTests(unittest.TestCase):
    def paths(self, root: Path):
        data = root / "data"
        config = root / "config"
        return mock.patch.multiple(
            app,
            DATA_ROOT=data,
            CONFIG_ROOT=config,
            SETTINGS=data / "settings.json",
            SITE_MANIFEST=config / "van_gogh2-site.json",
            OPERATION_RECEIPT=data / "operation.json",
            OPERATION_STATE={"id": 0, "state": "idle", "status": "idle"},
        )

    def test_clean_activation_creates_neutral_manifest_and_minimal_protected_settings(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            state = app._commit_activation(activation("wall-10"))
            self.assertEqual(state, "created_neutral")
            manifest = json.loads(app.SITE_MANIFEST.read_text(encoding="utf-8"))
            settings = json.loads(app.SETTINGS.read_text(encoding="utf-8"))
            self.assertEqual(manifest["panel"]["profile"], "wall-10")
            self.assertEqual(set(settings), {"credential", "site_id", "site_name"})
            self.assertNotIn("site_manifest", settings)
            self.assertNotIn("channel", settings)
            self.assertNotIn("profile", settings)
            self.assertNotIn("dashboard_targets", settings)
            self.assertEqual(stat.S_IMODE(app.SITE_MANIFEST.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(app.SETTINGS.stat().st_mode), 0o600)

    def test_installer_ui_has_no_profile_channel_or_dashboard_target_controls(self):
        html = (APP / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("dashboard-targets", html)
        self.assertNotIn("addMobileButton", html)
        self.assertNotIn("releaseChannel", html)
        self.assertNotIn("starterProfile", html)
        self.assertIn("Dashboard creation and editing happen only in Builder", html)

    def test_existing_manifest_is_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app.CONFIG_ROOT.mkdir(parents=True)
            before = b'{"panel":{"profile":"wall-10"},"site":{"id":"owner-existing"}}\n'
            app.SITE_MANIFEST.write_bytes(before)
            state = app._commit_activation(activation("mobile/app"))
            self.assertEqual(state, "preserved_existing")
            self.assertEqual(app.SITE_MANIFEST.read_bytes(), before)
            self.assertEqual(app._site_manifest_profile(), "wall-10")

    def test_invalid_existing_manifest_fails_before_code_exchange(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app.CONFIG_ROOT.mkdir(parents=True)
            app.SITE_MANIFEST.write_text("not-json", encoding="utf-8")
            with self.assertRaises(InstallError):
                app._site_manifest_profile()

    def test_status_does_not_return_credential_or_removed_installer_selections(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation())
            status = app._status()
            self.assertNotIn("credential", status)
            self.assertNotIn("test-credential", json.dumps(status, sort_keys=True))
            self.assertNotIn("channel", status)
            self.assertNotIn("profile", status)
            self.assertNotIn("dashboard_targets", status)

    def test_activation_ignores_removed_client_selectors_and_uses_server_bootstrap_contract(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                payload = json.dumps({
                    "install_code": "MTAE-VG-TEST",
                    "channel": "staging",
                    "profile": "mobile/app",
                    "dashboard_targets": ["wall-10", "mobile/app"],
                }).encode()
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/activate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with mock.patch.object(
                    app.ReleaseClient,
                    "activate",
                    return_value=activation("wall-10"),
                ) as activate:
                    response = json.loads(request.urlopen(post, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            activate.assert_called_once_with(
                "https://installer.mtae.com.au",
                "MTAE-VG-TEST",
                "production",
                "wall-10",
            )
            self.assertNotIn("dashboard_targets", response)
            self.assertNotIn("channel", app._status())

    def test_removed_dashboard_target_endpoint_is_404_and_state_safe(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            manifest_before = app.SITE_MANIFEST.read_bytes()
            settings_before = app.SETTINGS.read_bytes()
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/dashboard-targets",
                    data=b'{"dashboard_targets":["mobile/app"]}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(error.HTTPError) as response:
                    request.urlopen(post, timeout=5)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertEqual(response.exception.code, 404)
            self.assertEqual(app.SITE_MANIFEST.read_bytes(), manifest_before)
            self.assertEqual(app.SETTINGS.read_bytes(), settings_before)

    def test_install_uses_builder_owned_orchestration_without_dashboard_targets(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            captured: dict[str, object] = {}

            class FakeReleaseClient:
                def latest(self):
                    return {}

                def download(self, manifest, destination):
                    destination.write_bytes(b"package")

            class FakeUpdater:
                def __init__(self, manager, ha, installer_version):
                    captured["installer_version"] = installer_version

                def apply_builder_owned(self, *args, **kwargs):
                    captured["args"] = args
                    captured["kwargs"] = kwargs
                    return {"operation": "noop", "file_writes": 0, "readiness": {"builder_owned": True}}

            manager = mock.Mock()
            manager.current_receipt = Path(temporary) / "missing-receipt.json"
            ha = mock.Mock()
            ha.wait_ready.return_value = {"version": "2026.9.1"}
            ha.snapshot.return_value = {"resources": []}
            ha.finish_setup.return_value = {"builder_dashboard": {"path": "van-gogh-builder"}}
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/install",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with (
                    mock.patch.object(app, "_release_client", return_value=FakeReleaseClient()),
                    mock.patch.object(
                        app,
                        "_validate_product_release_manifest",
                        return_value={
                            "version": "2.0.0-builder.3",
                            "module_url": "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js",
                        },
                    ),
                    mock.patch.object(app, "module_url_for_release", return_value="unused"),
                    mock.patch.object(app, "InstallManager", return_value=manager),
                    mock.patch.object(app.HAClient, "from_environment", return_value=ha),
                    mock.patch.object(app, "ProductUpdateManager", FakeUpdater),
                ):
                    response = json.loads(request.urlopen(post, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertTrue(response["installed"])
            self.assertEqual(captured["installer_version"], "1.2.4")
            self.assertEqual(captured["kwargs"], {"defer_commit": True})
            self.assertEqual(captured["args"][3], "2026.9.1")
            self.assertFalse(any("target" in str(value).lower() for value in captured["kwargs"].values()))
            ha.finish_setup.assert_called_once()

    def test_noop_builder_entry_migration_records_exact_rollback_prestate(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))

            class FakeReleaseClient:
                def latest(self):
                    return {}

                def download(self, manifest, destination):
                    destination.write_bytes(b"package")

            class FakeUpdater:
                def __init__(self, manager, ha, installer_version):
                    pass

                def apply_builder_owned(self, *args, **kwargs):
                    return {"operation": "noop", "file_writes": 0}

            prestate = {
                "entries": [],
                "resources": [],
                "builder_dashboard": {
                    "url_path": "van-gogh-builder",
                    "exists": True,
                    "registry": {"id": "van_gogh_builder"},
                    "config": {"views": [{"path": "builder", "cards": []}]},
                },
            }
            manager = mock.Mock()
            manager.current_receipt = Path(temporary) / "current.json"
            manager.current_receipt.write_text("{}", encoding="utf-8")
            manager.annotate_current.return_value = {"recorded": True}
            ha = mock.Mock()
            ha.wait_ready.return_value = {"version": "2026.9.1"}
            ha.snapshot.return_value = prestate
            ha.finish_setup.return_value = {
                "builder_dashboard": {
                    "url_path": "van-gogh-builder",
                    "migrated": True,
                    "dashboard_writes": 1,
                }
            }
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/install",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with (
                    mock.patch.object(app, "_release_client", return_value=FakeReleaseClient()),
                    mock.patch.object(
                        app,
                        "_validate_product_release_manifest",
                        return_value={
                            "version": "2.0.0-builder.3",
                            "module_url": "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js",
                        },
                    ),
                    mock.patch.object(app, "module_url_for_release", return_value="unused"),
                    mock.patch.object(app, "InstallManager", return_value=manager),
                    mock.patch.object(app.HAClient, "from_environment", return_value=ha),
                    mock.patch.object(app, "ProductUpdateManager", FakeUpdater),
                ):
                    response = json.loads(request.urlopen(post, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertTrue(response["installed"])
            readiness = ha.finish_setup.return_value
            self.assertIn("restart_cycle", readiness)
            manager.annotate_current.assert_called_once_with(
                {
                    "ha_prestate": prestate,
                    "readiness": readiness,
                    "restart_required": False,
                }
            )


if __name__ == "__main__":
    unittest.main()