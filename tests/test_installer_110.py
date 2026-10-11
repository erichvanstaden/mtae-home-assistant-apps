from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))
os.environ.setdefault("VAN_GOGH_DATA_ROOT", tempfile.mkdtemp(prefix="vg-test-data-"))
os.environ.setdefault("VAN_GOGH_CONFIG_ROOT", tempfile.mkdtemp(prefix="vg-test-config-"))

import app  # noqa: E402
import installer  # noqa: E402  # pyright: ignore[reportMissingImports]
from ha_client import HAClient, HAError  # noqa: E402
from installer import InstallError  # noqa: E402


class FakeConnection:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def call(self, message: dict):
        self.state.setdefault("commands", []).append(dict(message))
        kind = message["type"]
        if kind == "lovelace/dashboards/list":
            return [dict(row) for row in self.state["rows"]]
        if kind == "lovelace/config":
            return self.state["configs"][message["url_path"]]
        if kind == "lovelace/config/save":
            self.state["configs"][message["url_path"]] = message["config"]
            return None
        if kind == "lovelace/dashboards/create":
            self.state["rows"].append({
                "id": "new-review-id",
                "url_path": message["url_path"],
                "title": message["title"],
                "icon": message["icon"],
                "show_in_sidebar": message["show_in_sidebar"],
                "require_admin": message["require_admin"],
                "mode": message["mode"],
            })
            return None
        if kind == "lovelace/dashboards/update":
            row = next(row for row in self.state["rows"] if row["id"] == message["dashboard_id"])
            for key in ("url_path", "title", "icon", "show_in_sidebar", "require_admin", "mode"):
                if key in message:
                    row[key] = message[key]
            return None
        if kind == "lovelace/dashboards/delete":
            self.state["rows"] = [row for row in self.state["rows"] if row["id"] != message["dashboard_id"]]
            return None
        raise AssertionError(f"unexpected command {message}")


class FakeHTTPResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, _limit: int):
        return self.payload


