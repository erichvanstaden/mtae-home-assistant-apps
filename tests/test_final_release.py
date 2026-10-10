import ast
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "van_gogh_2"
APP = ADDON / "rootfs" / "app"
BUILDER23_SHA256 = "c41d81c1615cebadabea8f1eff419c45bc8d74856d1698453c728d2cd46e39b9"


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
        self.assertEqual(yaml_scalar(config, "version"), "1.2.1")

    def test_runtime_reports_final_installer_version(self):
        self.assertEqual(assignment_string(APP / "app.py", "INSTALLER_VERSION"), "1.2.1")

    def test_frozen_builder23_package_is_the_release_payload(self):
        release = json.loads((ROOT / "dist" / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        archive = ROOT / "dist" / release["archive"]
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(digest, BUILDER23_SHA256)
        self.assertEqual(release["sha256"], BUILDER23_SHA256)
        self.assertEqual(release["package_version"], "1.0.0-builder.23-p0.3")
        self.assertEqual(release["component_version"], "2.0.0-builder.23-p0.3")

    def test_upgrade_safe_data_and_config_mounts_remain_stable(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        app = (APP / "app.py").read_text(encoding="utf-8")
        self.assertIn('homeassistant_config:rw', config)
        self.assertIn('VAN_GOGH_DATA_ROOT", "/data"', app)
        self.assertIn('VAN_GOGH_CONFIG_ROOT", "/homeassistant"', app)
        self.assertIn('SETTINGS = DATA_ROOT / "settings.json"', app)
        self.assertNotIn("rmtree(DATA_ROOT", app)
        self.assertNotIn("unlink(missing_ok=True)", app)

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
            self.assertIn("1.2.1", source)
            self.assertIn("1.0.0-builder.23-p0.3", source)
            self.assertIn("2.0.0-builder.23-p0.3", source)
        self.assertNotIn("Installer 1.1.17 accepts only", readme)
        self.assertNotIn("Migrate Home C-grid", readme)
        self.assertNotIn("Create / refresh Family review", readme)


if __name__ == "__main__":
    unittest.main()
