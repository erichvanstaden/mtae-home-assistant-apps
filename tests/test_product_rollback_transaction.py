import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from installer import InstallManager  # noqa: E402
from product_update import FOUNDATION_TARGETS, ProductUpdateManager  # noqa: E402


class FakeHA:
    def __init__(self) -> None:
        self.state = {
            "url_path": "synthetic",
            "present": True,
            "registry": {"id": "synthetic", "url_path": "synthetic", "title": "Current"},
            "config": {"state": "current"},
        }

    def dashboard_snapshot(self, path: str) -> dict[str, Any]:
        if path != "synthetic":
            raise RuntimeError("unexpected dashboard path")
        return copy.deepcopy(self.state)

    def restore_dashboard(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.state = copy.deepcopy(snapshot)
        return {"readback_verified": True, "restored_present": snapshot.get("present")}


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root: Path) -> dict[str, dict[str, Any]] | None:
    if not root.exists():
        return None
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows[relative] = {"type": "symlink", "target": str(path.readlink())}
        elif path.is_file():
            rows[relative] = {
                "type": "file",
                "bytes": path.stat().st_size,
                "sha256": file_sha(path),
            }
        elif path.is_dir():
            rows[relative] = {"type": "directory"}
    return rows


class ProductRollbackTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Path(self.temporary.name) / "config"
        self.manager = ProductUpdateManager(InstallManager(self.config), FakeHA(), "1.2.1")
        install = self.manager.installer
        self.backup_id = "synthetic-backup"

        install.target.mkdir(parents=True)
        (install.target / "manifest.json").write_text(
            json.dumps({"domain": "van_gogh2", "version": "2.0.0-builder.23-p0.3"}),
            encoding="utf-8",
        )
        (install.target / "current-marker.txt").write_text("component-current", encoding="utf-8")

        self.component_backup = install.backups / self.backup_id / "van_gogh2"
        self.component_backup.mkdir(parents=True)
        (self.component_backup / "manifest.json").write_text(
            json.dumps({"domain": "van_gogh2", "version": "1.1.17-prestate"}),
            encoding="utf-8",
        )
        (self.component_backup / "old-marker.txt").write_text("component-old", encoding="utf-8")
        (install.backups / self.backup_id / "receipt.json").write_text(
            json.dumps(
                {
                    "backup_id": self.backup_id,
                    "previous_present": True,
                    "previous_version": "1.1.17-prestate",
                }
            ),
            encoding="utf-8",
        )

        foundation_rows = []
        for key, relative in FOUNDATION_TARGETS.items():
            target = self.config / relative
            target.mkdir(parents=True)
            (target / "current-marker.txt").write_text(f"{key}-current", encoding="utf-8")
            backup = install.backups / self.backup_id / "product-files" / key
            backup.mkdir(parents=True)
            (backup / "old-marker.txt").write_text(f"{key}-old", encoding="utf-8")
            foundation_rows.append({"key": key, "previous_present": True})

        receipt = {
            "backup_id": self.backup_id,
            "dashboard_prestate": {
                "url_path": "synthetic",
                "present": True,
                "registry": {"id": "synthetic", "url_path": "synthetic", "title": "Old"},
                "config": {"state": "old"},
            },
            "dashboard_path": "synthetic",
            "foundation_files": foundation_rows,
        }
        install.current_receipt.parent.mkdir(parents=True, exist_ok=True)
        install.current_receipt.write_text(json.dumps(receipt), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot_all(self) -> dict[str, Any]:
        install = self.manager.installer
        return {
            "dashboard": copy.deepcopy(self.manager.ha.state),
            "component": tree_snapshot(install.target),
            "foundation": {
                key: tree_snapshot(install.config_root / relative)
                for key, relative in FOUNDATION_TARGETS.items()
            },
            "receipt": file_sha(install.current_receipt) if install.current_receipt.is_file() else None,
            "operations": tree_snapshot(install.operations),
            "debris": sorted(
                path.relative_to(install.config_root).as_posix()
                for path in install.config_root.rglob("*")
                if ".rollback." in path.name
                or ".displaced." in path.name
                or ".before-rollback." in path.name
            ),
        }

    def test_component_stage_failure_preserves_exact_current_state(self) -> None:
        before = self.snapshot_all()
        real_copytree = shutil.copytree

        def fail_component_stage(source, destination, *args, **kwargs):
            if Path(source) == self.component_backup:
                raise OSError("synthetic component rollback stage failure")
            return real_copytree(source, destination, *args, **kwargs)

        with patch("installer.shutil.copytree", side_effect=fail_component_stage):
            with self.assertRaisesRegex(OSError, "synthetic component rollback stage failure"):
                self.manager.rollback()

        self.assertEqual(self.snapshot_all(), before)

    def test_component_swap_failure_preserves_exact_current_state(self) -> None:
        before = self.snapshot_all()
        real_replace = os.replace

        def fail_component_swap(source, destination, *args, **kwargs):
            source_path = Path(source)
            destination_path = Path(destination)
            if ".van_gogh2.rollback." in source_path.name and destination_path == self.manager.installer.target:
                raise OSError("synthetic component rollback swap failure")
            return real_replace(source, destination, *args, **kwargs)

        with patch("installer.os.replace", side_effect=fail_component_swap):
            with self.assertRaisesRegex(OSError, "synthetic component rollback swap failure"):
                self.manager.rollback()

        self.assertEqual(self.snapshot_all(), before)

    def test_operation_receipt_write_failure_preserves_exact_current_state(self) -> None:
        before = self.snapshot_all()
        operations = self.manager.installer.operations
        real_replace = os.replace

        def fail_operation_receipt_replace(source: Path, destination: Path):
            destination = Path(destination)
            if destination.parent == operations and destination.name.endswith("-rollback.json"):
                raise OSError("synthetic rollback operation receipt write failure")
            return real_replace(source, destination)

        with patch("installer.os.replace", side_effect=fail_operation_receipt_replace):
            with self.assertRaisesRegex(
                OSError,
                "synthetic rollback operation receipt write failure",
            ):
                self.manager.rollback()

        self.assertEqual(self.snapshot_all(), before)

    def test_current_receipt_unlink_failure_preserves_exact_current_state(self) -> None:
        now = "20260102T030405Z"
        operations = self.manager.installer.operations
        operations.mkdir(parents=True)
        (operations / f"{now}-rollback.json").write_bytes(b'{"existing":true}\n')
        before = self.snapshot_all()
        current_receipt = self.manager.installer.current_receipt
        real_unlink = Path.unlink

        def fail_current_receipt_unlink(path: Path, *args, **kwargs):
            if path == current_receipt:
                raise OSError("synthetic rollback current receipt unlink failure")
            return real_unlink(path, *args, **kwargs)

        with patch("installer.time.strftime", return_value=now), patch(
            "pathlib.Path.unlink",
            new=fail_current_receipt_unlink,
        ):
            with self.assertRaisesRegex(
                OSError,
                "synthetic rollback current receipt unlink failure",
            ):
                self.manager.rollback()

        self.assertEqual(self.snapshot_all(), before)


if __name__ == "__main__":
    unittest.main()
