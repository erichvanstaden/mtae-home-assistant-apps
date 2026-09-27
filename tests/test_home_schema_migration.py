from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from dashboard_migration import (  # noqa: E402
    GRID_TYPE,
    LEGACY_BAY_TYPE,
    MigrationError,
    discover_dashboard,
    plan_config,
)
from ha_client import HAClient, HAError  # noqa: E402


BAY_NAMES = ("C1-C2", "C3-C4", "C5-C6", "C7-C8")


def module(kind: str, name: str) -> dict:
    return {
        "type": "custom:van-gogh2-home-module-card",
        "schema_version": 1,
        "kind": kind,
        "name": name,
        "entities": [f"sensor.{name.lower().replace(' ', '_')}"],
        "tap_action": {"action": "none"},
    }


def legacy_dashboard(route_prefix: str = "customer-a") -> dict:
    bays = [
        {"type": LEGACY_BAY_TYPE, "schema_version": 1, "bay": "C1-C2", "mode": "empty", "cards": []},
        {"type": LEGACY_BAY_TYPE, "schema_version": 1, "bay": "C3-C4", "mode": "large", "cards": [module("lighting", "Local lights")]},
        {"type": LEGACY_BAY_TYPE, "schema_version": 1, "bay": "C5-C6", "mode": "split", "cards": [module("security", "Local security"), module("media", "Local media")]},
        {"type": LEGACY_BAY_TYPE, "schema_version": 1, "bay": "C7-C8", "mode": "split", "cards": [module("windows", "Local windows")]},
    ]
    return {
        "title": "Customer-chosen title",
        "local_manifest": {"enabled_features": ["lighting", "windows"], "pool_enabled": False},
        "views": [
            {
                "type": "custom:van-gogh-wall10-view",
                "title": "Owner-chosen Home title",
                "path": "owner-home-route",
                "cards": [
                    {"type": "custom:van-gogh2-home-header-card", "schema_version": 1, "site_name": "Local display name"},
                    {"type": "custom:van-gogh2-home-climate-card", "schema_version": 1, "entity": "climate.local", "zones": ["climate.zone_a"]},
                    {"type": "custom:site-owned-card", "binding": "sensor.preserved"},
                    *bays,
                    {"type": "custom:van-gogh2-quick-actions-card", "schema_version": 1, "actions": [{"entity": "switch.local"}]},
                    {"type": "custom:van-gogh2-navigation-card", "schema_version": 1, "routes": [{"path": f"/{route_prefix}/owner-home-route"}]},
                ],
            },
            {
                "type": "custom:site-optional-view",
                "title": "Pools",
                "enabled": False,
                "cards": [{"type": "entities", "entities": ["switch.client_pool"]}],
            },
        ],
    }


def snapshot(path: str, config: dict) -> dict:
    return {
        "registry": {
            "id": f"id-{path}",
            "url_path": path,
            "title": "Local dashboard name",
            "mode": "storage",
            "show_in_sidebar": True,
        },
        "config": config,
    }


class FakeConnection:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def call(self, message: dict):
        self.state.setdefault("commands", []).append(deepcopy(message))
        kind = message["type"]
        if kind == "lovelace/dashboards/list":
            return deepcopy(self.state["rows"])
        if kind == "lovelace/config":
            return deepcopy(self.state["configs"][message["url_path"]])
        if kind == "lovelace/config/save":
            self.state["configs"][message["url_path"]] = deepcopy(message["config"])
            return None
        raise AssertionError(f"unexpected command {message}")


