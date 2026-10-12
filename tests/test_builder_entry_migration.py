from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

from ha_client import (  # noqa: E402
    BUILDER_DASHBOARD_PATH,
    HAClient,
    HAError,
    builder_dashboard_config,
)


LEGACY_1_1_17_CONFIG = {
    "views": [
        {
            "title": "Van Gogh Builder",
            "path": "builder",
            "icon": "mdi:palette-outline",
            "cards": [{"type": "custom:van-gogh-builder-card"}],
        }
    ]
}


def builder_row(config: dict) -> dict:
    return {
        "registry": {
            "id": "van_gogh_builder",
            "url_path": BUILDER_DASHBOARD_PATH,
            "title": "Van Gogh Builder",
            "icon": "mdi:palette",
            "show_in_sidebar": True,
            "require_admin": False,
            "mode": "storage",
        },
        "config": copy.deepcopy(config),
    }


class FakeWebSocket:
    MUTATING_COMMANDS = {
        "lovelace/config/save",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/update",
    }

    def __init__(self, rows: list[dict]) -> None:
        self.rows = copy.deepcopy(rows)
        self.commands: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _row(self, url_path: str) -> dict | None:
        return next(
            (
                row
                for row in self.rows
                if row["registry"]["url_path"] == url_path
            ),
            None,
        )

    def call(self, command: dict):
        self.commands.append(copy.deepcopy(command))
        command_type = command["type"]
        if command_type == "lovelace/dashboards/list":
            return [copy.deepcopy(row["registry"]) for row in self.rows]
        if command_type == "lovelace/config":
            row = self._row(command["url_path"])
            if row is None:
                raise HAError("synthetic missing dashboard")
            return copy.deepcopy(row["config"])
        if command_type == "lovelace/config/save":
            row = self._row(command["url_path"])
            if row is None:
                raise HAError("synthetic missing dashboard")
            row["config"] = copy.deepcopy(command["config"])
            return None
        if command_type == "lovelace/dashboards/create":
            self.rows.append(
                {
                    "registry": {
                        "id": command["url_path"].replace("-", "_"),
                        "url_path": command["url_path"],
                        "title": command["title"],
                        "icon": command.get("icon"),
                        "show_in_sidebar": command.get("show_in_sidebar", True),
                        "require_admin": command.get("require_admin", False),
                        "mode": "storage",
                    },
                    "config": {},
                }
            )
            return None
        if command_type == "lovelace/dashboards/update":
            row = next(
                (
                    candidate
                    for candidate in self.rows
                    if candidate["registry"]["id"] == command["dashboard_id"]
                ),
                None,
            )
            if row is None:
                raise HAError("synthetic missing dashboard")
            for key in (
                "title",
                "icon",
                "show_in_sidebar",
                "require_admin",
            ):
                if key in command:
                    row["registry"][key] = command[key]
            return None
        if command_type == "lovelace/dashboards/delete":
            self.rows = [
                row
                for row in self.rows
                if row["registry"]["url_path"] != command["url_path"]
            ]
            return None
        raise AssertionError(f"Unexpected command: {command}")

    @property
    def mutating_commands(self) -> list[dict]:
        return [
            command
            for command in self.commands
            if command["type"] in self.MUTATING_COMMANDS
        ]


class BuilderEntryMigrationTests(unittest.TestCase):
    def client(self, config: dict) -> tuple[HAClient, FakeWebSocket]:
        fake = FakeWebSocket([builder_row(config)])
        client = HAClient("test-token")
        client.websocket = lambda: fake
        return client, fake

    def test_exact_installer_1_1_17_entry_migrates_once(self) -> None:
        client, fake = self.client(LEGACY_1_1_17_CONFIG)

        result = client.ensure_builder_dashboard()

        self.assertEqual(fake.rows[0]["config"], builder_dashboard_config())
        self.assertEqual(len(fake.mutating_commands), 1)
        self.assertEqual(
            fake.mutating_commands[0]["type"],
            "lovelace/config/save",
        )
        self.assertTrue(result["migrated"])
        self.assertEqual(result["migrated_from_installer"], "1.1.17")
        self.assertEqual(result["dashboard_writes"], 1)
        self.assertTrue(result["readback_verified"])

    def test_current_entry_is_zero_write_idempotent(self) -> None:
        current = builder_dashboard_config()
        client, fake = self.client(current)

        result = client.ensure_builder_dashboard()

        self.assertEqual(fake.rows[0]["config"], current)
        self.assertEqual(fake.mutating_commands, [])
        self.assertFalse(result["created"])
        self.assertFalse(result["migrated"])
        self.assertEqual(result["dashboard_writes"], 0)
        self.assertTrue(result["readback_verified"])

    def test_unknown_collision_fails_closed_without_write(self) -> None:
        unknown = copy.deepcopy(LEGACY_1_1_17_CONFIG)
        unknown["views"][0]["title"] = "Owner edited Builder"
        client, fake = self.client(unknown)
        before = copy.deepcopy(fake.rows)

        with self.assertRaisesRegex(
            HAError,
            "unrecognised Builder dashboard collision",
        ):
            client.ensure_builder_dashboard()

        self.assertEqual(fake.rows, before)
        self.assertEqual(fake.mutating_commands, [])

    def test_migration_rollback_restores_exact_1_1_17_prestate(self) -> None:
        client, fake = self.client(LEGACY_1_1_17_CONFIG)
        before_rows = copy.deepcopy(fake.rows)
        before = client.dashboard_snapshot(BUILDER_DASHBOARD_PATH)

        migration = client.ensure_builder_dashboard()
        self.assertTrue(migration["migrated"])
        self.assertEqual(fake.rows[0]["config"], builder_dashboard_config())

        rollback = client.restore_dashboard(before)

        self.assertEqual(fake.rows, before_rows)
        self.assertTrue(rollback["readback_verified"])


if __name__ == "__main__":
    unittest.main()
