import copy
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from product_update import build_foundation_release, inspect_package  # noqa: E402


EXPECTED_ROUTES = {
    "wall-10": ["home", "lights", "windows", "ac", "rooms", "security", "irrigation"],
    "mobile/app": [
        "home",
        "lights",
        "windows",
        "climate",
        "security",
        "rooms",
        "irrigation",
        "media",
        "access",
        "more",
        "settings",
    ],
}
STARTERS = {
    "wall-10": "starter/wall-10-dashboard.json",
    "mobile/app": "starter/mobile-app-dashboard.json",
}
PROFILE_IDS = {"wall-10": "wall-10", "mobile/app": "mobile-app"}
LIVE_BINDING = re.compile(
    r"^(?:alarm_control_panel|automation|binary_sensor|button|camera|climate|cover|"
    r"device_tracker|fan|input_[a-z_]+|light|lock|media_player|person|scene|script|"
    r"select|sensor|switch|timer|weather)\.[a-z0-9_]+$",
    re.IGNORECASE,
)
FORBIDDEN_DONOR_TOKENS = (
    "van staden",
    "vshome",
    "/ellie-mobile/",
    "beau",
    "keegan",
    "family_whole_home",
    "arrowhead_eci",
    "shellypro",
    "shelly1mini",
    "7c2c67",
    "ccba97",
    "master_bedroom",
)


def scalar_strings(value, pointer=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from scalar_strings(child, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from scalar_strings(child, f"{pointer}/{index}")
    elif isinstance(value, str):
        yield pointer, value


class TargetProfilePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads((ROOT / "dist" / "RELEASE-MANIFEST.json").read_text())
        cls.archive = ROOT / "dist" / cls.release["archive"]
        cls.package = inspect_package(cls.archive, cls.release["sha256"])
        cls.example = json.loads(
            (ROOT / "tests" / "fixtures" / "site-manifest.example.json").read_text()
        )

    def starter(self, profile):
        with tarfile.open(self.archive, "r:gz") as bundle:
            member = bundle.getmember(STARTERS[profile])
            return json.load(bundle.extractfile(member))

    def assert_neutral_profile(self, profile, dashboard):
        self.assertEqual(dashboard["mtae_profile"]["id"], PROFILE_IDS[profile])
        self.assertEqual([view["path"] for view in dashboard["views"]], EXPECTED_ROUTES[profile])
        serialized = json.dumps(dashboard, sort_keys=True).lower()
        for token in FORBIDDEN_DONOR_TOKENS:
            self.assertNotIn(token, serialized)
        live = [
            {"pointer": pointer, "value": value}
            for pointer, value in scalar_strings(dashboard)
            if LIVE_BINDING.fullmatch(value)
        ]
        self.assertEqual(live, [], f"neutral starter retained live bindings: {live[:10]}")

    def test_exact_package_contains_neutral_wall_and_mobile_starters(self):
        for profile in EXPECTED_ROUTES:
            with self.subTest(profile=profile):
                self.assert_neutral_profile(profile, self.starter(profile))

    def test_component_preserves_non_config_flow_contract(self):
        with tarfile.open(self.archive, "r:gz") as bundle:
            names = set(bundle.getnames())
            manifest_file = bundle.extractfile("custom_components/van_gogh2/manifest.json")
            component_file = bundle.extractfile("custom_components/van_gogh2/__init__.py")
            self.assertIsNotNone(manifest_file)
            self.assertIsNotNone(component_file)
            assert manifest_file is not None and component_file is not None
            manifest = json.load(manifest_file)
            component = component_file.read().decode("utf-8")

        self.assertIs(manifest["config_flow"], False)
        self.assertNotIn("custom_components/van_gogh2/config_flow.py", names)
        self.assertNotIn("async_setup_entry", component)

    def test_installer_selects_exact_target_profile_route_family(self):
        for profile, routes in EXPECTED_ROUTES.items():
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                site = copy.deepcopy(self.example)
                site["site"] = {"id": "neutral-install", "title": "Neutral Install"}
                site["panel"] = {
                    "id": "neutral-install-panel",
                    "type": "mobile" if profile == "mobile/app" else "wall",
                    "profile": profile,
                    "display_mode": "ha-shell",
                }
                site_path = workspace / "site.json"
                site_path.write_text(json.dumps(site, indent=2, sort_keys=True) + "\n")
                _, dashboard = build_foundation_release(
                    self.package,
                    site_path,
                    workspace / "build",
                )
                self.assertEqual([view["path"] for view in dashboard["views"]], routes)
                self.assertEqual(dashboard["mtae_profile"]["id"], PROFILE_IDS[profile])


if __name__ == "__main__":
    unittest.main()
