#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1] / "van_gogh_2/rootfs/app"
sys.path.insert(0, str(APP))

from product_update import (  # noqa: E402
    MANAGED_FOUNDATION,
    PRODUCT_MARKER,
    PRODUCT_SCHEMA_VERSION,
    ProductPackage,
    _compose_dashboard,
)


ROUTES = [
    "home",
    "lights",
    "windows",
    "ac",
    "rooms",
    "security",
    "irrigation",
    "settings",
]


def product65() -> ProductPackage:
    return ProductPackage(
        manifest={
            "package_version": "2.0.0-suite.2-owner-corrections.65",
            "component_version": "2.0.0-suite.2-owner-corrections.65",
            "foundation": {"version": "1.5.0"},
            "dashboard_route_replacements": {"wall-10": ["settings"]},
        },
        catalogue={"catalogue_version": "1.0.0", "features": {}},
        site_schema={},
        archive=Path("product65.tar.gz"),
        archive_sha256="5f99409358421c2249640f174314341ce956a01c79d82aca17cb2f56ec844299",
    )


def generated_dashboard() -> dict[str, object]:
    views: list[dict[str, object]] = []
    for path in ROUTES:
        card: dict[str, object] = {
            "type": "custom:van-gogh2-feature-page-card",
            "page": path,
        }
        if path == "rooms":
            card = {
                "type": "custom:van-gogh2-room-card",
                "name": "Room",
            }
        views.append(
            {
                "title": path.title(),
                "path": path,
                "cards": [card],
            }
        )
    return {
        "title": "Generated Product .65",
        "mtae_profile": {"id": "wall-10", "dashboard_path": "clean-room-site-wall10"},
        "wallpanel": {"enabled": True},
        "views": views,
    }


def owner_edited_product59_dashboard() -> dict[str, object]:
    views = deepcopy(generated_dashboard()["views"])
    assert isinstance(views, list)
    for view in views:
        assert isinstance(view, dict)
        view[MANAGED_FOUNDATION] = {
            "schema_version": PRODUCT_SCHEMA_VERSION,
            "foundation_version": "1.5.0",
        }
        view["mtae_native_edit_qualification"] = True
        if view.get("path") == "rooms":
            cards = view["cards"]
            assert isinstance(cards, list) and isinstance(cards[0], dict)
            cards[0]["name"] = "Qualification Room"
            cards[0]["mtae_native_edit_qualification"] = True
    return {
        "title": "Installed Product .59",
        "mtae_profile": {"id": "wall-10", "dashboard_path": "clean-room-site-wall10"},
        "wallpanel": {"enabled": True},
        "mtae_site_data_qualification": {"site": "preserve-me"},
        PRODUCT_MARKER: {
            "schema_version": PRODUCT_SCHEMA_VERSION,
            "package_version": "2.0.0-suite.2-owner-corrections.59",
            "component_version": "2.0.0-suite.2-owner-corrections.59",
            "foundation_version": "1.5.0",
            "site_id": "clean-room-site",
            "site_schema_version": 1,
            "feature_catalogue_version": "1.0.0",
            "enabled_features": [],
        },
        "views": views,
    }


class NativeOwnerEditPreservationTest(unittest.TestCase):
    def test_product_upgrade_preserves_all_owner_edited_managed_views_exactly(self) -> None:
        before = owner_edited_product59_dashboard()
        before_snapshot = deepcopy(before)
        generated = generated_dashboard()
        site = {
            "schema_version": 1,
            "site": {"id": "clean-room-site", "title": "Clean Room Site"},
            "panel": {"id": "clean-room-site-wall10", "profile": "wall-10"},
            "features": {},
        }

        result = _compose_dashboard(generated, before, product65(), site)

        self.assertEqual(result["views"], before_snapshot["views"])
        self.assertEqual(
            [view["path"] for view in result["views"]],
            ROUTES,
        )
        self.assertTrue(
            all(view["mtae_native_edit_qualification"] for view in result["views"])
        )
        rooms = next(view for view in result["views"] if view["path"] == "rooms")
        self.assertEqual(rooms["cards"][0]["name"], "Qualification Room")
        self.assertTrue(
            rooms["cards"][0]["mtae_native_edit_qualification"]
        )
        self.assertEqual(
            result["mtae_site_data_qualification"],
            before_snapshot["mtae_site_data_qualification"],
        )
        self.assertEqual(
            result[PRODUCT_MARKER]["package_version"],
            "2.0.0-suite.2-owner-corrections.65",
        )
        self.assertEqual(before, before_snapshot)


if __name__ == "__main__":
    unittest.main()