class Installer110ContractTests(unittest.TestCase):
    def manifest(self):
        return {
            "product": "van-gogh2",
            "version": "2.0.0-staging.8",
            "component_version": "2.0.0-staging.8",
            "foundation_version": "1.5.0",
            "package_version": "1.0.0-rc.3",
            "package_schema": app.PRODUCT_PACKAGE_SCHEMA,
            "sha256": "a" * 64,
        }

    def test_product_release_manifest_is_accepted(self):
        manifest = self.manifest()
        self.assertIs(app._validate_product_release_manifest(manifest), manifest)

    def test_release_lookup_preserves_product_package_identity(self):
        manifest = {
            **self.manifest(),
            "archive": "/private/product-package.tar.gz",
            "module_url": "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js",
            "supported_home_assistant": {"minimum": "2026.8.3", "maximum_exclusive": "2027.0.0"},
        }
        with mock.patch.object(installer, "_urlopen", return_value=FakeHTTPResponse(manifest)):
            result = installer.ReleaseClient("https://example.invalid", "site-credential").latest()
        for key in (
            "package_version",
            "component_version",
            "foundation_version",
            "package_schema",
            "module_url",
        ):
            self.assertEqual(result[key], manifest[key])

    def test_ha_client_exposes_product_dashboard_writer_not_legacy_builder_bootstrap(self):
        self.assertTrue(callable(getattr(HAClient, "save_dashboard", None)))
        self.assertFalse(hasattr(HAClient, "ensure_builder_dashboard"))

    def test_finish_setup_uses_builtin_local_assets_without_config_entry(self):
        client = HAClient(token="test")
        client.wait_ready = lambda: {"version": "2026.8.3"}
        client.ensure_config_entry = lambda: self.fail("config entry must not be created")
        client.ensure_resource = lambda module_url: {"url": module_url, "count": 1}
        client.probe_module = lambda module_url: {"url": module_url, "status": 200}

        module_url = "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js"
        result = client.finish_setup(module_url)

        self.assertEqual(result["integration"], {"required": False, "state": "not_required"})
        self.assertEqual(result["resource"]["url"], module_url)


    def test_wrong_product_identity_or_digest_is_rejected(self):
        for key, value in (
            ("product", "not-van-gogh"),
            ("package_schema", "unsupported"),
            ("package_version", None),
            ("foundation_version", None),
            ("component_version", "2.0.0-staging.7"),
            ("sha256", "z" * 64),
        ):
            manifest = self.manifest()
            manifest[key] = value
            with self.assertRaises(InstallError):
                app._validate_product_release_manifest(manifest)

    def test_review_refresh_changes_only_exact_review_path(self):
        state = {
            "rows": [
                {"id": "family-id", "url_path": "unrelated-family", "title": "Family", "mode": "storage"},
                {"id": "review-id", "url_path": "van-gogh-c-grid-review", "title": "Review", "mode": "storage"},
            ],
            "configs": {
                "van-gogh-c-grid-review": {"views": [{"title": "old"}]},
            },
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        candidate = {"views": [{"title": "accepted"}]}
        result = client.save_review_dashboard("van-gogh-c-grid-review", candidate, title="Review")
        self.assertTrue(result["readback_verified"])
        self.assertEqual(state["configs"]["van-gogh-c-grid-review"], candidate)
        self.assertEqual([row["url_path"] for row in state["rows"]], ["unrelated-family", "van-gogh-c-grid-review"])
        mutating = [cmd for cmd in state["commands"] if cmd["type"] not in {"lovelace/dashboards/list", "lovelace/config"}]
        self.assertEqual([cmd["type"] for cmd in mutating], ["lovelace/dashboards/update", "lovelace/config/save"])
        update, save = mutating
        self.assertNotIn("url_path", update)
        self.assertEqual(save.get("url_path"), "van-gogh-c-grid-review")

    def test_review_dashboard_create_keeps_mode_but_existing_updates_omit_it(self):
        existing = {
            "rows": [
                {"id": "review-id", "url_path": "van-gogh-c-grid-review", "title": "Review", "mode": "storage"},
            ],
            "configs": {"van-gogh-c-grid-review": {"views": [{}]}},
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(existing)
        client.save_review_dashboard("van-gogh-c-grid-review", {"views": [{"title": "accepted"}]}, title="Review")
        client.restore_dashboard({
            "url_path": "van-gogh-c-grid-review",
            "exists": True,
            "registry": dict(existing["rows"][0]),
            "config": {"views": [{"title": "accepted"}]},
        })
        updates = [cmd for cmd in existing["commands"] if cmd["type"] == "lovelace/dashboards/update"]
        self.assertEqual(len(updates), 2)
        self.assertTrue(all("mode" not in command and "url_path" not in command for command in updates))
        self.assertEqual(existing["rows"][0]["mode"], "storage")

        missing = {"rows": [], "configs": {}}
        client.websocket = lambda: FakeConnection(missing)
        client.save_review_dashboard("new-review", {"views": [{}]}, title="Review")
        creates = [cmd for cmd in missing["commands"] if cmd["type"] == "lovelace/dashboards/create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["mode"], "storage")

    def test_review_rollback_deletes_only_new_review_dashboard(self):
        state = {
            "rows": [
                {"id": "family-id", "url_path": "unrelated-family", "title": "Family", "mode": "storage"},
                {"id": "review-id", "url_path": "van-gogh-c-grid-review", "title": "Review", "mode": "storage"},
            ],
            "configs": {"van-gogh-c-grid-review": {"views": [{}]}},
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        result = client.restore_dashboard({"url_path": "van-gogh-c-grid-review", "exists": False})
        self.assertTrue(result["readback_verified"])
        self.assertEqual([row["url_path"] for row in state["rows"]], ["unrelated-family"])
        deletes = [cmd for cmd in state["commands"] if cmd["type"] == "lovelace/dashboards/delete"]
        self.assertEqual(deletes, [{"type": "lovelace/dashboards/delete", "dashboard_id": "review-id"}])

    def test_invalid_dashboard_path_fails_closed(self):
        client = HAClient(token="test")
        with self.assertRaises(HAError):
            client.save_review_dashboard("../unrelated-family", {"views": [{}]}, title="Review")

if __name__ == "__main__":
    unittest.main()
