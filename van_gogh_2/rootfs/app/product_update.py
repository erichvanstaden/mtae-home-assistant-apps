"""Site-neutral Van Gogh product-package planning, application and rollback."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from dashboard_migration import HOME_VIEW_TYPE, MigrationError, discover_dashboard
from installer import InstallManager, safe_extract, sha256_file

PACKAGE_SCHEMA = "mtae.van-gogh.product-package.v1"
CATALOGUE_SCHEMA = "mtae.van-gogh.feature-catalogue.v1"
PRODUCT_SCHEMA_VERSION = 2
FOUNDATION_BUNDLE_ID = "van-gogh-foundation-site-neutral"
FOUNDATION_VERSION = "1.5.0"
FRONTEND_VERSION = "2.0.0-suite.1"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
PATH = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SECRET_WORDS = re.compile(r"(?:password|secret|token|credential|api[_-]?key|install[_-]?code)", re.I)
PRODUCT_MARKER = "van_gogh_product"
MANAGED_FOUNDATION = "van_gogh_managed_foundation"
FOUNDATION_TARGETS = {
    "assets": Path("www") / "van-gogh" / FOUNDATION_VERSION,
    "frontend": Path("www") / "van-gogh2" / FRONTEND_VERSION,
    "owner_maintenance": Path("custom_components") / "mtae_owner_maintenance",
}
SUPPORTED_DASHBOARD_TARGETS = ("wall-10", "mobile/app")


class ProductUpdateError(RuntimeError):
    """Raised before mutation when a package, site, or installed state is unsafe."""


@dataclass(frozen=True)
class ProductPackage:
    manifest: dict[str, Any]
    catalogue: dict[str, Any]
    site_schema: dict[str, Any]
    archive: Path
    archive_sha256: str


@dataclass(frozen=True)
class ProductPlan:
    state: str
    url_path: str
    title: str
    before: dict[str, Any] | None
    after: dict[str, Any]
    dashboard_changed: bool


def _json_member(package: tarfile.TarFile, name: str) -> dict[str, Any]:
    try:
        member = package.getmember(name)
        stream = package.extractfile(member)
    except (KeyError, tarfile.TarError) as error:
        raise ProductUpdateError(f"Product package is missing {name}") from error
    if stream is None or not member.isfile() or member.size > 2 * 1024 * 1024:
        raise ProductUpdateError(f"Product package member {name} is invalid")
    try:
        value = json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductUpdateError(f"Product package member {name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise ProductUpdateError(f"Product package member {name} must be an object")
    return value


def _version(value: Any) -> tuple[int, int, int, tuple[tuple[int, str], ...]]:
    match = SEMVER.fullmatch(str(value))
    if not match:
        raise ProductUpdateError(f"Invalid semantic version: {value!r}")
    prerelease: list[tuple[int, str]] = []
    if match.group(4):
        for part in match.group(4).split("."):
            prerelease.append((0, f"{int(part):020d}") if part.isdigit() else (1, part))
    else:
        prerelease.append((2, ""))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(prerelease)


def _in_range(value: str, policy: Any, label: str) -> None:
    if not isinstance(policy, dict):
        raise ProductUpdateError(f"Product package {label} compatibility is missing")
    minimum = policy.get("minimum")
    maximum = policy.get("maximum_exclusive")
    parsed = _version(value)
    if parsed < _version(minimum) or parsed >= _version(maximum):
        raise ProductUpdateError(f"{label} {value} is outside supported range >={minimum},<{maximum}")


def inspect_package(archive: Path, expected_sha256: str | None = None) -> ProductPackage:
    """Verify outer identity, exact inventory, and independent bundled identities."""
    actual_sha = sha256_file(archive)
    if expected_sha256 is not None and actual_sha != expected_sha256:
        raise ProductUpdateError("Product-package SHA-256 does not match release metadata")
    try:
        with tarfile.open(archive, "r:gz") as package:
            members = package.getmembers()
            by_name = {member.name.removeprefix("./"): member for member in members}
            if len(by_name) != len(members):
                raise ProductUpdateError("Product package contains duplicate member names")
            for name, member in by_name.items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or not member.isfile():
                    raise ProductUpdateError(f"Product package contains unsafe member {name!r}")
            manifest = _json_member(package, "product-package.json")
            if manifest.get("schema") != PACKAGE_SCHEMA or manifest.get("product") != "van-gogh2":
                raise ProductUpdateError("Product-package schema or product identity is unsupported")
            _version(manifest.get("package_version"))
            _version(manifest.get("component_version"))
            inventory = manifest.get("file_inventory")
            if not isinstance(inventory, list) or not inventory:
                raise ProductUpdateError("Product-package file inventory is missing")
            expected_paths: set[str] = set()
            for row in inventory:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise ProductUpdateError("Product-package file inventory row is invalid")
                name = row["path"]
                if name in expected_paths or name == "product-package.json":
                    raise ProductUpdateError("Product-package file inventory is ambiguous")
                expected_paths.add(name)
                member = by_name.get(name)
                stream = package.extractfile(member) if member is not None else None
                if member is None or stream is None or not member.isfile():
                    raise ProductUpdateError(f"Product-package inventory member {name!r} is missing")
                data = stream.read()
                if len(data) != row.get("size") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
                    raise ProductUpdateError(f"Product-package inventory member {name!r} failed verification")
            if set(by_name) - {"product-package.json"} != expected_paths:
                raise ProductUpdateError("Product-package file set does not match its inventory")
            catalogue_name = manifest.get("feature_catalogue")
            site_schema_meta = manifest.get("site_manifest_schema")
            foundation = manifest.get("foundation")
            if not isinstance(catalogue_name, str) or not isinstance(site_schema_meta, dict):
                raise ProductUpdateError("Product-package catalogue/schema pointers are invalid")
            if not isinstance(foundation, dict):
                raise ProductUpdateError("Product-package Foundation identity is missing")
            catalogue = _json_member(package, catalogue_name)
            site_schema_path = site_schema_meta.get("path")
            if not isinstance(site_schema_path, str):
                raise ProductUpdateError("Product-package site schema path is invalid")
            site_schema = _json_member(package, site_schema_path)
            bundle = _json_member(package, str(foundation.get("bundle_manifest") or ""))
            component = _json_member(package, "custom_components/van_gogh2/manifest.json")
    except (OSError, tarfile.TarError) as error:
        raise ProductUpdateError("Product package is not a readable gzip tar archive") from error
    if component.get("domain") != "van_gogh2" or component.get("version") != manifest.get("component_version"):
        raise ProductUpdateError("Shared component identity does not match product-package metadata")
    if (
        foundation.get("bundle_id") != FOUNDATION_BUNDLE_ID
        or foundation.get("version") != FOUNDATION_VERSION
        or bundle.get("bundle_id") != FOUNDATION_BUNDLE_ID
        or bundle.get("version") != FOUNDATION_VERSION
    ):
        raise ProductUpdateError("Bundled Foundation identity is unsupported")
    schema_inventory = {row["path"]: row for row in manifest["file_inventory"]}
    schema_row = schema_inventory.get(site_schema_path)
    if not schema_row or schema_row.get("sha256") != site_schema_meta.get("sha256"):
        raise ProductUpdateError("Canonical site-manifest schema identity does not match inventory")
    if site_schema_meta.get("version") != 1:
        raise ProductUpdateError("Canonical site-manifest schema version is unsupported")
    if catalogue.get("schema") != CATALOGUE_SCHEMA or catalogue.get("schema_version") != 1:
        raise ProductUpdateError("Feature catalogue schema is unsupported")
    if (
        catalogue.get("foundation_bundle_id") != FOUNDATION_BUNDLE_ID
        or catalogue.get("foundation_version") != FOUNDATION_VERSION
    ):
        raise ProductUpdateError("Feature catalogue does not target the bundled Foundation")
    return ProductPackage(manifest, catalogue, site_schema, archive, actual_sha)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SECRET_WORDS.search(str(key)) or _contains_secret_key(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False


def _pointer(value: dict[str, Any], pointer: str) -> Any:
    current: Any = value
    if not pointer.startswith("/"):
        raise ProductUpdateError(f"Feature catalogue pointer is invalid: {pointer!r}")
    for segment in pointer[1:].split("/"):
        if not isinstance(current, dict) or segment not in current:
            raise ProductUpdateError(f"Site manifest does not satisfy feature pointer {pointer}")
        current = current[segment]
    return current


def validate_site_manifest(site: Any, package: ProductPackage) -> dict[str, Any]:
    """Validate the untouched local manifest against the packaged canonical schema."""
    if not isinstance(site, dict):
        raise ProductUpdateError("Local site manifest must be a JSON object")
    try:
        Draft202012Validator.check_schema(package.site_schema)
        Draft202012Validator(package.site_schema).validate(site)
    except (SchemaError, ValidationError) as error:
        path = "/" + "/".join(str(part) for part in getattr(error, "absolute_path", []))
        raise ProductUpdateError(f"Local site manifest fails canonical schema at {path}: {error.message}") from error
    if site.get("schema_version") != package.manifest["site_manifest_schema"]["version"]:
        raise ProductUpdateError("Local site-manifest version is unsupported")
    if _contains_secret_key(site):
        raise ProductUpdateError("Secrets and credentials are forbidden in the site manifest")
    features = package.catalogue.get("features")
    if not isinstance(features, dict) or not features:
        raise ProductUpdateError("Feature catalogue entries are invalid")
    for name, entry in features.items():
        if not isinstance(entry, dict) or entry.get("optional") is not True:
            raise ProductUpdateError(f"Feature catalogue entry {name} is invalid")
        enabled = _pointer(site, str(entry.get("manifest_pointer") or ""))
        content = _pointer(site, str(entry.get("content_pointer") or ""))
        if not isinstance(enabled, bool) or not isinstance(content, list):
            raise ProductUpdateError(f"Feature catalogue entry {name} does not match the canonical manifest")
        if enabled and not content:
            raise ProductUpdateError(f"Enabled feature {name} has no configured content")
    return deepcopy(site)


def _normalise_dashboard_targets(value: Any, fallback: str) -> list[str]:
    requested = [fallback] if value is None else value
    if not isinstance(requested, list) or not requested:
        raise ProductUpdateError("At least one dashboard target is required")
    if any(
        not isinstance(item, str) or item not in SUPPORTED_DASHBOARD_TARGETS
        for item in requested
    ):
        raise ProductUpdateError("Dashboard target selection is unsupported")
    return [target for target in SUPPORTED_DASHBOARD_TARGETS if target in requested]


def _site_for_dashboard_target(site: dict[str, Any], profile: str) -> dict[str, Any]:
    target = deepcopy(site)
    site_id = str(target["site"]["id"])
    if profile == "mobile/app":
        target["site"]["title"] = f'{target["site"]["title"]} Mobile'
    target["panel"] = {
        **target["panel"],
        "id": f"{site_id}-mobile" if profile == "mobile/app" else f"{site_id}-wall10",
        "type": "phone" if profile == "mobile/app" else "wall",
        "profile": profile,
        "display_mode": "companion-app" if profile == "mobile/app" else "ha-shell",
    }
    return target


def _dashboard_path(site: dict[str, Any]) -> str:
    site_id = str(site["site"]["id"])
    return f"{site_id}-mobile" if site["panel"]["profile"] == "mobile/app" else site_id


def _enabled_features(site: dict[str, Any], package: ProductPackage) -> list[str]:
    return sorted(
        name
        for name, entry in package.catalogue["features"].items()
        if _pointer(site, entry["manifest_pointer"]) is True
    )


def _schema_marker(package: ProductPackage, site: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PRODUCT_SCHEMA_VERSION,
        "package_version": package.manifest["package_version"],
        "component_version": package.manifest["component_version"],
        "foundation_version": package.manifest["foundation"]["version"],
        "site_id": site["site"]["id"],
        "site_schema_version": site["schema_version"],
        "feature_catalogue_version": package.catalogue["catalogue_version"],
        "enabled_features": _enabled_features(site, package),
    }


def build_foundation_release(
    package: ProductPackage,
    site_manifest_path: Path,
    workspace: Path,
) -> tuple[Path, dict[str, Any]]:
    """Run the exact packaged Foundation generator in an isolated local directory."""
    extracted = workspace / "package"
    release_root = workspace / "foundation-release"
    safe_extract(package.archive, extracted)
    foundation_root = extracted / package.manifest["foundation"]["root"]
    site = json.loads(site_manifest_path.read_text(encoding="utf-8"))
    profile = site.get("panel", {}).get("profile")
    target_profile_starters = {
        "wall-10": "wall-10-dashboard.json",
        "mobile/app": "mobile-app-dashboard.json",
    }
    if profile not in target_profile_starters:
        raise ProductUpdateError(f"Unsupported target profile: {profile}")
    generator_manifest = site_manifest_path.resolve()
    if profile == "mobile/app":
        # Foundation v1 deliberately has no mobile renderer.  Use an equivalent
        # wall manifest only to generate shared assets/contracts; the frozen
        # Builder.22 target-profile starter supplies the dashboard below.
        foundation_site = deepcopy(site)
        foundation_site["panel"] = {
            **foundation_site["panel"],
            "id": f"{foundation_site['panel']['id']}-foundation",
            "type": "wall",
            "profile": "wall-10",
            "display_mode": "ha-shell",
        }
        generator_manifest = workspace / "mobile-foundation-input.json"
        generator_manifest.write_text(
            json.dumps(foundation_site, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    command = [
        sys.executable,
        "-m",
        "van_gogh.tools.cli",
        "build",
        str(generator_manifest),
        "--output",
        str(release_root),
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(foundation_root)
    completed = subprocess.run(
        command,
        cwd=foundation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        safe_detail = detail[-1][:300] if detail else "unknown generator failure"
        raise ProductUpdateError(f"Canonical Foundation generator rejected the site manifest: {safe_detail}")
    starter = extracted / "starter" / target_profile_starters[profile]
    contract_path = extracted / "starter" / "PROFILE-ADAPTER-CONTRACT.json"
    try:
        dashboard = json.loads(starter.read_text(encoding="utf-8"))
        adapter_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        profile_contract = adapter_contract["profiles"][profile]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ProductUpdateError(
            f"Builder.22 package has no valid neutral {profile} starter contract"
        ) from error
    routes = [view.get("path") for view in dashboard.get("views", [])]
    if (
        adapter_contract.get("schema") != "mtae.van-gogh.neutral-target-profile-adapter.v1"
        or routes != profile_contract.get("routes")
        or dashboard.get("mtae_profile", {}).get("id") != profile_contract.get("profile_id")
    ):
        raise ProductUpdateError(f"Builder.22 neutral {profile} starter failed its profile contract")

    site_id = str(site["site"]["id"])
    site_title = str(site["site"]["title"])
    replacements = {
        "neutral-target": site_id,
        "van-gogh": site_id,
        "Van Gogh": site_title,
        "starter-v1:neutral-target": f"starter-v1:{site_id}",
    }

    def replace_starter_identity(value: Any) -> Any:
        if isinstance(value, str):
            if value in replacements:
                return replacements[value]
            if value == "/van-gogh":
                return f"/{site_id}"
            if value.startswith("/van-gogh/"):
                return f"/{site_id}/{value.removeprefix('/van-gogh/')}"
            return value
        if isinstance(value, list):
            return [replace_starter_identity(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_starter_identity(item) for key, item in value.items()}
        return value

    dashboard = replace_starter_identity(dashboard)
    (release_root / "dashboard.json").write_text(
        json.dumps(dashboard, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_manifest_path = release_root / "release.json"
    release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    release_manifest["target_profile"] = profile
    release_manifest["files"] = _tree_inventory(release_root, excluded={"release.json"})
    release_manifest_path.write_text(
        json.dumps(release_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        dashboard = json.loads((release_root / "dashboard.json").read_text(encoding="utf-8"))
        release_manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductUpdateError("Canonical Foundation generator did not emit a valid release") from error
    if (
        not isinstance(dashboard, dict)
        or not isinstance(dashboard.get("views"), list)
        or not dashboard["views"]
        or release_manifest.get("bundle_version") != FOUNDATION_VERSION
        or release_manifest.get("status") != "generated-not-deployed"
    ):
        raise ProductUpdateError("Canonical Foundation release identity or dashboard shape is unsupported")
    declared_files = release_manifest.get("files")
    if not isinstance(declared_files, dict) or declared_files != _tree_inventory(
        release_root, excluded={"release.json"}
    ):
        raise ProductUpdateError("Canonical Foundation release file inventory failed verification")
    return release_root, dashboard


def _managed_foundation_view(view: Any) -> bool:
    """Match only views carrying the updater's explicit ownership marker.

    The historical ``custom:van-gogh-wall10-view`` type is a supported schema
    marker, not an ownership grant. Current and legacy views of that type may
    contain local cards, bindings, order and settings that must survive.
    """
    return isinstance(view, dict) and MANAGED_FOUNDATION in view


def _compose_dashboard(
    generated: dict[str, Any],
    base: dict[str, Any] | None,
    package: ProductPackage,
    site: dict[str, Any],
) -> dict[str, Any]:
    foundation_views = deepcopy(generated["views"])
    for foundation_view in foundation_views:
        if not isinstance(foundation_view, dict):
            raise ProductUpdateError("Canonical Foundation release contains a non-object view")
        foundation_view[MANAGED_FOUNDATION] = {
            "schema_version": PRODUCT_SCHEMA_VERSION,
            "foundation_version": FOUNDATION_VERSION,
        }
    if base is None:
        result = deepcopy(generated)
        result["views"] = foundation_views
    else:
        result = deepcopy(base)
        views = result.get("views")
        if not isinstance(views, list):
            raise ProductUpdateError("Installed dashboard config has no view array")
        generated_by_path = {
            str(view.get("path")): view
            for view in foundation_views
            if isinstance(view.get("path"), str) and view.get("path")
        }
        consumed_paths: set[str] = set()
        preserved_paths: set[str] = set()
        composed: list[Any] = []
        for existing in views:
            path = str(existing.get("path") or "") if isinstance(existing, dict) else ""
            if _managed_foundation_view(existing):
                replacement = generated_by_path.get(path)
                if replacement is not None:
                    composed.append(replacement)
                    consumed_paths.add(path)
                # A previously managed optional route is removed when disabled.
                continue
            composed.append(deepcopy(existing))
            if path:
                preserved_paths.add(path)
        composed.extend(
            view
            for view in foundation_views
            if str(view.get("path") or "") not in consumed_paths | preserved_paths
        )
        result["views"] = composed
    for key in ("mtae_profile", "wallpanel"):
        if key in generated:
            result[key] = deepcopy(generated[key])
        else:
            result.pop(key, None)
    result[PRODUCT_MARKER] = _schema_marker(package, site)
    return result


def plan_update(
    package: ProductPackage,
    site: dict[str, Any],
    inventory: list[dict[str, Any]],
    generated: dict[str, Any],
) -> ProductPlan:
    """Create a complete plan; unsupported states never become writes."""
    target_path = _dashboard_path(site)
    if not PATH.fullmatch(target_path):
        raise ProductUpdateError("Canonical site id cannot be used as a dashboard path")
    target_rows = []
    for row in inventory:
        registry = row.get("registry") if isinstance(row, dict) else None
        if isinstance(registry, dict) and registry.get("url_path") == target_path:
            target_rows.append(row)
    if len(target_rows) > 1:
        raise ProductUpdateError("Multiple dashboards occupy the canonical site path")
    if target_rows:
        row = target_rows[0]
        registry = row.get("registry")
        before = row.get("config")
        if not isinstance(registry, dict) or not isinstance(before, dict):
            raise ProductUpdateError("Installed product dashboard snapshot is invalid")
        if PRODUCT_MARKER in before:
            marker = before[PRODUCT_MARKER]
            if not isinstance(marker, dict) or marker.get("schema_version") != PRODUCT_SCHEMA_VERSION:
                raise ProductUpdateError("Installed dashboard product schema is unsupported")
            if marker.get("site_id") not in {None, site["site"]["id"]}:
                raise ProductUpdateError("Installed dashboard belongs to a different canonical site")
            after = _compose_dashboard(generated, before, package, site)
            return ProductPlan(
                "current_upgrade",
                target_path,
                str(registry.get("title") or generated.get("title") or site["site"]["title"]),
                deepcopy(before),
                after,
                after != before,
            )
    try:
        existing = discover_dashboard(target_rows)
    except MigrationError as error:
        if str(error) != "No supported Installer-managed Van Gogh dashboard was discovered":
            raise ProductUpdateError(str(error)) from error
        productish = []
        for row in target_rows:
            config = row.get("config") if isinstance(row, dict) else None
            if isinstance(config, dict) and any(
                isinstance(view, dict) and (view.get("type") == HOME_VIEW_TYPE or MANAGED_FOUNDATION in view)
                for view in config.get("views", [])
            ):
                productish.append(row)
        if productish:
            raise ProductUpdateError("Unsupported Van Gogh dashboard state was discovered")
        if target_rows:
            raise ProductUpdateError("Clean-install dashboard path is already occupied")
        after = _compose_dashboard(generated, None, package, site)
        return ProductPlan("clean_install", target_path, str(generated.get("title") or site["site"]["title"]), None, after, True)
    after = _compose_dashboard(generated, existing.after, package, site)
    return ProductPlan(
        "legacy_upgrade" if existing.state == "upgrade" else "current_upgrade",
        existing.url_path,
        str(existing.registry.get("title") or generated.get("title") or site["site"]["title"]),
        deepcopy(existing.before),
        after,
        after != existing.before,
    )


def site_manifest_sha256(path: Path) -> str:
    return sha256_file(path)


def _tree_inventory(
    root: Path,
    *,
    excluded: set[str] | None = None,
    ignore_runtime_cache: bool = False,
) -> dict[str, dict[str, Any]] | None:
    """Return a normalized exact file inventory, or ``None`` for unsafe trees."""
    if not root.is_dir() or root.is_symlink():
        return None
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            return None
        if not path.is_file():
            continue
        if excluded and relative in excluded:
            continue
        if ignore_runtime_cache and ("__pycache__" in path.parts or path.suffix == ".pyc"):
            continue
        rows[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return rows


def _component_expected_inventory(package: ProductPackage) -> dict[str, dict[str, Any]]:
    prefix = "custom_components/van_gogh2/"
    return {
        row["path"][len(prefix):]: {"bytes": row["size"], "sha256": row["sha256"]}
        for row in package.manifest["file_inventory"]
        if row["path"].startswith(prefix)
    }


class ProductUpdateManager:
    """Transaction coordinator over component, Foundation files and dashboard."""

    def __init__(self, installer: InstallManager, ha: Any, installer_version: str) -> None:
        self.installer = installer
        self.ha = ha
        self.installer_version = installer_version
        self._pending_apply: dict[str, Any] | None = None

    def _foundation_sources(self, release_root: Path) -> dict[str, Path]:
        return {
            "assets": release_root / "assets",
            "frontend": release_root.parent / "package" / "www" / "van-gogh2" / FRONTEND_VERSION,
            "owner_maintenance": release_root / "custom_components" / "mtae_owner_maintenance",
        }

    def _managed_files_current(self, package: ProductPackage, release_root: Path) -> bool:
        component_actual = _tree_inventory(
            self.installer.target,
            ignore_runtime_cache=True,
        )
        if component_actual != _component_expected_inventory(package):
            return False
        for key, source in self._foundation_sources(release_root).items():
            expected = _tree_inventory(source)
            target = self.installer.config_root / FOUNDATION_TARGETS[key]
            if expected is None or _tree_inventory(target) != expected:
                return False
        return True

    def _install_foundation_files(self, release_root: Path, backup_id: str) -> list[dict[str, Any]]:
        backup_root = self.installer.backups / backup_id / "product-files"
        backup_root.mkdir(parents=True, exist_ok=False)
        rows: list[dict[str, Any]] = []
        completed: list[str] = []
        try:
            for key, source in self._foundation_sources(release_root).items():
                if not source.is_dir():
                    raise ProductUpdateError(f"Generated Foundation release is missing {key}")
                target = self.installer.config_root / FOUNDATION_TARGETS[key]
                previous_present = target.is_dir()
                created_parent_dirs: list[str] = []
                parent = target.parent
                while parent != self.installer.config_root:
                    if not parent.exists():
                        created_parent_dirs.append(
                            parent.relative_to(self.installer.config_root).as_posix()
                        )
                    parent = parent.parent
                if previous_present:
                    shutil.copytree(target, backup_root / key)
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = target.parent / f".{target.name}.new.{uuid.uuid4().hex}"
                displaced = target.parent / f".{target.name}.old.{uuid.uuid4().hex}"
                shutil.copytree(source, stage)
                if target.exists():
                    os.replace(target, displaced)
                os.replace(stage, target)
                shutil.rmtree(displaced, ignore_errors=True)
                completed.append(key)
                rows.append({
                    "key": key,
                    "previous_present": previous_present,
                    "created_parent_dirs": created_parent_dirs,
                })
        except Exception:
            for key in reversed(completed):
                target = self.installer.config_root / FOUNDATION_TARGETS[key]
                shutil.rmtree(target, ignore_errors=True)
                previous = backup_root / key
                if previous.is_dir():
                    shutil.copytree(previous, target)
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
        return rows

    def _foundation_rollback_plan(
        self, receipt: dict[str, Any]
    ) -> list[tuple[str, Path, Path | None]]:
        backup_id = receipt.get("backup_id")
        rows = receipt.get("foundation_files")
        if not isinstance(backup_id, str) or not isinstance(rows, list):
            raise ProductUpdateError("Product-update receipt has no Foundation file prestate")
        backup_root = self.installer.backups / backup_id / "product-files"
        plan: list[tuple[str, Path, Path | None]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("key") not in FOUNDATION_TARGETS:
                raise ProductUpdateError("Product-update Foundation rollback receipt is invalid")
            key = row["key"]
            previous_present = row.get("previous_present")
            if key in seen or not isinstance(previous_present, bool):
                raise ProductUpdateError("Product-update Foundation rollback receipt is invalid")
            source = backup_root / key if previous_present else None
            if source is not None and (not source.is_dir() or source.is_symlink()):
                raise ProductUpdateError(f"Foundation rollback backup is missing {key}")
            plan.append((key, self.installer.config_root / FOUNDATION_TARGETS[key], source))
            seen.add(key)
        if seen != set(FOUNDATION_TARGETS):
            raise ProductUpdateError("Product-update Foundation rollback receipt is incomplete")
        return plan

    def _restore_foundation_files(
        self,
        receipt: dict[str, Any],
        plan: list[tuple[str, Path, Path | None]] | None = None,
        stages: dict[str, Path | None] | None = None,
    ) -> int:
        plan = plan if plan is not None else self._foundation_rollback_plan(receipt)
        stages = stages if stages is not None else self._stage_foundation_rollback(plan)
        transaction = self._apply_staged_foundation_rollback(plan, stages)
        self._finalize_staged_foundation_rollback(transaction)
        self._cleanup_foundation_parent_dirs(receipt)
        return int(transaction["restored"])

    def _cleanup_foundation_parent_dirs(self, receipt: dict[str, Any]) -> None:
        """Remove only empty parent directories created by the corresponding install."""
        rows = receipt.get("foundation_files")
        if not isinstance(rows, list):
            return
        relative_paths = {
            relative
            for row in rows
            if isinstance(row, dict)
            for relative in row.get("created_parent_dirs", [])
            if isinstance(relative, str) and relative
        }
        for relative in sorted(
            relative_paths,
            key=lambda value: len(Path(value).parts),
            reverse=True,
        ):
            path = self.installer.config_root / relative
            try:
                path.rmdir()
            except OSError:
                pass

    def _apply_staged_foundation_rollback(
        self,
        plan: list[tuple[str, Path, Path | None]],
        stages: dict[str, Path | None],
    ) -> dict[str, Any]:
        """Apply staged predecessors while retaining exact current-state recovery."""
        transaction: dict[str, Any] = {
            "stages": stages,
            "swaps": [],
            "moved_targets": set(),
            "installed_targets": set(),
            "restored": 0,
            "applied": False,
            "committed": False,
        }
        restored = 0
        swaps: list[tuple[Path, Path | None, Path]] = []
        moved_targets: set[Path] = set()
        installed_targets: set[Path] = set()
        transaction["swaps"] = swaps
        transaction["moved_targets"] = moved_targets
        transaction["installed_targets"] = installed_targets
        try:
            for key, target, _source in reversed(plan):
                stage = stages[key]
                displaced = target.parent / f".{target.name}.displaced.{uuid.uuid4().hex}"
                target_present = target.exists() or target.is_symlink()
                swaps.append((target, stage, displaced))
                if target_present:
                    os.replace(target, displaced)
                    moved_targets.add(target)
                if stage is not None:
                    os.replace(stage, target)
                    installed_targets.add(target)
                restored += 1
        except Exception:
            try:
                self._recover_staged_foundation_rollback(transaction)
            except Exception as recovery_error:
                raise ProductUpdateError(
                    "Foundation rollback swap failed and current-state recovery failed"
                ) from recovery_error
            raise
        transaction["restored"] = restored
        transaction["applied"] = True
        return transaction

    def _recover_staged_foundation_rollback(self, transaction: dict[str, Any]) -> None:
        """Restore current Foundation trees retained by an uncommitted rollback."""
        if transaction["committed"]:
            raise ProductUpdateError("Committed Foundation rollback cannot be recovered")
        recovery_errors: list[str] = []
        moved_targets = transaction["moved_targets"]
        installed_targets = transaction["installed_targets"]
        for target, stage, displaced in reversed(transaction["swaps"]):
            try:
                if target in installed_targets or target in moved_targets:
                    self._remove_foundation_path(target)
                if target in moved_targets and (displaced.exists() or displaced.is_symlink()):
                    os.replace(displaced, target)
                self._remove_foundation_path(stage)
                self._remove_foundation_path(displaced)
            except Exception as recovery_error:
                recovery_errors.append(str(recovery_error))
        self._discard_foundation_stages(transaction["stages"])
        transaction["applied"] = False
        if recovery_errors:
            raise ProductUpdateError(
                "Foundation current-state recovery failed: " + "; ".join(recovery_errors)
            )

    def _finalize_staged_foundation_rollback(self, transaction: dict[str, Any]) -> None:
        """Release retained Foundation current state after product commit succeeds."""
        for _target, _stage, displaced in transaction["swaps"]:
            try:
                self._remove_foundation_path(displaced)
            except OSError:
                pass
        self._discard_foundation_stages(transaction["stages"])
        transaction["committed"] = True

    @staticmethod
    def _remove_foundation_path(path: Path | None) -> None:
        if path is None:
            return
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _discard_foundation_stages(stages: dict[str, Path | None]) -> None:
        for stage in stages.values():
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)

    def _stage_foundation_rollback(
        self,
        plan: list[tuple[str, Path, Path | None]],
    ) -> dict[str, Path | None]:
        stages: dict[str, Path | None] = {}
        try:
            for key, target, source in plan:
                if source is None:
                    stages[key] = None
                    continue
                stage = target.parent / f".{target.name}.rollback.{uuid.uuid4().hex}"
                stages[key] = stage
                shutil.copytree(source, stage)
        except Exception:
            self._discard_foundation_stages(stages)
            raise
        return stages

    def apply(
        self,
        archive: Path,
        release: dict[str, Any],
        site_manifest_path: Path,
        home_assistant_version: str,
        *,
        dashboard_targets: list[str] | None = None,
        defer_commit: bool = False,
    ) -> dict[str, Any]:
        if self._pending_apply is not None:
            raise ProductUpdateError("A previous product update transaction is still pending")
        package = inspect_package(archive, str(release.get("sha256") or ""))
        if str(release.get("package_version")) != package.manifest["package_version"]:
            raise ProductUpdateError("Release and package versions do not match")
        if str(release.get("foundation_version")) != package.manifest["foundation"]["version"]:
            raise ProductUpdateError("Release and Foundation versions do not match")
        _in_range(self.installer_version, package.manifest.get("installer"), "Installer")
        _in_range(home_assistant_version, package.manifest.get("home_assistant"), "Home Assistant")
        if not site_manifest_path.is_file():
            raise ProductUpdateError("Local canonical site manifest is missing")
        before_site_sha = site_manifest_sha256(site_manifest_path)
        try:
            site_raw = json.loads(site_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProductUpdateError("Local site manifest cannot be read as JSON") from error
        site = validate_site_manifest(site_raw, package)
        targets = _normalise_dashboard_targets(dashboard_targets, str(site["panel"]["profile"]))
        with tempfile.TemporaryDirectory(prefix="van-gogh-product-update-") as temporary:
            workspace = Path(temporary)
            generated_targets: list[tuple[str, dict[str, Any], Path, dict[str, Any]]] = []
            for index, target in enumerate(targets):
                target_site = _site_for_dashboard_target(site, target)
                target_manifest = workspace / f"site-{index}.json"
                target_manifest.write_text(
                    json.dumps(target_site, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                release_root, generated = build_foundation_release(
                    package,
                    target_manifest,
                    workspace / f"target-{index}",
                )
                generated_targets.append((target, target_site, release_root, generated))
            inventory = self.ha.storage_dashboard_inventory()
            plans = [
                plan_update(package, target_site, inventory, generated)
                for _target, target_site, _release_root, generated in generated_targets
            ]
            release_root = generated_targets[0][2]
            states = {plan.state for plan in plans}
            update_state = next(iter(states)) if len(states) == 1 else "mixed_update"
            current = None
            if self.installer.current_receipt.is_file():
                try:
                    current = json.loads(self.installer.current_receipt.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = None
            package_current = (
                self.installer.installed_version() == package.manifest["component_version"]
                and isinstance(current, dict)
                and current.get("package_version") == package.manifest["package_version"]
                and current.get("archive_sha256") == package.archive_sha256
                and current.get("site_manifest_sha256") == before_site_sha
                and self._managed_files_current(package, release_root)
            )
            if package_current and not any(plan.dashboard_changed for plan in plans):
                return {
                    "operation": "noop",
                    "state": update_state,
                    "package_version": package.manifest["package_version"],
                    "component_version": package.manifest["component_version"],
                    "foundation_version": package.manifest["foundation"]["version"],
                    "file_writes": 0,
                    "dashboard_writes": 0,
                    "site_manifest_writes": 0,
                    "site_manifest_sha256": before_site_sha,
                    "dashboard_targets": targets,
                }
            snapshots = [self.ha.dashboard_snapshot(plan.url_path) for plan in plans]
            component_changed = False
            foundation_changed = False
            dashboard_writes = 0
            install_receipt: dict[str, Any] | None = None
            foundation_rows: list[dict[str, Any]] = []
            try:
                install_manifest = {
                    "product": "van-gogh2",
                    "version": package.manifest["component_version"],
                    "sha256": package.archive_sha256,
                }
                install_receipt = self.installer.install_archive(
                    archive,
                    install_manifest,
                    defer_commit=True,
                )
                component_changed = True
                foundation_rows = self._install_foundation_files(release_root, install_receipt["backup_id"])
                foundation_changed = True
                for plan in plans:
                    if not plan.dashboard_changed:
                        continue
                    if plan.before is None:
                        self.ha.save_dashboard(plan.url_path, plan.after, title=plan.title)
                    else:
                        self.ha.save_existing_dashboard(plan.url_path, plan.before, plan.after)
                    dashboard_writes += 1
                generated_manifest_sha = sha256_file(release_root / "release.json")
                self.installer.annotate_current({
                    "package_version": package.manifest["package_version"],
                    "package_schema": package.manifest["schema"],
                    "foundation_version": package.manifest["foundation"]["version"],
                    "foundation_release_manifest_sha256": generated_manifest_sha,
                    "foundation_files": foundation_rows,
                    "site_manifest_sha256": before_site_sha,
                    "dashboard_prestate": snapshots[0],
                    "dashboard_path": plans[0].url_path,
                    "dashboard_state": update_state,
                    "dashboard_prestates": snapshots,
                    "dashboard_paths": [plan.url_path for plan in plans],
                    "dashboard_targets": targets,
                })
            except Exception as error:
                try:
                    self._recover_apply_state(
                        snapshots,
                        install_receipt,
                        foundation_rows,
                        foundation_changed,
                        component_changed,
                    )
                except Exception as recovery_error:
                    raise ProductUpdateError(
                        "Product update failed and exact prestate recovery failed"
                    ) from recovery_error
                raise ProductUpdateError(
                    "Product update failed; exact prestate restored automatically"
                ) from error
            after_site_sha = site_manifest_sha256(site_manifest_path)
            if after_site_sha != before_site_sha:
                try:
                    self._recover_apply_state(
                        snapshots,
                        install_receipt,
                        foundation_rows,
                        foundation_changed,
                        component_changed,
                    )
                except Exception as recovery_error:
                    raise ProductUpdateError(
                        "Site manifest changed and exact prestate recovery failed"
                    ) from recovery_error
                raise ProductUpdateError("Local site manifest changed during update")
            result = {
                "operation": "product_update",
                "state": update_state,
                "package_version": package.manifest["package_version"],
                "component_version": package.manifest["component_version"],
                "foundation_version": package.manifest["foundation"]["version"],
                "file_writes": int(component_changed) + len(foundation_rows),
                "dashboard_writes": dashboard_writes,
                "site_manifest_writes": 0,
                "site_manifest_sha256": before_site_sha,
                "rollback_available": component_changed and foundation_changed,
                "dashboard_targets": targets,
            }
            self._pending_apply = {
                "component_changed": component_changed,
                "foundation_changed": foundation_changed,
                "foundation_rows": foundation_rows,
                "install_receipt": install_receipt,
                "snapshots": snapshots,
            }
            if not defer_commit:
                self.commit_pending_apply()
            return result

    def _recover_apply_state(
        self,
        snapshots: dict[str, Any] | list[dict[str, Any]],
        install_receipt: dict[str, Any] | None,
        foundation_rows: list[dict[str, Any]],
        foundation_changed: bool,
        component_changed: bool,
    ) -> None:
        """Recover every mutable product surface before reporting a failed update."""
        recovery_errors: list[Exception] = []
        dashboard_snapshots = snapshots if isinstance(snapshots, list) else [snapshots]
        for snapshot in reversed(dashboard_snapshots):
            try:
                self.ha.restore_dashboard(snapshot)
            except Exception as error:
                recovery_errors.append(error)
        if foundation_changed and install_receipt is not None:
            try:
                rollback_receipt = dict(install_receipt)
                rollback_receipt["foundation_files"] = foundation_rows
                self._restore_foundation_files(rollback_receipt)
            except Exception as error:
                recovery_errors.append(error)
        if component_changed:
            try:
                self.installer.recover_pending_install()
            except Exception as error:
                recovery_errors.append(error)
        if recovery_errors:
            raise ProductUpdateError(
                "Product update exact prestate recovery failed"
            ) from recovery_errors[0]

    def commit_pending_apply(self) -> None:
        """Commit a deferred product update after app finalisation succeeds."""
        if self._pending_apply is None:
            raise ProductUpdateError("No product update transaction is pending")
        self.installer.commit_pending_install()
        self._pending_apply = None

    def recover_pending_apply(self) -> None:
        """Recover every product surface from a deferred update."""
        if self._pending_apply is None:
            raise ProductUpdateError("No product update transaction is pending")
        pending = self._pending_apply
        self._recover_apply_state(
            pending["snapshots"],
            pending["install_receipt"],
            pending["foundation_rows"],
            pending["foundation_changed"],
            pending["component_changed"],
        )
        self._pending_apply = None

    def rollback(self) -> dict[str, Any]:
        if self._pending_apply is not None:
            self.commit_pending_apply()
        if not self.installer.current_receipt.is_file():
            raise ProductUpdateError("No product-update receipt is available to roll back")
        receipt = json.loads(self.installer.current_receipt.read_text(encoding="utf-8"))
        has_multi_dashboard_receipt = (
            "dashboard_prestates" in receipt or "dashboard_paths" in receipt
        )
        if has_multi_dashboard_receipt:
            if not isinstance(receipt.get("dashboard_prestate"), dict):
                raise ProductUpdateError("Product-update receipt has no dashboard prestate")
            if not isinstance(receipt.get("dashboard_path"), str) or not receipt["dashboard_path"]:
                raise ProductUpdateError("Product-update receipt has no dashboard path")
        snapshots = receipt.get("dashboard_prestates")
        dashboard_paths = receipt.get("dashboard_paths")
        if snapshots is None and dashboard_paths is None:
            snapshots = [receipt.get("dashboard_prestate")]
            dashboard_paths = [receipt.get("dashboard_path")]
        if (
            not isinstance(snapshots, list)
            or not snapshots
            or any(not isinstance(snapshot, dict) for snapshot in snapshots)
        ):
            raise ProductUpdateError("Product-update receipt has no dashboard prestate")
        if (
            not isinstance(dashboard_paths, list)
            or len(dashboard_paths) != len(snapshots)
            or any(not isinstance(path, str) or not path for path in dashboard_paths)
        ):
            raise ProductUpdateError("Product-update receipt has no dashboard path")
        foundation_plan = self._foundation_rollback_plan(receipt)
        foundation_stages = self._stage_foundation_rollback(foundation_plan)
        try:
            component_transaction = self.installer.stage_rollback()
        except Exception:
            self._discard_foundation_stages(foundation_stages)
            raise
        current_dashboards = [self.ha.dashboard_snapshot(path) for path in dashboard_paths]
        foundation_transaction: dict[str, Any] | None = None
        try:
            self.installer.apply_staged_rollback(component_transaction)
            dashboards = [self.ha.restore_dashboard(snapshot) for snapshot in reversed(snapshots)]
            foundation_transaction = self._apply_staged_foundation_rollback(
                foundation_plan,
                foundation_stages,
            )
            foundation_restored = int(foundation_transaction["restored"])
            files = self.installer.commit_staged_rollback(component_transaction)
            self._finalize_staged_foundation_rollback(foundation_transaction)
            self._cleanup_foundation_parent_dirs(receipt)
        except Exception:
            recovery_errors: list[Exception] = []
            if foundation_transaction is not None and not foundation_transaction["committed"]:
                try:
                    self._recover_staged_foundation_rollback(foundation_transaction)
                except Exception as caught:
                    recovery_errors.append(caught)
            else:
                self._discard_foundation_stages(foundation_stages)
            for current_dashboard in reversed(current_dashboards):
                try:
                    self.ha.restore_dashboard(current_dashboard)
                except Exception as caught:
                    recovery_errors.append(caught)
            if not component_transaction["committed"]:
                try:
                    self.installer.discard_staged_rollback(component_transaction)
                except Exception as caught:
                    recovery_errors.append(caught)
            if recovery_errors:
                raise ProductUpdateError(
                    "Product rollback failed and current-state recovery failed"
                ) from recovery_errors[0]
            raise
        return {
            "operation": "product_rollback",
            "dashboard": dashboards[-1],
            "dashboards": list(reversed(dashboards)),
            "foundation_files_restored": foundation_restored,
            "files": files,
            "readback_verified": all(
                bool(dashboard.get("readback_verified")) for dashboard in dashboards
            ),
        }