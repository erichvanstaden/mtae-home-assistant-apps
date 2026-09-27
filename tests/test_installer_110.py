from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))
os.environ.setdefault("VAN_GOGH_DATA_ROOT", tempfile.mkdtemp(prefix="vg-test-data-"))
os.environ.setdefault("VAN_GOGH_CONFIG_ROOT", tempfile.mkdtemp(prefix="vg-test-config-"))

import app  # noqa: E402
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


class Installer110ContractTests(unittest.TestCase):
    def manifest(self):
        return {
            "product": "van-gogh2",
            "version": app.EXPECTED_RELEASE,
            "sha256": app.EXPECTED_RELEASE_SHA256,
            "archive": "api/v1/van-gogh2/archive/van-gogh2-2.0.0-staging.6.tar.gz",
            "supported_home_assistant": {"minimum": "2026.8.3", "tested": "2026.8.3"},
        }

    def test_exact_release_manifest_is_accepted(self):
        manifest = self.manifest()
        self.assertIs(app._validate_release_manifest(manifest), manifest)

    def test_wrong_release_hash_or_ha_is_rejected(self):
        for key, value in (("version", "2.0.0-staging.5"), ("sha256", "0" * 64)):
            manifest = self.manifest()
            manifest[key] = value
            with self.assertRaises(InstallError):
                app._validate_release_manifest(manifest)
        manifest = self.manifest()
        manifest["supported_home_assistant"]["tested"] = "2026.8.4"
        with self.assertRaises(InstallError):
            app._validate_release_manifest(manifest)

    def test_review_payload_requires_nonempty_views(self):
        self.assertEqual(app._review_config({"config": {"views": [{}]}}), {"views": [{}]})
        for payload in ({}, {"config": []}, {"config": {"views": []}}):
            with self.assertRaises(InstallError):
                app._review_config(payload)

    def test_review_refresh_changes_only_exact_review_path(self):
        state = {
            "rows": [
                {"id": "family-id", "url_path": "ellie-family", "title": "Family", "mode": "storage"},
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
        self.assertEqual([row["url_path"] for row in state["rows"]], ["ellie-family", "van-gogh-c-grid-review"])
        mutating = [cmd for cmd in state["commands"] if cmd["type"] not in {"lovelace/dashboards/list", "lovelace/config"}]
        self.assertEqual([cmd["type"] for cmd in mutating], ["lovelace/dashboards/update", "lovelace/config/save"])
        self.assertTrue(all(cmd.get("url_path") == "van-gogh-c-grid-review" for cmd in mutating))

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
        self.assertTrue(all("mode" not in command for command in updates))
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
                {"id": "family-id", "url_path": "ellie-family", "title": "Family", "mode": "storage"},
                {"id": "review-id", "url_path": "van-gogh-c-grid-review", "title": "Review", "mode": "storage"},
            ],
            "configs": {"van-gogh-c-grid-review": {"views": [{}]}},
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        result = client.restore_dashboard({"url_path": "van-gogh-c-grid-review", "exists": False})
        self.assertTrue(result["readback_verified"])
        self.assertEqual([row["url_path"] for row in state["rows"]], ["ellie-family"])
        deletes = [cmd for cmd in state["commands"] if cmd["type"] == "lovelace/dashboards/delete"]
        self.assertEqual(deletes, [{"type": "lovelace/dashboards/delete", "dashboard_id": "review-id"}])

    def test_invalid_dashboard_path_fails_closed(self):
        client = HAClient(token="test")
        with self.assertRaises(HAError):
            client.save_review_dashboard("../ellie-family", {"views": [{}]}, title="Review")


if __name__ == "__main__":
    unittest.main()
