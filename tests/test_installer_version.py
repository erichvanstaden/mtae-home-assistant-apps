from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.2.13"


class InstallerVersionConsistencyTests(unittest.TestCase):
    def test_config_and_runtime_report_release_version(self) -> None:
        config = (ROOT / "van_gogh_2/config.yaml").read_text(encoding="utf-8")
        self.assertIn(f'version: "{EXPECTED_VERSION}"', config)

        module = ast.parse(
            (ROOT / "van_gogh_2/rootfs/app/app.py").read_text(encoding="utf-8")
        )
        assignments = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "INSTALLER_VERSION"
        }
        self.assertEqual(assignments.get("INSTALLER_VERSION"), EXPECTED_VERSION)

    def test_release_notes_name_the_candidate_pair(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "van_gogh_2/CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"Installer `{EXPECTED_VERSION}`", readme)
        self.assertIn("2.0.0-suite.2-owner-corrections.65", readme)
        self.assertIn(f"## {EXPECTED_VERSION}", changelog)
        self.assertIn("2.0.0-suite.2-owner-corrections.65", changelog)
        self.assertIn("from exact Product `.59`", changelog)
        self.assertIn("retaining exact Product `.59` as rollback", changelog)

    def test_aarch64_build_requires_binary_cp314_dependencies(self) -> None:
        dockerfile = (ROOT / "van_gogh_2/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--only-binary=:all:", dockerfile)
        self.assertIn('"PyYAML==6.0.3"', dockerfile)
        self.assertNotIn('"PyYAML==6.0.2"', dockerfile)


if __name__ == "__main__":
    unittest.main()
