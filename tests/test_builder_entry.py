#!/usr/bin/env python3
"""Builder first-run entry and rollback contract tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from ha_client import (  # noqa: E402
    BUILDER_DASHBOARD_PATH,
    HAClient,
    builder_dashboard_config,
)


class TestBuilderEntry(unittest.TestCase):
    def test_config_is_one_supported_builder_card(self) -> None:
        self.assertEqual(
            builder_dashboard_config(),
            {
                "views": [
                    {
                        "title": "Van Gogh Builder",
                        "path": "builder",
                        "icon": "mdi:palette-outline",
                        "cards": [{"type": "custom:van-gogh-builder-card"}],
                    }
                ]
            },
        )

    def test_clean_first_run_creates_and_returns_owner_route(self) -> None:
        client = HAClient(token="test")
        client.dashboard_snapshot = mock.Mock(return_value={"url_path": BUILDER_DASHBOARD_PATH, "exists": False})  # type: ignore[method-assign]
        client.save_review_dashboard = mock.Mock(return_value={"created": True, "url_path": BUILDER_DASHBOARD_PATH, "readback_verified": True})  # type: ignore[method-assign]
        result = client.ensure_builder_dashboard()
        self.assertTrue(result["created"])
        self.assertEqual(result["owner_route"], "/van-gogh-builder/builder")
        client.save_review_dashboard.assert_called_once_with(
            BUILDER_DASHBOARD_PATH,
            builder_dashboard_config(),
            title="Van Gogh Builder",
        )

    def test_existing_owner_dashboard_is_not_overwritten(self) -> None:
        client = HAClient(token="test")
        client.dashboard_snapshot = mock.Mock(return_value={"url_path": BUILDER_DASHBOARD_PATH, "exists": True, "config": {"owner": True}})  # type: ignore[method-assign]
        client.save_review_dashboard = mock.Mock()  # type: ignore[method-assign]
        result = client.ensure_builder_dashboard()
        self.assertFalse(result["created"])
        self.assertTrue(result["preserved_existing"])
        client.save_review_dashboard.assert_not_called()

    def test_snapshot_includes_builder_dashboard_prestate(self) -> None:
        client = HAClient(token="test")
        client.entries = mock.Mock(return_value=[])  # type: ignore[method-assign]
        client.resources = mock.Mock(return_value=[])  # type: ignore[method-assign]
        expected = {"url_path": BUILDER_DASHBOARD_PATH, "exists": False}
        client.dashboard_snapshot = mock.Mock(return_value=expected)  # type: ignore[method-assign]
        self.assertEqual(client.snapshot()["builder_dashboard"], expected)


if __name__ == "__main__":
    unittest.main()
