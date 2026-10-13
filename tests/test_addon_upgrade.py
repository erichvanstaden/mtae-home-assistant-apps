from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "van_gogh_2"
APP_ROOT = ADDON / "rootfs" / "app"
TASK_ROOT = ROOT.parent
DONOR_ADDONS = (
    ("1.1.17", TASK_ROOT / ".scratch" / "installer-1.1.17-source" / "van_gogh_2"),
    ("1.2.3", TASK_ROOT / ".scratch" / "installer-1.2.3-source" / "van_gogh_2"),
)
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("VAN_GOGH_DATA_ROOT", tempfile.mkdtemp(prefix="vg-upgrade-data-"))
os.environ.setdefault("VAN_GOGH_CONFIG_ROOT", tempfile.mkdtemp(prefix="vg-upgrade-config-"))

import app  # noqa: E402  # pyright: ignore[reportMissingImports]


def yaml_scalar(text: str, key: str) -> str:
    prefix = f"{key}:"
    for raw in text.splitlines():
        if raw.startswith(prefix):
            return raw.split(":", 1)[1].strip().strip('"').strip("'")
    raise AssertionError(f"{key} is missing")


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(f"d\0{relative}\0".encode())
            continue
        if not path.is_file():
            raise AssertionError(f"unsupported tree member: {relative}")
        digest.update(f"f\0{relative}\0{path.stat().st_mode & 0o777:o}\0".encode())
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def replace_addon(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


class AddonUpgradeLineageTests(unittest.TestCase):
    def setUp(self):
        for version, source in DONOR_ADDONS:
            self.assertTrue(source.is_dir(), f"frozen Installer {version} donor is missing: {source}")

    def test_117_and_123_keep_addon_identity_mounts_and_persisted_settings(self):
        final_config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(yaml_scalar(final_config, "version"), "1.2.5")
        self.assertEqual(yaml_scalar(final_config, "slug"), "van_gogh_2")
        self.assertIn('homeassistant_config:rw', final_config)

        preserved = {
            "credential": "[REDACTED]",
            "site_id": "site-preserved",
            "site_name": "Preserved site",
        }
        encoded = (json.dumps(preserved, sort_keys=True) + "\n").encode("utf-8")

        for version, donor in DONOR_ADDONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                baseline = (donor / "config.yaml").read_text(encoding="utf-8")
                self.assertEqual(yaml_scalar(baseline, "version"), version)
                self.assertEqual(yaml_scalar(baseline, "slug"), "van_gogh_2")
                self.assertIn('homeassistant_config:rw', baseline)

                data_root = Path(temporary) / "data"
                data_root.mkdir()
                settings = data_root / "settings.json"
                settings.write_bytes(encoded)
                before = settings.read_bytes()
                with mock.patch.object(app, "DATA_ROOT", data_root), mock.patch.object(app, "SETTINGS", settings):
                    self.assertEqual(app._settings(), preserved)
                self.assertEqual(settings.read_bytes(), before)

    def test_117_and_123_upgrade_rollback_and_identical_reinstall_are_state_safe(self):
        settings_bytes = b'{"credential":"[REDACTED]","site_id":"site-preserved"}\n'
        options_bytes = b'{"release_endpoint":"https://release.example.invalid"}\n'
        dashboard_bytes = b'{"title":"Protected pre-existing dashboard"}\n'

        for version, donor in DONOR_ADDONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                baseline = root / "baseline" / "van_gogh_2"
                shutil.copytree(donor, baseline)
                installed = root / "supervisor" / "addons" / "van_gogh_2"
                data_root = root / "supervisor" / "data"
                data_file = data_root / "settings.json"
                options_file = data_root / "options.json"
                dashboard = root / "homeassistant" / ".storage" / "lovelace.protected"
                data_file.parent.mkdir(parents=True)
                dashboard.parent.mkdir(parents=True)
                data_file.write_bytes(settings_bytes)
                options_file.write_bytes(options_bytes)
                dashboard.write_bytes(dashboard_bytes)

                replace_addon(baseline, installed)
                baseline_digest = tree_sha256(installed)

                replace_addon(ADDON, installed)
                candidate_digest = tree_sha256(installed)
                self.assertEqual(yaml_scalar((installed / "config.yaml").read_text(), "version"), "1.2.5")
                self.assertEqual(data_file.read_bytes(), settings_bytes)
                self.assertEqual(options_file.read_bytes(), options_bytes)
                self.assertEqual(dashboard.read_bytes(), dashboard_bytes)

                replace_addon(baseline, installed)
                self.assertEqual(tree_sha256(installed), baseline_digest)
                self.assertEqual(data_file.read_bytes(), settings_bytes)
                self.assertEqual(options_file.read_bytes(), options_bytes)
                self.assertEqual(dashboard.read_bytes(), dashboard_bytes)

                replace_addon(ADDON, installed)
                self.assertEqual(tree_sha256(installed), candidate_digest)
                self.assertEqual(data_file.read_bytes(), settings_bytes)
                self.assertEqual(options_file.read_bytes(), options_bytes)
                self.assertEqual(dashboard.read_bytes(), dashboard_bytes)

    def test_frozen_donor_identities_match_task_control(self):
        control = json.loads((TASK_ROOT / "DONOR-BASELINES.json").read_text(encoding="utf-8"))
        donors = control["immutable_donors"]
        self.assertEqual(
            donors["installer_1_1_17_rollback"]["commit"],
            "ba78736542c44161637a84fa4913e9fc058794d2",
        )
        self.assertEqual(
            donors["installer_1_2_3"]["commit"],
            "50179fd8738059cd09ae47a09e156342e796974e",
        )


if __name__ == "__main__":
    unittest.main()
