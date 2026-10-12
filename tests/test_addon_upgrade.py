from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "van_gogh_2"
APP_ROOT = ADDON / "rootfs" / "app"
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


def tagged_config(tag: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{tag}:van_gogh_2/config.yaml"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


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


def export_tagged_addon(tag: str, destination: Path) -> Path:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", tag, "van_gogh_2"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("van_gogh_2",):
                raise AssertionError(f"unsafe tagged path: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                source = bundle.extractfile(member)
                if source is None:
                    raise AssertionError(f"missing tagged bytes: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
                target.chmod(member.mode & 0o777)
            else:
                raise AssertionError(f"unsupported tagged member: {member.name}")
    return destination / "van_gogh_2"


def replace_addon(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


class AddonUpgradeLineageTests(unittest.TestCase):
    def test_114_and_117_keep_addon_identity_mounts_and_persisted_settings(self):
        final_config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(yaml_scalar(final_config, "version"), "1.2.3")
        self.assertEqual(yaml_scalar(final_config, "slug"), "van_gogh_2")
        self.assertIn('homeassistant_config:rw', final_config)

        preserved = {
            "credential": "[REDACTED]",
            "site_id": "site-preserved",
            "site_name": "Preserved site",
        }
        encoded = (json.dumps(preserved, sort_keys=True) + "\n").encode("utf-8")

        for tag, version in (("v1.1.14", "1.1.14"), ("v1.1.17", "1.1.17")):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as temporary:
                baseline = tagged_config(tag)
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

    def test_114_and_117_upgrade_rollback_and_identical_reinstall_are_state_safe(self):
        settings_bytes = b'{"credential":"[REDACTED]","site_id":"site-preserved"}\n'
        options_bytes = b'{"release_endpoint":"https://release.example.invalid"}\n'
        dashboard_bytes = b'{"title":"Protected pre-existing dashboard"}\n'

        for tag in ("v1.1.14", "v1.1.17"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                baseline = export_tagged_addon(tag, root / "baseline")
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
                self.assertEqual(yaml_scalar((installed / "config.yaml").read_text(), "version"), "1.2.3")
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

    def test_candidate_is_descended_from_public_117_release(self):
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "v1.1.17", "HEAD"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
