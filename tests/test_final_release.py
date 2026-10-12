import ast
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "van_gogh_2"
APP = ADDON / "rootfs" / "app"
BUILDER22_DONOR_SHA256 = "8f98971c808b8116d65473fa6d1995f2a046b3d1708f275eaea8689e8fa825b7"


def assignment_string(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise AssertionError(f"{name} is not a literal string assignment in {path}")


def yaml_scalar(path: Path, key: str) -> str:
    prefix = f"{key}:"
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith(prefix):
            return raw.split(":", 1)[1].strip().strip('"').strip("'")
    raise AssertionError(f"{key} is missing from {path}")


class FinalReleaseIdentityTests(unittest.TestCase):
    def test_public_namespace_is_preserved_at_final_version(self):
        config = ADDON / "config.yaml"
        self.assertEqual(yaml_scalar(config, "name"), "MTAE Van Gogh Installer")
        self.assertEqual(yaml_scalar(config, "slug"), "van_gogh_2")
        self.assertEqual(yaml_scalar(config, "version"), "1.2.4")

    def test_runtime_reports_final_installer_version(self):
        self.assertEqual(assignment_string(APP / "app.py", "INSTALLER_VERSION"), "1.2.4")

    def test_frozen_builder22_successor_is_the_release_payload(self):
        release = json.loads((ROOT / "dist" / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        archive = ROOT / "dist" / release["archive"]
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(release["sha256"], digest)
        self.assertEqual(release["predecessor_package_sha256"], BUILDER22_DONOR_SHA256)
        self.assertEqual(release["package_version"], "1.0.0-builder.22-deployable.6")
        self.assertEqual(release["component_version"], "2.0.0-builder.3")
        self.assertEqual(
            release["module_url"],
            "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js",
        )

    def test_upgrade_safe_data_and_config_mounts_remain_stable(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        app = (APP / "app.py").read_text(encoding="utf-8")
        self.assertIn('homeassistant_config:rw', config)
        self.assertIn('VAN_GOGH_DATA_ROOT", "/data"', app)
        self.assertIn('VAN_GOGH_CONFIG_ROOT", "/homeassistant"', app)
        self.assertIn('SETTINGS = DATA_ROOT / "settings.json"', app)
        self.assertNotIn("rmtree(DATA_ROOT", app)
        self.assertNotIn("rmtree(CONFIG_ROOT", app)

    def test_declared_architectures_use_commissioned_python_314_bases(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        build = (ADDON / "build.yaml").read_text(encoding="utf-8")
        for architecture in ("amd64", "aarch64"):
            self.assertIn(f"  - {architecture}", config)
            self.assertIn(
                f'{architecture}: "ghcr.io/home-assistant/{architecture}-base-python:3.14-alpine3.23"',
                build,
            )

    def test_ingress_ui_describes_the_product_package_contract(self):
        source = (APP / "index.html").read_text(encoding="utf-8")
        self.assertIn("validates the shared product package and local site manifest", source)
        self.assertIn("applying_product_update", source)
        self.assertIn("Restoring protected files and dashboard", source)
        self.assertNotIn("Migrate Home layout", source)
        self.assertNotIn("Create / refresh Family review", source)

    def test_public_release_documentation_names_the_final_payload(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ADDON / "CHANGELOG.md").read_text(encoding="utf-8")
        for source in (readme, changelog):
            self.assertIn("1.2.4", source)
            self.assertIn("1.0.0-builder.22-deployable.6", source)
            self.assertIn("2.0.0-builder.3", source)
        self.assertNotIn("Installer 1.1.17 accepts only", readme)
        self.assertNotIn("Migrate Home C-grid", readme)
        self.assertNotIn("Create / refresh Family review", readme)


if __name__ == "__main__":
    unittest.main()
