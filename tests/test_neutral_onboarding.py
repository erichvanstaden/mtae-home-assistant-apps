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

    def test_clean_activation_creates_neutral_manifest_and_protected_settings(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            state = app._commit_activation(activation("mobile/app"))
            self.assertEqual(state, "created_neutral")
            manifest = json.loads(app.SITE_MANIFEST.read_text(encoding="utf-8"))
            settings = json.loads(app.SETTINGS.read_text(encoding="utf-8"))
            self.assertEqual(manifest["panel"]["profile"], "mobile/app")
            self.assertNotIn("site_manifest", settings)
            self.assertEqual(settings["channel"], "staging")
            self.assertEqual(stat.S_IMODE(app.SITE_MANIFEST.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(app.SETTINGS.stat().st_mode), 0o600)

    def test_installer_ui_offers_both_on_activation_and_add_mobile_after_activation(self):
        html = (APP / "index.html").read_text(encoding="utf-8")

        self.assertIn('value="both">Both — Wall-10 + Mobile</option>', html)
        self.assertIn('id="addMobileButton"', html)
        self.assertIn('>Add Mobile</button>', html)
        self.assertIn("api/dashboard-targets", html)

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

    def test_status_does_not_return_credential(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation())
            status = app._status()
            self.assertNotIn("credential", status)
            self.assertNotIn("test-credential", json.dumps(status, sort_keys=True))

    def test_activated_wall_can_add_mobile_without_reactivation_or_manifest_change(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            manifest_before = app.SITE_MANIFEST.read_bytes()
            settings_before = json.loads(app.SETTINGS.read_text(encoding="utf-8"))

            with mock.patch.object(
                app.ReleaseClient,
                "activate",
                side_effect=AssertionError("target selection must not repeat activation"),
            ):
                selected = app._set_dashboard_targets(["wall-10", "mobile/app"])

            settings_after = json.loads(app.SETTINGS.read_text(encoding="utf-8"))
            self.assertEqual(selected, ["wall-10", "mobile/app"])
            self.assertEqual(settings_after["dashboard_targets"], selected)
            self.assertEqual(settings_after["credential"], settings_before["credential"])
            self.assertEqual(app.SITE_MANIFEST.read_bytes(), manifest_before)
            self.assertEqual(app._status()["dashboard_targets"], selected)
            self.assertNotIn("credential", app._status())

    def test_dashboard_target_selection_is_additive(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            app._set_dashboard_targets(["wall-10", "mobile/app"])

            selected = app._set_dashboard_targets(["wall-10"])

            self.assertEqual(selected, ["wall-10", "mobile/app"])

    def test_dashboard_target_endpoint_updates_an_activated_site_without_code_exchange(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            manifest_before = app.SITE_MANIFEST.read_bytes()
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                payload = json.dumps({"dashboard_targets": ["wall-10", "mobile/app"]}).encode()
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/dashboard-targets",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with mock.patch.object(
                    app.ReleaseClient,
                    "activate",
                    side_effect=AssertionError("target selection must not repeat activation"),
                ):
                    response = json.loads(request.urlopen(post, timeout=5).read())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertEqual(response["dashboard_targets"], ["wall-10", "mobile/app"])
            self.assertEqual(app.SITE_MANIFEST.read_bytes(), manifest_before)

    def test_one_activation_can_select_both_dashboard_targets(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                payload = json.dumps({
                    "install_code": "MTAE-VG-TEST",
                    "channel": "staging",
                    "profile": "wall-10",
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
                "staging",
                "wall-10",
            )
            self.assertEqual(response["dashboard_targets"], ["wall-10", "mobile/app"])
            self.assertEqual(app._status()["dashboard_targets"], ["wall-10", "mobile/app"])

    def test_invalid_dashboard_target_fails_before_activation_code_exchange(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                payload = json.dumps({
                    "install_code": "MTAE-VG-TEST",
                    "channel": "staging",
                    "profile": "wall-10",
                    "dashboard_targets": ["unsupported"],
                }).encode()
                post = request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/activate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with mock.patch.object(app.ReleaseClient, "activate") as activate:
                    with self.assertRaises(error.HTTPError) as response:
                        request.urlopen(post, timeout=5)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertEqual(response.exception.code, 400)
            activate.assert_not_called()

    def test_install_forwards_saved_dashboard_targets_to_product_update(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation("wall-10"))
            app._set_dashboard_targets(["wall-10", "mobile/app"])
            captured: dict[str, object] = {}

            class FakeReleaseClient:
                def latest(self):
                    return {}

                def download(self, manifest, destination):
                    destination.write_bytes(b"package")

            class FakeUpdater:
                def __init__(self, manager, ha, installer_version):
                    pass

                def apply(self, *args, **kwargs):
                    captured.update(kwargs)
                    return {"operation": "noop", "file_writes": 0}

            manager = mock.Mock()
            manager.current_receipt = Path(temporary) / "missing-receipt.json"
            ha = mock.Mock()
            ha.wait_ready.return_value = {"version": "2026.8.0"}
            ha.snapshot.return_value = {"resources": []}
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
                        return_value={"version": "1.2.3", "module_url": "unused"},
                    ),
                    mock.patch.object(app, "module_url_for_release", return_value="unused"),
                    mock.patch.object(app, "InstallManager", return_value=manager),
                    mock.patch.object(app.HAClient, "from_environment", return_value=ha),
                    mock.patch.object(app, "ProductUpdateManager", FakeUpdater),
                ):
                    request.urlopen(post, timeout=5).read()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

            self.assertEqual(captured["dashboard_targets"], ["wall-10", "mobile/app"])


if __name__ == "__main__":
    unittest.main()
