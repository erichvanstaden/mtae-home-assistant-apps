#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
import unittest

APP = Path(__file__).resolve().parents[1] / "van_gogh_2/rootfs/app"
sys.path.insert(0, str(APP))

from product_update import (  # noqa: E402
    ProductPackage,
    ProductUpdateError,
    _compose_dashboard,
    _materialise_media_bindings,
    _site_for_dashboard_target,
    plan_update,
)


def package() -> ProductPackage:
    return ProductPackage(
        manifest={
            "package_version": "2.0.0-suite.2-owner-corrections.44",
            "component_version": "2.0.0-suite.2-owner-corrections.44",
            "foundation": {"version": "1.5.0"},
            "dashboard_route_replacements": {
                "wall-10": ["media"],
                "mobile/app": ["media"],
            },
        },
        catalogue={"catalogue_version": "1.0.0", "features": {}},
        site_schema={},
        archive=Path("candidate.44.tar.gz"),
        archive_sha256="0" * 64,
    )


class MediaRouteReplacementTest(unittest.TestCase):
    def test_wall_upgrade_keeps_profile_route_identity_separate_from_storage_path(self) -> None:
        site = {
            "schema_version": 1,
            "site": {"id": "neutral-target", "title": "Accepted site"},
            "panel": {"id": "neutral-target-wall10", "profile": "wall-10"},
        }
        inventory = [
            {
                "registry": {"url_path": "installed-storage"},
                "config": {
                    "mtae_profile": {
                        "id": "wall-10",
                        "dashboard_path": "accepted-wall-routes",
                    }
                },
            }
        ]

        target = _site_for_dashboard_target(site, "wall-10", inventory)

        self.assertEqual(target["site"]["id"], "installed-storage")
        self.assertEqual(target["panel"]["id"], "accepted-wall-routes")

    def test_installed_profile_route_identity_must_be_a_safe_path(self) -> None:
        site = {
            "schema_version": 1,
            "site": {"id": "neutral-target", "title": "Accepted site"},
            "panel": {"id": "neutral-target-wall10", "profile": "wall-10"},
        }
        inventory = [
            {
                "registry": {"url_path": "installed-storage"},
                "config": {
                    "mtae_profile": {
                        "id": "wall-10",
                        "dashboard_path": "../unsafe",
                    }
                },
            }
        ]

        with self.assertRaisesRegex(ProductUpdateError, "invalid profile path"):
            _site_for_dashboard_target(site, "wall-10", inventory)

    def test_legacy_profile_without_route_identity_keeps_storage_path_fallback(self) -> None:
        site = {
            "schema_version": 1,
            "site": {"id": "neutral-target", "title": "Accepted site"},
            "panel": {"id": "neutral-target-wall10", "profile": "wall-10"},
        }
        inventory = [
            {
                "registry": {"url_path": "legacy-storage"},
                "config": {"mtae_profile": {"id": "wall-10"}},
            }
        ]

        target = _site_for_dashboard_target(site, "wall-10", inventory)

        self.assertEqual(target["site"]["id"], "legacy-storage")
        self.assertEqual(target["panel"]["id"], "legacy-storage")

    def test_wall_upgrade_reconciles_one_shared_navigation_per_route(self) -> None:
        existing_home = {
            "title": "Existing home",
            "path": "home",
            "cards": [
                {
                    "type": "custom:van-gogh2-navigation-card",
                    "selected": "legacy-home",
                    "routes": [
                        {"id": "home", "path": "/legacy/home", "name": "Home"},
                        {"id": "settings", "path": "/legacy/settings", "name": "Settings"},
                    ],
                    "owner_setting": "preserve-me",
                }
            ],
        }
        existing_settings = {
            "title": "Existing settings",
            "path": "settings",
            "cards": [
                {
                    "type": "custom:van-gogh2-navigation-card",
                    "selected": "legacy-settings",
                    "routes": [
                        {"id": "home", "path": "/legacy/home", "name": "Home"},
                        {"id": "settings", "path": "/legacy/settings", "name": "Settings"},
                    ],
                }
            ],
        }
        existing_media = {
            "title": "Existing media",
            "path": "media",
            "cards": [{"type": "custom:legacy-media-card"}],
        }
        canonical_routes = [
            {"id": "home", "path": "/test/home", "name": "Home"},
            {
                "id": "media",
                "path": "/test/media",
                "name": "Media",
                "icon": "mdi:play-circle-outline",
            },
            {"id": "settings", "path": "/test/settings", "name": "Settings"},
        ]
        generated = {
            "title": "Generated",
            "views": [
                {
                    "title": "Generated home",
                    "path": "home",
                    "cards": [
                        {
                            "type": "custom:van-gogh2-navigation-card",
                            "selected": "home",
                            "routes": canonical_routes,
                        }
                    ],
                },
                {
                    "title": "Media",
                    "path": "media",
                    "type": "custom:van-gogh-wall10-view",
                    "cards": [
                        {
                            "type": "custom:van-gogh2-feature-page-card",
                            "page": "media",
                        },
                        {
                            "type": "custom:van-gogh2-navigation-card",
                            "selected": "media",
                            "routes": canonical_routes,
                        },
                    ],
                },
                {
                    "title": "Generated settings",
                    "path": "settings",
                    "cards": [
                        {
                            "type": "custom:van-gogh2-navigation-card",
                            "selected": "settings",
                            "routes": canonical_routes,
                        }
                    ],
                },
            ],
        }
        site = {
            "schema_version": 1,
            "site": {"id": "test", "title": "Test"},
            "panel": {"profile": "wall-10"},
        }

        result = _compose_dashboard(
            generated,
            {
                "title": "Installed",
                "views": [existing_home, existing_settings, existing_media],
            },
            package(),
            site,
        )
        installed_catalogue_routes = [
            canonical_routes[0],
            canonical_routes[2],
            canonical_routes[1],
        ]
        home = next(view for view in result["views"] if view["path"] == "home")
        navigation = home["cards"][0]

        self.assertEqual(
            [view["path"] for view in result["views"]],
            ["home", "settings", "media"],
        )
        self.assertEqual(navigation["routes"], installed_catalogue_routes)
        self.assertEqual(navigation["owner_setting"], "preserve-me")
        self.assertEqual(navigation["selected"], "home")
        settings = next(view for view in result["views"] if view["path"] == "settings")
        self.assertEqual(settings["cards"][0]["routes"], installed_catalogue_routes)
        self.assertEqual(settings["cards"][0]["selected"], "settings")
        media = next(view for view in result["views"] if view["path"] == "media")
        media_navigation = [
            card
            for card in media["cards"]
            if card.get("type") == "custom:van-gogh2-navigation-card"
        ]
        self.assertEqual(len(media_navigation), 1)
        self.assertEqual(media_navigation[0]["selected"], "media")
        self.assertEqual(media_navigation[0]["routes"], installed_catalogue_routes)

    def test_explicit_media_replacement_preserves_unrelated_existing_view(self) -> None:
        existing_home = {
            "title": "Existing home",
            "path": "home",
            "cards": [{"type": "markdown", "content": "owner content"}],
        }
        existing_media = {
            "title": "Generic media",
            "path": "media",
            "cards": [{"type": "custom:legacy-media-card"}],
        }
        generated = {
            "title": "Generated",
            "views": [
                {"title": "Generated home", "path": "home", "cards": []},
                {
                    "title": "Media",
                    "path": "media",
                    "cards": [
                        {
                            "type": "custom:van-gogh2-feature-page-card",
                            "page": "media",
                        }
                    ],
                },
            ],
        }
        existing = {
            "title": "Installed",
            "views": [existing_home, existing_media],
        }
        site = {
            "schema_version": 1,
            "site": {"id": "test", "title": "Test"},
            "panel": {"profile": "mobile/app"},
        }

        result = _compose_dashboard(generated, existing, package(), site)
        views = {view["path"]: view for view in result["views"]}

        self.assertEqual(views["home"], existing_home)
        self.assertEqual(
            views["media"]["cards"][0]["type"],
            "custom:van-gogh2-feature-page-card",
        )
        self.assertNotEqual(views["media"], existing_media)

    def test_site_media_bindings_are_grouped_into_each_production_wrapper(self) -> None:
        dashboard = {
            "views": [
                {
                    "path": "media",
                    "cards": [
                        {
                            "type": "custom:van-gogh2-feature-page-card",
                            "page": "media",
                        },
                        {
                            "type": "custom:mtae-mobile-dashboard-card",
                            "initial_page": "media",
                        },
                    ],
                },
                {"path": "home", "cards": [{"type": "markdown"}]},
            ]
        }
        site = {
            "rooms": [
                {"id": "living", "name": "Living Room"},
                {"id": "kitchen", "name": "Kitchen"},
            ],
            "bindings": {
                "living_music": {
                    "entity_id": "media_player.living_music",
                    "capability": "media",
                    "room": "living",
                },
                "living_tv": {
                    "entity_id": "media_player.living_tv",
                    "capability": "media",
                    "room": "living",
                },
                "kitchen_music": {
                    "entity_id": "media_player.kitchen",
                    "capability": "media",
                    "room": "kitchen",
                },
            },
            "features": {"media": True},
            "content": {
                "media": ["living_music", "living_tv", "kitchen_music"],
            },
        }

        result = _materialise_media_bindings(dashboard, site)
        expected = [
            {
                "id": "living",
                "name": "Living Room",
                "icon": "mdi:speaker",
                "entities": [
                    "media_player.living_music",
                    "media_player.living_tv",
                ],
            },
            {
                "id": "kitchen",
                "name": "Kitchen",
                "icon": "mdi:speaker",
                "entities": ["media_player.kitchen"],
            },
        ]

        wrappers = result["views"][0]["cards"]
        self.assertEqual(wrappers[0]["media_rooms"], expected)
        self.assertEqual(wrappers[1]["media_rooms"], expected)
        self.assertEqual(result["views"][1], dashboard["views"][1])
        self.assertNotIn("media_rooms", dashboard["views"][0]["cards"][0])

    def test_accepted_legacy_mobile_profile_replaces_only_media_route(self) -> None:
        existing_home = {
            "title": "Existing home",
            "path": "home",
            "type": "custom:van-gogh-mobile-home-view",
            "cards": [
                {
                    "type": "custom:van-gogh-mobile-navigation-card",
                    "routes": [
                        {"id": "home", "path": "/test-mobile/home"},
                        {"id": "media", "path": "/test-mobile/media"},
                    ],
                }
            ],
        }
        existing_media = {
            "title": "Generic media",
            "path": "media",
            "type": "custom:van-gogh-mobile-home-view",
            "cards": [{"type": "grid"}],
        }
        before = {
            "title": "Installed Mobile",
            "mtae_profile": {
                "id": "mobile-app",
                "site_manifest_identity": "accepted:test-mobile",
            },
            "views": [existing_home, existing_media],
        }
        generated = {
            "title": "Generated Mobile",
            "mtae_profile": {"id": "mobile-app"},
            "views": [
                {"title": "Generated home", "path": "home", "cards": []},
                {
                    "title": "Media",
                    "path": "media",
                    "cards": [
                        {
                            "type": "custom:mtae-mobile-dashboard-card",
                            "initial_page": "media",
                            "pages": ["home", "media", "more"],
                        }
                    ],
                },
            ],
        }
        site = {
            "schema_version": 1,
            "site": {"id": "test", "title": "Test"},
            "panel": {"profile": "mobile/app"},
        }
        inventory = [
            {
                "registry": {
                    "id": "test_mobile",
                    "title": "Installed Mobile",
                    "url_path": "test-mobile",
                },
                "config": before,
            }
        ]

        plan = plan_update(package(), site, inventory, generated)
        views = {view["path"]: view for view in plan.after["views"]}

        self.assertEqual(plan.state, "legacy_profile_upgrade")
        self.assertEqual(plan.url_path, "test-mobile")
        self.assertEqual(views["home"], existing_home)
        self.assertEqual(
            views["media"]["cards"][0]["type"],
            "custom:mtae-mobile-dashboard-card",
        )
        self.assertEqual(plan.before, before)

    def test_occupied_mobile_path_without_accepted_profile_identity_fails_closed(self) -> None:
        site = {
            "schema_version": 1,
            "site": {"id": "test", "title": "Test"},
            "panel": {"profile": "mobile/app"},
        }
        generated = {
            "title": "Generated Mobile",
            "views": [
                {"path": "home", "cards": []},
                {"path": "media", "cards": []},
            ],
        }
        inventory = [
            {
                "registry": {"url_path": "test-mobile", "title": "Other"},
                "config": {
                    "mtae_profile": {
                        "id": "mobile-app",
                        "site_manifest_identity": "accepted:other-mobile",
                    },
                    "views": [
                        {
                            "path": "home",
                            "type": "custom:van-gogh-mobile-home-view",
                            "cards": [
                                {"type": "custom:van-gogh-mobile-navigation-card"}
                            ],
                        },
                        {
                            "path": "media",
                            "type": "custom:van-gogh-mobile-home-view",
                            "cards": [],
                        },
                    ],
                },
            }
        ]

        with self.assertRaisesRegex(
            ProductUpdateError, "Clean-install dashboard path is already occupied"
        ):
            plan_update(package(), site, inventory, generated)


if __name__ == "__main__":
    unittest.main()
