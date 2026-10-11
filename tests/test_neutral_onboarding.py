import json
import stat
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))
import app  # noqa: E402
from installer import InstallError  # noqa: E402


def activation(profile: str = "wall-10") -> dict:
    return {
        "credential": "test-credential-not-for-evidence",
        "site_id": "a" * 32,
        "site_name": "Neutral Site",
        "channel": "staging",
        "profile": profile,
        "site_manifest": {
            "schema_version": 1,
            "site": {"id": "a" * 32, "title": "Neutral Site"},
            "panel": {
                "id": "neutral-panel",
                "type": "phone" if profile == "mobile/app" else "wall",
                "profile": profile,
                "display_mode": "companion-app" if profile == "mobile/app" else "ha-shell",
            },
        },
    }


class NeutralOnboardingTests(unittest.TestCase):
    def paths(self, root: Path):
        data = root / "data"
        config = root / "config"
        return mock.patch.multiple(
            app,
            DATA_ROOT=data,
            CONFIG_ROOT=config,
            SETTINGS=data / "settings.json",
            SITE_MANIFEST=config / "van_gogh2-site.json",
        )

    def test_clean_activation_creates_neutral_manifest_and_protected_settings(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            state = app._commit_activation(activation("mobile/app"))
            self.assertEqual(state, "created_neutral")
            manifest = json.loads(app.SITE_MANIFEST.read_text(encoding="utf-8"))
            settings = json.loads(app.SETTINGS.read_text(encoding="utf-8"))
            self.assertEqual(manifest["panel"]["profile"], "mobile/app")
            self.assertNotIn("site_manifest", settings)
            self.assertEqual(settings["channel"], "staging")
            self.assertEqual(stat.S_IMODE(app.SITE_MANIFEST.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(app.SETTINGS.stat().st_mode), 0o600)

    def test_existing_manifest_is_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app.CONFIG_ROOT.mkdir(parents=True)
            before = b'{"panel":{"profile":"wall-10"},"site":{"id":"owner-existing"}}\n'
            app.SITE_MANIFEST.write_bytes(before)
            state = app._commit_activation(activation("mobile/app"))
            self.assertEqual(state, "preserved_existing")
            self.assertEqual(app.SITE_MANIFEST.read_bytes(), before)
            self.assertEqual(app._site_manifest_profile(), "wall-10")

    def test_invalid_existing_manifest_fails_before_code_exchange(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app.CONFIG_ROOT.mkdir(parents=True)
            app.SITE_MANIFEST.write_text("not-json", encoding="utf-8")
            with self.assertRaises(InstallError):
                app._site_manifest_profile()

    def test_status_does_not_return_credential(self):
        with tempfile.TemporaryDirectory() as temporary, self.paths(Path(temporary)):
            app._commit_activation(activation())
            status = app._status()
            self.assertNotIn("credential", status)
            self.assertNotIn("test-credential", json.dumps(status, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
