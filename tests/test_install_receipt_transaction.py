from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

import installer  # noqa: E402
from installer import InstallManager  # noqa: E402


def tree_snapshot(root: Path):
    if not root.exists():
        return None
    rows = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows[relative] = {
                "type": "symlink",
                "target": str(path.readlink()),
            }
        elif path.is_file():
            rows[relative] = {
                "type": "file",
                "mode": path.stat().st_mode & 0o777,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        elif path.is_dir():
            rows[relative] = {
                "type": "directory",
                "mode": path.stat().st_mode & 0o777,
            }
    return rows


class InstallReceiptTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.manager = InstallManager(self.config)
        self.source = self.root / "source-component"
        self.source.mkdir()
        (self.source / "manifest.json").write_text(
            json.dumps(
                {
                    "domain": "van_gogh2",
                    "version": "2.0.0-builder.23-p0.3",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.source / "candidate-marker.txt").write_text(
            "candidate\n",
            encoding="utf-8",
        )
        self.release = {
            "archive": "synthetic-product.tar.gz",
            "sha256": "a" * 64,
            "version": "2.0.0-builder.23-p0.3",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def seed_existing_install(self) -> None:
        self.manager.target.mkdir(parents=True)
        (self.manager.target / "manifest.json").write_text(
            json.dumps(
                {
                    "domain": "van_gogh2",
                    "version": "1.1.17-prestate",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.manager.target / "prestate-marker.txt").write_text(
            "prestate\n",
            encoding="utf-8",
        )
        self.manager.current_receipt.parent.mkdir(parents=True, exist_ok=True)
        self.manager.current_receipt.write_bytes(
            b'{"backup_id":"pre-existing","state":"prestate"}\n'
        )
        self.manager.operations.mkdir(parents=True, exist_ok=True)
        (self.manager.operations / "pre-existing.json").write_bytes(
            b'{"state":"prestate"}\n'
        )

    def inject_receipt_failure(self, boundary: str):
        real_write = installer._write_json_atomic

        def injected(path, payload):
            path = Path(path)
            if boundary == "current" and path == self.manager.current_receipt:
                raise OSError("synthetic current receipt finalisation failure")
            if boundary == "operation" and path.parent == self.manager.operations:
                raise OSError("synthetic operation receipt finalisation failure")
            return real_write(path, payload)

        return injected

    def test_current_receipt_failure_restores_exact_absent_prestate(self) -> None:
        before = tree_snapshot(self.config)

        with patch(
            "installer._write_json_atomic",
            side_effect=self.inject_receipt_failure("current"),
        ):
            with self.assertRaisesRegex(
                OSError,
                "synthetic current receipt finalisation failure",
            ):
                self.manager._install_component(
                    self.source,
                    self.release,
                    self.release["sha256"],
                )

        self.assertEqual(tree_snapshot(self.config), before)
        self.assertIsNone(self.manager.installed_version())
        self.assertFalse(self.manager.current_receipt.exists())

    def test_operation_receipt_failure_restores_exact_existing_prestate(self) -> None:
        self.seed_existing_install()
        before = tree_snapshot(self.config)

        with patch(
            "installer._write_json_atomic",
            side_effect=self.inject_receipt_failure("operation"),
        ):
            with self.assertRaisesRegex(
                OSError,
                "synthetic operation receipt finalisation failure",
            ):
                self.manager._install_component(
                    self.source,
                    self.release,
                    self.release["sha256"],
                )

        self.assertEqual(tree_snapshot(self.config), before)
        self.assertEqual(self.manager.installed_version(), "1.1.17-prestate")
        self.assertEqual(
            self.manager.current_receipt.read_bytes(),
            b'{"backup_id":"pre-existing","state":"prestate"}\n',
        )

    def test_successful_install_commits_component_and_receipts(self) -> None:
        before = tree_snapshot(self.config)

        receipt = self.manager._install_component(
            self.source,
            self.release,
            self.release["sha256"],
        )

        self.assertNotEqual(tree_snapshot(self.config), before)
        self.assertEqual(self.manager.installed_version(), self.release["version"])
        self.assertTrue(self.manager.current_receipt.is_file())
        operation_receipts = sorted(self.manager.operations.glob("*-install.json"))
        self.assertEqual(len(operation_receipts), 1)
        self.assertEqual(
            json.loads(self.manager.current_receipt.read_text(encoding="utf-8")),
            receipt,
        )
        self.assertEqual(
            json.loads(operation_receipts[0].read_text(encoding="utf-8")),
            receipt,
        )
        debris = [
            path
            for path in self.config.rglob("*")
            if any(token in path.name for token in (".stage.", ".displaced.", ".tmp"))
        ]
        self.assertEqual(debris, [])


if __name__ == "__main__":
    unittest.main()
