import copy
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))

from dashboard_migration import GRID_TYPE, HOME_VIEW_TYPE, discover_dashboard  # noqa: E402
from installer import InstallError, InstallManager  # noqa: E402
from product_update import (  # noqa: E402
    FOUNDATION_TARGETS,
    MANAGED_FOUNDATION,
    PRODUCT_MARKER,
    ProductUpdateError,
    ProductUpdateManager,
    inspect_package,
    validate_site_manifest,
)


class FakeHA:
    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows or [])
        self.dashboard_writes = 0
        self.fail_write = False

    def storage_dashboard_inventory(self):
        return copy.deepcopy(self.rows)

    def _row(self, path):
        return next(
            (
                row
                for row in self.rows
                if isinstance(row.get("registry"), dict)
                and row["registry"].get("url_path") == path
            ),
            None,
        )

    def dashboard_snapshot(self, path):
        row = self._row(path)
        if row is None:
            return {"url_path": path, "present": False, "registry": None, "config": None}
        return {
            "url_path": path,
            "present": True,
            "registry": copy.deepcopy(row["registry"]),
            "config": copy.deepcopy(row["config"]),
        }

    def save_dashboard(self, path, config, title):
        if self.fail_write:
            raise RuntimeError("synthetic dashboard write failure")
        if self._row(path) is not None:
            raise RuntimeError("path already exists")
        row = {
            "registry": {
                "id": path,
                "url_path": path,
                "title": title,
                "mode": "storage",
            },
            "config": copy.deepcopy(config),
        }
        self.rows.append(row)
        self.dashboard_writes += 1
        if self._row(path)["config"] != config:
            raise RuntimeError("synthetic readback mismatch")
        return {"readback_verified": True}

    def save_existing_dashboard(self, path, before, after):
        if self.fail_write:
            raise RuntimeError("synthetic dashboard write failure")
        row = self._row(path)
        if row is None or row["config"] != before:
            raise RuntimeError("synthetic compare-before-save mismatch")
        row["config"] = copy.deepcopy(after)
        self.dashboard_writes += 1
        if row["config"] != after:
            raise RuntimeError("synthetic readback mismatch")
        return {"readback_verified": True}

    def restore_dashboard(self, snapshot):
        path = snapshot["url_path"]
        row = self._row(path)
        if snapshot["present"]:
            if row is None:
                self.rows.append(
                    {
                        "registry": copy.deepcopy(snapshot["registry"]),
                        "config": copy.deepcopy(snapshot["config"]),
                    }
                )
            else:
                row["registry"] = copy.deepcopy(snapshot["registry"])
                row["config"] = copy.deepcopy(snapshot["config"])
        elif row is not None:
            self.rows.remove(row)
        restored = self._row(path)
        expected = snapshot["config"] if snapshot["present"] else None
        actual = restored["config"] if restored else None
        if actual != expected:
            raise RuntimeError("synthetic restore readback mismatch")
        return {"readback_verified": True, "restored_present": snapshot["present"]}


class ProductUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads((ROOT / "dist" / "RELEASE-MANIFEST.json").read_text())
        cls.archive = ROOT / "dist" / cls.release["archive"]
        cls.example = json.loads((ROOT / "tests" / "fixtures" / "site-manifest.example.json").read_text())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.site_path = self.root / "site.json"

    def tearDown(self):
        self.temporary.cleanup()

    def site(self, site_id="van-gogh-home", title="Van Gogh Home", irrigation=False):
        site = copy.deepcopy(self.example)
        site["site"] = {"id": site_id, "title": title}
        site["panel"]["id"] = f"{site_id}-wall10"
        if irrigation:
            site["features"]["irrigation"] = True
            site["bindings"]["garden_irrigation"] = {
                "adapter": {
                    "commissioning": "synthetic-verified",
                    "control": "button",
                    "id": "ha-button-v1",
                    "state_semantics": "measured",
                },
                "capability": "irrigation-zone",
                "entity_id": f"switch.{site_id.replace('-', '_')}_irrigation",
                "room": "studio",
                "services": ["switch.turn_off", "switch.turn_on"],
                "source": "neutral synthetic fixture",
                "verified": True,
            }
            site["content"]["irrigation"] = ["garden_irrigation"]
        return site

    def write_site(self, value):
        self.site_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        return hashlib.sha256(self.site_path.read_bytes()).hexdigest()

    def manager(self, ha):
        return ProductUpdateManager(InstallManager(self.config), ha, "1.2.1")

    def apply(self, manager):
        return manager.apply(self.archive, self.release, self.site_path, "2026.9.1")

    @staticmethod
    def tree_snapshot(root):
        if not root.exists():
            return None
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def current_row(self, site_id="van-gogh-home", include_external=True):
        site = self.site(site_id=site_id)
        config = {
            "title": "Locally Owned Title",
            "custom_top_level": {"owner": "keep-exact"},
            "views": [
                {
                    "title": "Home",
                    "path": "home",
                    "type": HOME_VIEW_TYPE,
                    "cards": [
                        {
                            "type": "custom:van-gogh2-home-header-card",
                            "schema_version": 1,
                            "site_name": "Local display name",
                        },
                        {
                            "type": "custom:van-gogh2-home-climate-card",
                            "schema_version": 1,
                            "entity": "climate.local",
                            "zones": ["climate.local_zone"],
                        },
                        {
                            "type": "custom:site-owned-card",
                            "binding": "sensor.preserved",
                        },
                        {
                            "type": "custom:van-gogh2-home-c-grid-card",
                            "schema_version": 1,
                            "cards": [
                                {
                                    "type": "custom:van-gogh2-home-module-card",
                                    "schema_version": 1,
                                    "kind": "lighting",
                                    "slot": "C3",
                                    "span": 2,
                                    "name": "Local lights",
                                    "entities": ["light.local"],
                                }
                            ],
                        },
                        {
                            "type": "custom:van-gogh2-quick-actions-card",
                            "schema_version": 1,
                            "actions": [{"entity": "switch.local"}],
                        },
                        {
                            "type": "custom:van-gogh2-navigation-card",
                            "schema_version": 1,
                            "routes": [{"path": "/van-gogh-home/home"}],
                        },
                    ],
                }
            ],
        }
        if include_external:
            config["views"].append(
                {
                    "title": "Owner Notes",
                    "path": "owner-notes",
                    "cards": [{"type": "markdown", "content": "exact local content"}],
                }
            )
        return {
            "registry": {
                "id": "existing-dashboard",
                "url_path": "van-gogh-home",
                "title": "Existing Van Gogh",
                "mode": "storage",
            },
            "config": config,
        }, site

    def legacy_row(self):
        def module(kind, name):
            return {
                "type": "custom:van-gogh2-home-module-card",
                "schema_version": 1,
                "kind": kind,
                "name": name,
                "entities": [f"sensor.{name.lower().replace(' ', '_')}"],
            }

        bays = [
            {
                "type": "custom:van-gogh-c-bay-card",
                "schema_version": 1,
                "bay": "C1-C2",
                "mode": "empty",
                "cards": [],
            },
            {
                "type": "custom:van-gogh-c-bay-card",
                "schema_version": 1,
                "bay": "C3-C4",
                "mode": "large",
                "cards": [module("lighting", "Local lights")],
            },
            {
                "type": "custom:van-gogh-c-bay-card",
                "schema_version": 1,
                "bay": "C5-C6",
                "mode": "split",
                "cards": [module("security", "Local security"), module("media", "Local media")],
            },
            {
                "type": "custom:van-gogh-c-bay-card",
                "schema_version": 1,
                "bay": "C7-C8",
                "mode": "split",
                "cards": [module("windows", "Local windows")],
            },
        ]
        return {
            "registry": {
                "id": "legacy-dashboard",
                "url_path": "van-gogh-home",
                "title": "Legacy Van Gogh",
                "mode": "storage",
            },
            "config": {
                "title": "Legacy",
                "owner_extension": {"preserve": [1, 2, 3]},
                "views": [
                    {
                        "title": "Home",
                        "path": "home",
                        "type": HOME_VIEW_TYPE,
                        "cards": [
                            {"type": "custom:van-gogh2-home-header-card", "schema_version": 1},
                            {
                                "type": "custom:van-gogh2-home-climate-card",
                                "schema_version": 1,
                                "entity": "climate.local",
                            },
                            {"type": "custom:site-owned-card", "binding": "sensor.preserved"},
                            *bays,
                            {"type": "custom:van-gogh2-quick-actions-card", "schema_version": 1},
                            {"type": "custom:van-gogh2-navigation-card", "schema_version": 1},
                        ],
                    },
                    {
                        "title": "External",
                        "path": "external",
                        "cards": [{"type": "markdown", "content": "do not touch"}],
                    },
                ],
            },
        }

    def test_package_is_deterministic_site_neutral_and_identity_separated(self):
        package = inspect_package(self.archive, self.release["sha256"])
        self.assertEqual(package.manifest["package_version"], "1.0.0-builder.23-p0.3")
        self.assertEqual(package.manifest["component_version"], "2.0.0-builder.23-p0.3")
        self.assertEqual(package.manifest["foundation"]["version"], "1.5.0")
        self.assertNotEqual(package.manifest["package_version"], package.manifest["component_version"])
        with tarfile.open(self.archive, "r:gz") as package_tar:
            payload = b"".join(
                stream.read()
                for member in package_tar.getmembers()
                if member.isfile() and (stream := package_tar.extractfile(member)) is not None
            )
        for forbidden in (b"site-alpha", b"site-beta", b"nabu", b"192.168."):
            self.assertNotIn(forbidden, payload.lower())

    def test_clean_install_and_identical_package_for_distinct_sites(self):
        hashes = []
        dashboards = []
        for site_id, title in (("site-alpha", "Site Alpha"), ("site-beta", "Site Beta")):
            with self.subTest(site_id=site_id):
                site_sha = self.write_site(self.site(site_id, title))
                ha = FakeHA()
                receipt = self.apply(self.manager(ha))
                self.assertEqual(receipt["state"], "clean_install")
                self.assertEqual(receipt["site_manifest_sha256"], site_sha)
                self.assertEqual(receipt["site_manifest_writes"], 0)
                self.assertEqual(ha.dashboard_writes, 1)
                row = ha._row(site_id)
                self.assertIsNotNone(row)
                self.assertIn(PRODUCT_MARKER, row["config"])
                self.assertIn(MANAGED_FOUNDATION, row["config"]["views"][0])
                hashes.append(hashlib.sha256(self.archive.read_bytes()).hexdigest())
                dashboards.append(row["config"])
                self.config = self.root / f"config-{site_id}"
                self.config.mkdir()
        self.assertEqual(len(set(hashes)), 1)
        self.assertNotEqual(dashboards[0], dashboards[1])

    def test_clean_install_refuses_missing_canonical_site_manifest_without_writes(self):
        ha = FakeHA()
        manager = self.manager(ha)

        with self.assertRaisesRegex(
            ProductUpdateError,
            "Local canonical site manifest is missing",
        ):
            self.apply(manager)

        self.assertEqual(ha.dashboard_writes, 0)
        self.assertFalse(manager.installer.target.exists())
        self.assertFalse(manager.installer.current_receipt.exists())

    def test_clean_install_preserves_canonical_site_manifest_bytes_exactly(self):
        site_sha = self.write_site(self.site("site-byte-exact", "Byte Exact Site"))
        before = self.site_path.read_bytes()
        ha = FakeHA()
        manager = self.manager(ha)

        receipt = self.apply(manager)

        self.assertEqual(receipt["state"], "clean_install")
        self.assertEqual(receipt["site_manifest_sha256"], site_sha)
        self.assertEqual(receipt["site_manifest_writes"], 0)
        self.assertEqual(self.site_path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(self.site_path.read_bytes()).hexdigest(), site_sha)

    def test_optional_feature_present_for_all_but_locally_enabled(self):
        markers = []
        for enabled in (False, True):
            self.config = self.root / f"config-{enabled}"
            self.config.mkdir()
            self.write_site(self.site(f"feature-{str(enabled).lower()}", "Feature Site", enabled))
            ha = FakeHA()
            self.apply(self.manager(ha))
            config = ha.rows[0]["config"]
            markers.append(config[PRODUCT_MARKER]["enabled_features"])
        self.assertNotIn("irrigation", markers[0])
        self.assertIn("irrigation", markers[1])
        package = inspect_package(self.archive, self.release["sha256"])
        self.assertIn("irrigation", package.catalogue["features"])

    def test_product_package_matches_frozen_builder23_identity(self):
        self.assertEqual(
            hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "c41d81c1615cebadabea8f1eff419c45bc8d74856d1698453c728d2cd46e39b9",
        )
        self.assertEqual(self.release["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_production_update_logic_has_no_customer_or_site_identifiers(self):
        sources = [
            APP / "app.py",
            APP / "product_update.py",
        ]
        forbidden = (
            "vg-site-001-erich-home",
            "erich",
            "lucas",
            "neil",
            "site-alpha",
            "site-beta",
            "192.168.",
            "nabu",
        )
        for source in sources:
            text = source.read_text(encoding="utf-8").lower()
            for marker in forbidden:
                with self.subTest(source=source.name, marker=marker):
                    self.assertNotIn(marker, text)

    def test_deployed_text_assets_contain_no_embedded_credentials_or_private_networks(self):
        self.write_site(self.site())
        manager = self.manager(FakeHA())
        self.apply(manager)
        deployed_roots = (
            manager.installer.target,
            self.config / FOUNDATION_TARGETS["owner_maintenance"],
            self.config / FOUNDATION_TARGETS["assets"],
        )
        forbidden = (
            re.compile(r"-----begin (?:rsa |ec |openssh )?private key-----", re.IGNORECASE),
            re.compile(r"\bbearer\s+[a-z0-9._~-]{20,}", re.IGNORECASE),
            re.compile(r"\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b", re.IGNORECASE),
            re.compile(r"\b10(?:\.\d{1,3}){3}\b"),
            re.compile(r"\b192\.168(?:\.\d{1,3}){2}\b"),
            re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b"),
        )
        text_suffixes = {".css", ".html", ".js", ".json", ".md", ".py", ".svg", ".txt", ".yaml", ".yml"}
        deployed = sorted(
            path
            for root in deployed_roots
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in text_suffixes
        )
        self.assertTrue(deployed)
        for path in deployed:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                with self.subTest(path=path.relative_to(self.config), pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_current_upgrade_preserves_local_manifest_and_unaffected_content(self):
        row, site = self.current_row()
        site_bytes = (json.dumps(site, indent=2, sort_keys=True) + "\n").encode()
        self.site_path.write_bytes(site_bytes)
        before_home = copy.deepcopy(row["config"]["views"][0])
        before_external = copy.deepcopy(row["config"]["views"][1])
        before_top = copy.deepcopy(row["config"]["custom_top_level"])
        ha = FakeHA([row])
        receipt = self.apply(self.manager(ha))
        self.assertEqual(receipt["state"], "current_upgrade")
        after = ha._row("van-gogh-home")["config"]
        self.assertEqual(after["views"][0], before_home)
        self.assertEqual(after["views"][0]["cards"][1]["entity"], "climate.local")
        self.assertEqual(after["views"][0]["cards"][2]["type"], "custom:site-owned-card")
        self.assertEqual(after["views"][0]["cards"][3]["type"], GRID_TYPE)
        self.assertEqual(next(view for view in after["views"] if view.get("path") == "owner-notes"), before_external)
        self.assertEqual(after["custom_top_level"], before_top)
        self.assertEqual(self.site_path.read_bytes(), site_bytes)

    def test_legacy_upgrade_preserves_unaffected_content(self):
        self.write_site(self.site())
        row = self.legacy_row()
        expected_home = discover_dashboard([copy.deepcopy(row)]).after["views"][0]
        external = copy.deepcopy(row["config"]["views"][1])
        owner_extension = copy.deepcopy(row["config"]["owner_extension"])
        ha = FakeHA([row])
        receipt = self.apply(self.manager(ha))
        self.assertEqual(receipt["state"], "legacy_upgrade")
        after = ha._row("van-gogh-home")["config"]
        self.assertEqual(after["views"][0], expected_home)
        preserved_home = json.dumps(after["views"][0], sort_keys=True)
        self.assertIn('"climate.local"', preserved_home)
        self.assertIn('"custom:site-owned-card"', preserved_home)
        self.assertIn(f'"{GRID_TYPE}"', preserved_home)
        self.assertEqual(next(view for view in after["views"] if view.get("path") == "external"), external)
        self.assertEqual(after["owner_extension"], owner_extension)

    def test_second_run_is_zero_write_noop(self):
        self.write_site(self.site())
        ha = FakeHA()
        manager = self.manager(ha)
        first = self.apply(manager)
        writes = ha.dashboard_writes
        component_sha = hashlib.sha256((manager.installer.target / "manifest.json").read_bytes()).hexdigest()
        second = self.apply(manager)
        self.assertEqual(first["operation"], "product_update")
        self.assertEqual(second["operation"], "noop")
        self.assertEqual(second["file_writes"], 0)
        self.assertEqual(second["dashboard_writes"], 0)
        self.assertEqual(ha.dashboard_writes, writes)
        self.assertEqual(
            hashlib.sha256((manager.installer.target / "manifest.json").read_bytes()).hexdigest(),
            component_sha,
        )

    def test_managed_inventory_repairs_missing_modified_and_unexpected_files(self):
        self.write_site(self.site())
        ha = FakeHA()
        manager = self.manager(ha)
        first = self.apply(manager)
        self.assertEqual(first["operation"], "product_update")

        asset_root = self.config / FOUNDATION_TARGETS["assets"]
        asset = asset_root / "almond-blossom.svg"
        expected_asset = asset.read_bytes()

        asset.unlink()
        missing = self.apply(manager)
        self.assertEqual(missing["operation"], "product_update")
        self.assertEqual(asset.read_bytes(), expected_asset)

        asset.write_bytes(b"modified managed asset")
        modified = self.apply(manager)
        self.assertEqual(modified["operation"], "product_update")
        self.assertEqual(asset.read_bytes(), expected_asset)

        unexpected = asset_root / "unexpected-local-copy.js"
        unexpected.write_text("not in authoritative inventory", encoding="utf-8")
        extra = self.apply(manager)
        self.assertEqual(extra["operation"], "product_update")
        self.assertFalse(unexpected.exists())

        component = manager.installer.target / "__init__.py"
        expected_component = component.read_bytes()
        component.write_bytes(b"modified component")
        component_drift = self.apply(manager)
        self.assertEqual(component_drift["operation"], "product_update")
        self.assertEqual(component.read_bytes(), expected_component)

        settled = self.apply(manager)
        self.assertEqual(settled["operation"], "noop")
        self.assertEqual(settled["file_writes"], 0)

    def test_unsupported_state_and_versions_are_zero_write(self):
        self.write_site(self.site())
        unsupported = {
            "registry": {
                "id": "van-gogh-home",
                "url_path": "van-gogh-home",
                "title": "Odd",
                "mode": "storage",
            },
            "config": {
                "views": [
                    {
                        "title": "Home",
                        "path": "home",
                        "type": HOME_VIEW_TYPE,
                        MANAGED_FOUNDATION: {"schema_version": 999},
                        "cards": [],
                    }
                ]
            },
        }
        ha = FakeHA([unsupported])
        manager = self.manager(ha)
        before = copy.deepcopy(ha.rows)
        with self.assertRaises(ProductUpdateError):
            self.apply(manager)
        self.assertEqual(ha.rows, before)
        self.assertFalse(manager.installer.target.exists())
        with self.assertRaises(ProductUpdateError):
            manager.apply(self.archive, self.release, self.site_path, "2028.1.0")
        self.assertEqual(ha.rows, before)
        self.assertFalse(manager.installer.target.exists())

    def test_invalid_or_secret_site_manifest_is_zero_write(self):
        for site in ({"schema_version": 999}, {**self.site(), "api_token": "forbidden"}):
            with self.subTest(site=site.get("schema_version")):
                self.write_site(site)
                ha = FakeHA()
                manager = self.manager(ha)
                with self.assertRaises(ProductUpdateError):
                    self.apply(manager)
                self.assertEqual(ha.dashboard_writes, 0)
                self.assertFalse(manager.installer.target.exists())

    def test_write_failure_automatically_rolls_back_all_surfaces(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        before = copy.deepcopy(row)
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        (component / "old.txt").write_text("component-prestate")
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        ha.fail_write = True
        manager = self.manager(ha)
        with self.assertRaises(RuntimeError):
            self.apply(manager)
        self.assertEqual(ha.rows[0], before)
        self.assertEqual((component / "old.txt").read_text(), "component-prestate")
        self.assertEqual(manager.installer.installed_version(), "1.9.9")
        for key, relative in FOUNDATION_TARGETS.items():
            self.assertEqual((self.config / relative / "old.txt").read_text(), f"{key}-prestate")
        self.assertFalse(manager.installer.current_receipt.exists())

    def test_explicit_rollback_restores_exact_prestate(self):
        site_sha = self.write_site(self.site())
        row, _ = self.current_row()
        before = copy.deepcopy(row)
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        (component / "old.txt").write_text("component-prestate")
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        applied = self.apply(manager)
        self.assertEqual(applied["site_manifest_sha256"], site_sha)
        rolled_back = manager.rollback()
        self.assertTrue(rolled_back["readback_verified"])
        self.assertEqual(ha.rows[0], before)
        self.assertEqual((component / "old.txt").read_text(), "component-prestate")
        self.assertEqual(manager.installer.installed_version(), "1.9.9")
        for key, relative in FOUNDATION_TARGETS.items():
            self.assertEqual((self.config / relative / "old.txt").read_text(), f"{key}-prestate")

    def test_rollback_missing_first_foundation_backup_is_zero_write(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        receipt = json.loads(manager.installer.current_receipt.read_text())
        first_key = receipt["foundation_files"][0]["key"]
        backup_root = manager.installer.backups / receipt["backup_id"] / "product-files"
        shutil.rmtree(backup_root / first_key)
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }

        with self.assertRaisesRegex(ProductUpdateError, f"backup is missing {first_key}"):
            manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_rollback_missing_second_foundation_backup_is_zero_write(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        receipt = json.loads(manager.installer.current_receipt.read_text())
        second_key = receipt["foundation_files"][1]["key"]
        backup_root = manager.installer.backups / receipt["backup_id"] / "product-files"
        shutil.rmtree(backup_root / second_key)
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }

        with self.assertRaisesRegex(ProductUpdateError, f"backup is missing {second_key}"):
            manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_rollback_foundation_stage_copy_failure_is_zero_write(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }

        with patch("product_update.shutil.copytree", side_effect=OSError("synthetic stage copy failure")):
            with self.assertRaisesRegex(OSError, "synthetic stage copy failure"):
                manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_rollback_foundation_swap_failure_recovers_current_state(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }
        real_replace = os.replace
        replace_calls = 0

        def fail_second_replace(source, target):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("synthetic atomic swap failure")
            return real_replace(source, target)

        with patch("product_update.os.replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "synthetic atomic swap failure"):
                manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())
        rollback_debris = [
            path
            for path in self.config.rglob("*")
            if ".rollback." in path.name or ".displaced." in path.name
        ]
        self.assertEqual(rollback_debris, [])

    def test_rollback_foundation_initial_swap_failure_preserves_current_state(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }

        with patch("product_update.os.replace", side_effect=OSError("synthetic initial swap failure")):
            with self.assertRaisesRegex(OSError, "synthetic initial swap failure"):
                manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_rollback_missing_component_backup_is_zero_write(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        receipt = json.loads(manager.installer.current_receipt.read_text())
        shutil.rmtree(manager.installer.backups / receipt["backup_id"] / "van_gogh2")
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }

        with self.assertRaisesRegex(InstallError, "Rollback integration backup is missing"):
            manager.rollback()

        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_rollback_incomplete_receipt_fails_before_staging_or_mutation(self):
        self.write_site(self.site())
        row, _ = self.current_row()
        component = self.config / "custom_components" / "van_gogh2"
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(json.dumps({"domain": "van_gogh2", "version": "1.9.9"}))
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"{key}-prestate")
        ha = FakeHA([row])
        manager = self.manager(ha)
        self.apply(manager)
        receipt = json.loads(manager.installer.current_receipt.read_text())
        receipt.pop("dashboard_path")
        manager.installer.current_receipt.write_text(json.dumps(receipt))
        dashboard_before = copy.deepcopy(ha.rows)
        component_before = self.tree_snapshot(component)
        foundation_before = {
            key: self.tree_snapshot(self.config / relative)
            for key, relative in FOUNDATION_TARGETS.items()
        }
        real_copytree = shutil.copytree

        with patch("product_update.shutil.copytree", wraps=real_copytree) as copytree:
            with self.assertRaisesRegex(ProductUpdateError, "receipt has no dashboard path"):
                manager.rollback()

        copytree.assert_not_called()
        self.assertEqual(ha.rows, dashboard_before)
        self.assertEqual(self.tree_snapshot(component), component_before)
        self.assertEqual(
            {
                key: self.tree_snapshot(self.config / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            foundation_before,
        )
        self.assertTrue(manager.installer.current_receipt.is_file())

    def test_catalogue_and_canonical_site_schema_fail_closed(self):
        package = inspect_package(self.archive, self.release["sha256"])
        valid = self.site()
        self.assertEqual(validate_site_manifest(valid, package), valid)
        invalid = copy.deepcopy(valid)
        invalid["features"]["irrigation"] = True
        invalid["content"]["irrigation"] = []
        with self.assertRaises(ProductUpdateError):
            validate_site_manifest(invalid, package)

    def test_staging8_prestate_installs_to_supplied_canary_path_without_protected_drift(self):
        fixtures = ROOT / "tests" / "fixtures"
        captured = json.loads(
            (fixtures / "qa_b23_live_containment_001_prestate.json").read_text()
        )
        configs = {
            row["url_path"]: row["config"]
            for row in captured["dashboard_configs"]
        }
        rows = [
            {
                "registry": copy.deepcopy(registry),
                "config": copy.deepcopy(configs[registry["url_path"]]),
            }
            for registry in captured["dashboard_registry"]
        ]
        protected_paths = ("map", "van-gogh-builder", "van-gogh-home")
        target_path = "builder23-canary-wall10"
        before = copy.deepcopy(rows)
        site_bytes = (
            fixtures / "qa_b23_live_containment_001_site_manifest.json"
        ).read_bytes()
        self.site_path.parent.mkdir(parents=True, exist_ok=True)
        self.site_path.write_bytes(site_bytes)
        self.assertEqual(
            hashlib.sha256(site_bytes).hexdigest(),
            "1476f808051f3389c6beb2b8f03c62b802ce7d0d03b9160692ae92ff89fe640d",
        )

        ha = FakeHA(rows)
        manager = self.manager(ha)
        first = self.apply(manager)

        self.assertEqual(first["state"], "clean_install")
        self.assertIsNotNone(ha._row(target_path))
        self.assertEqual(
            [row for row in ha.rows if row["registry"]["url_path"] in protected_paths],
            before,
        )

        current = copy.deepcopy(ha.rows)
        noop = self.apply(manager)
        self.assertEqual(noop["operation"], "noop")
        self.assertEqual(ha.rows, current)

        manager.rollback()
        self.assertIsNone(ha._row(target_path))
        self.assertEqual(ha.rows, before)

        reinstall = self.apply(manager)
        self.assertEqual(reinstall["state"], "clean_install")
        self.assertIsNotNone(ha._row(target_path))
        self.assertEqual(
            [row for row in ha.rows if row["registry"]["url_path"] in protected_paths],
            before,
        )


if __name__ == "__main__":
    unittest.main()