class GenericHomeMigrationTests(unittest.TestCase):
    def test_supported_upgrade_is_generic_and_preserves_local_configuration(self):
        before = legacy_dashboard("bespoke-client-route")
        planned = plan_config(before)
        self.assertIsNotNone(planned)
        state, after, view_index = planned
        self.assertEqual(state, "upgrade")
        self.assertEqual(view_index, 0)
        self.assertEqual(before["local_manifest"], after["local_manifest"])
        self.assertEqual(before["views"][1], after["views"][1])
        before_cards = before["views"][0]["cards"]
        after_cards = after["views"][0]["cards"]
        self.assertEqual(before_cards[:3], after_cards[:3])
        self.assertEqual(before_cards[7:], after_cards[4:])
        grids = [card for card in after_cards if card.get("type") == GRID_TYPE]
        self.assertEqual(len(grids), 1)
        children = grids[0]["cards"]
        self.assertEqual([(card["slot"], card["span"]) for card in children], [("C3", 2), ("C5", 1), ("C6", 1), ("C7", 1)])
        self.assertTrue(children[-1]["legacy_footprint"])
        self.assertEqual(children[0]["entities"], ["sensor.local_lights"])
        self.assertEqual(before, legacy_dashboard("bespoke-client-route"), "input was mutated")

    def test_clean_install_current_schema_is_idempotent_with_zero_writes(self):
        before = legacy_dashboard("another-client")
        _, current, _ = plan_config(before)
        plan = discover_dashboard([snapshot("arbitrary-local-dashboard", current)])
        self.assertEqual(plan.state, "current")
        self.assertFalse(plan.changed)
        self.assertEqual(plan.before, plan.after)
        self.assertEqual(plan.url_path, "arbitrary-local-dashboard")

    def test_marker_discovery_does_not_depend_on_title_or_path(self):
        plan = discover_dashboard([
            snapshot("ordinary-dashboard", {"title": "Unrelated", "views": [{"cards": []}]}),
            snapshot("client-selected-path", legacy_dashboard("client-selected-path")),
        ])
        self.assertEqual(plan.url_path, "client-selected-path")
        self.assertEqual(plan.state, "upgrade")

    def test_unsupported_or_ambiguous_layouts_fail_closed(self):
        malformed = legacy_dashboard()
        malformed["views"][0]["cards"][3]["bay"] = "C99"
        with self.assertRaises(MigrationError):
            discover_dashboard([snapshot("malformed", malformed)])
        with self.assertRaises(MigrationError):
            discover_dashboard([
                snapshot("customer-one", legacy_dashboard("customer-one")),
                snapshot("customer-two", legacy_dashboard("customer-two")),
            ])
        with self.assertRaises(MigrationError):
            discover_dashboard([snapshot("ordinary", {"views": [{"cards": []}]})])

    def test_inventory_failure_paths_issue_no_home_assistant_writes(self):
        malformed = legacy_dashboard()
        malformed["views"][0]["cards"][3]["bay"] = "unsupported"
        state = {
            "rows": [{"id": "x", "url_path": "local-path", "title": "X", "mode": "storage"}],
            "configs": {"local-path": malformed},
            "commands": [],
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        inventory = client.storage_dashboard_inventory()
        with self.assertRaises(MigrationError):
            discover_dashboard(inventory)
        self.assertTrue(state["commands"])
        self.assertTrue(all(command["type"] != "lovelace/config/save" for command in state["commands"]))

    def test_compare_and_save_changes_only_discovered_config(self):
        before = legacy_dashboard("local-choice")
        _, after, _ = plan_config(before)
        unrelated = {"title": "Map", "views": [{"cards": [{"type": "map"}]}]}
        state = {
            "rows": [
                {"id": "map", "url_path": "map-local", "title": "Map", "mode": "storage"},
                {"id": "vg", "url_path": "local-choice", "title": "VG", "mode": "storage"},
            ],
            "configs": {"map-local": deepcopy(unrelated), "local-choice": deepcopy(before)},
            "commands": [],
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        result = client.save_existing_dashboard("local-choice", before, after)
        self.assertTrue(result["readback_verified"])
        self.assertEqual(result["dashboard_writes"], 1)
        self.assertEqual(state["configs"]["local-choice"], after)
        self.assertEqual(state["configs"]["map-local"], unrelated)
        writes = [command for command in state["commands"] if command["type"] == "lovelace/config/save"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["url_path"], "local-choice")

    def test_compare_and_save_rejects_race_before_any_write(self):
        before = legacy_dashboard("race")
        _, after, _ = plan_config(before)
        changed = deepcopy(before)
        changed["owner_change"] = True
        state = {
            "rows": [{"id": "vg", "url_path": "race", "title": "VG", "mode": "storage"}],
            "configs": {"race": changed},
            "commands": [],
        }
        client = HAClient(token="test")
        client.websocket = lambda: FakeConnection(state)
        with self.assertRaises(HAError):
            client.save_existing_dashboard("race", before, after)
        self.assertFalse(any(command["type"] == "lovelace/config/save" for command in state["commands"]))
        self.assertEqual(state["configs"]["race"], changed)


if __name__ == "__main__":
    unittest.main()
