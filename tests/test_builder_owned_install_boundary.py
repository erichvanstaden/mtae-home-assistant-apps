from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


APP_ROOT = Path(__file__).resolve().parents[1] / "van_gogh_2/rootfs/app"
sys.path.insert(0, str(APP_ROOT))

import product_update  # noqa: E402


class BuilderOwnedInstallBoundaryTests(unittest.TestCase):
    def test_builder_owned_install_never_implicitly_requests_dashboard_targets(self) -> None:
        manager = product_update.ProductUpdateManager.__new__(
            product_update.ProductUpdateManager
        )
        manager.apply = Mock(return_value={"dashboard_writes": 0})
        archive = Path("product.tar.gz")
        site = Path("site.json")
        release = {"package_version": "example"}

        result = manager.apply_builder_owned(
            archive,
            release,
            site,
            "2026.9.1",
            defer_commit=True,
        )

        self.assertEqual(result, {"dashboard_writes": 0})
        manager.apply.assert_called_once_with(
            archive,
            release,
            site,
            "2026.9.1",
            defer_commit=True,
            builder_owned=True,
        )


if __name__ == "__main__":
    unittest.main()