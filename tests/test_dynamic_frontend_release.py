from __future__ import annotations

from pathlib import Path
import sys
import unittest


APP_ROOT = Path(__file__).resolve().parents[1] / "van_gogh_2/rootfs/app"
sys.path.insert(0, str(APP_ROOT))

import product_update  # noqa: E402
import ha_client  # noqa: E402


TARGET_VERSION = (
    "2.0.0-suite.2-owner-corrections.66-mobile-stage5.8-"
    "checkpoint6-all-off.28-builder-runtime-guard.1"
)


class _Package:
    def __init__(self, *, version: str = TARGET_VERSION, resource_url: str | None = None) -> None:
        self.manifest = {
            "package_version": version,
            "component_version": version,
            "build_identity": {"release_component_version": version},
            "resource_url": resource_url
            or f"/local/van-gogh2/{version}/van-gogh2.js",
        }


class DynamicFrontendReleaseTests(unittest.TestCase):
    def test_frontend_source_and_target_follow_exact_package_release_path(self) -> None:
        package = _Package()
        target = Path("www") / "van-gogh2" / TARGET_VERSION

        self.assertEqual(product_update._foundation_targets(package)["frontend"], target)
        self.assertEqual(
            product_update._foundation_sources(package, Path("/tmp/foundation-release"))["frontend"],
            Path("/tmp/package") / target,
        )

    def test_mismatched_or_traversal_resource_url_fails_closed(self) -> None:
        stale = _Package(
            resource_url=(
                "/local/van-gogh2/"
                "2.0.0-suite.2-owner-corrections.66-mobile-stage5.8-"
                "checkpoint6-all-off.27/van-gogh2.js"
            )
        )
        traversal = _Package(resource_url="/local/van-gogh2/../van-gogh2.js")

        with self.assertRaisesRegex(product_update.ProductUpdateError, "frontend resource"):
            product_update._foundation_targets(stale)
        with self.assertRaisesRegex(product_update.ProductUpdateError, "frontend resource"):
            product_update._foundation_targets(traversal)

    def test_receipt_target_is_bounded_to_the_recorded_release_directory(self) -> None:
        package = _Package()
        expected = Path("www") / "van-gogh2" / TARGET_VERSION
        row = {"key": "frontend", "target": expected.as_posix(), "previous_present": False}

        self.assertEqual(product_update._foundation_receipt_target(row), expected)
        with self.assertRaisesRegex(product_update.ProductUpdateError, "rollback receipt"):
            product_update._foundation_receipt_target(
                {"key": "frontend", "target": "../../outside", "previous_present": False}
            )

    def test_installer_has_no_fixed_successor_frontend_version(self) -> None:
        source = (APP_ROOT / "product_update.py").read_text(encoding="utf-8")
        self.assertNotIn("FRONTEND_VERSION =", source)
        self.assertNotIn("checkpoint6-all-off.27", source)

    def test_installer_resource_url_is_the_exact_queryless_release_path(self) -> None:
        expected = f"/local/van-gogh2/{TARGET_VERSION}/van-gogh2.js"

        self.assertEqual(ha_client.module_url_for_release(TARGET_VERSION), expected)
        self.assertEqual(ha_client.validate_module_url(expected), expected)
        with self.assertRaisesRegex(ha_client.HAError, "module URL"):
            ha_client.validate_module_url(f"{expected}?v={TARGET_VERSION}")


if __name__ == "__main__":
    unittest.main()
