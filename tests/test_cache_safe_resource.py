from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from ha_client import HAClient, HAError  # noqa: E402

OLD_URL = "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js"
NEW_URL = "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js?v=2.0.0-builder.5"


class ResourceConnection:
    def __init__(self, state: dict[str, object]):
        self.state = state

    def __enter__(self) -> "ResourceConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def call(self, command: dict[str, object]) -> object:
        commands = self.state.setdefault("commands", [])
        assert isinstance(commands, list)
        commands.append(dict(command))
        resources = self.state["resources"]
        assert isinstance(resources, list)
        kind = command["type"]
        if kind == "lovelace/resources":
            return [dict(item) for item in resources]
        if kind == "lovelace/resources/update":
            row = next(item for item in resources if item["id"] == command["resource_id"])
            row.update({"url": command["url"], "type": command["res_type"]})
            return None
        if kind == "lovelace/resources/create":
            resources.append(
                {
                    "id": f"resource-{len(resources) + 1}",
                    "url": command["url"],
                    "type": command["res_type"],
                }
            )
            return None
        if kind == "lovelace/resources/delete":
            self.state["resources"] = [
                item for item in resources if item["id"] != command["resource_id"]
            ]
            return None
        raise AssertionError(f"unexpected command: {command}")


class CacheSafeResourceTests(unittest.TestCase):
    def test_update_noop_and_rollback_preserve_one_resource_and_exact_url(self) -> None:
        state: dict[str, object] = {
            "resources": [{"id": "van-gogh-resource", "url": OLD_URL, "type": "module"}],
            "commands": [],
        }
        client = HAClient(token="test")
        client.websocket = lambda: ResourceConnection(state)  # type: ignore[method-assign]

        updated = client.ensure_resource(NEW_URL)
        self.assertEqual(updated, {"resource_id": "van-gogh-resource", "url": NEW_URL, "count": 1})
        resources = state["resources"]
        assert isinstance(resources, list)
        self.assertEqual(resources, [{"id": "van-gogh-resource", "url": NEW_URL, "type": "module"}])

        commands = state["commands"]
        assert isinstance(commands, list)
        first_mutations = [item for item in commands if item["type"] != "lovelace/resources"]
        self.assertEqual([item["type"] for item in first_mutations], ["lovelace/resources/update"])

        command_count = len(commands)
        self.assertEqual(client.ensure_resource(NEW_URL)["count"], 1)
        noop_mutations = [
            item for item in commands[command_count:] if item["type"] != "lovelace/resources"
        ]
        self.assertEqual(noop_mutations, [])

        rollback = client.restore_resources(
            [{"id": "van-gogh-resource", "url": OLD_URL, "type": "module"}]
        )
        self.assertTrue(rollback["readback_verified"])
        self.assertTrue(rollback["resource_ids_preserved"])
        self.assertEqual(resources, [{"id": "van-gogh-resource", "url": OLD_URL, "type": "module"}])

    def test_unsafe_url_fails_before_home_assistant_connection(self) -> None:
        client = HAClient(token="test")
        client.websocket = lambda: self.fail("unsafe URL reached Home Assistant")  # type: ignore[method-assign]
        with self.assertRaises(HAError):
            client.ensure_resource("/local/van-gogh2/2.0.0-suite.1/van-gogh2.js")


if __name__ == "__main__":
    unittest.main()
