"""Verified installer and deterministic rollback for the Van Gogh 2 app."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

PRODUCT = "van-gogh2"
COMPONENT = "van_gogh2"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class InstallError(RuntimeError):
    """Raised when a release cannot be safely installed or rolled back."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_member(name: str) -> Path:
    cleaned = name.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    path = Path(cleaned)
    if not cleaned or path.is_absolute() or ".." in path.parts:
        raise InstallError(f"Unsafe archive member: {name!r}")
    return path


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract regular files/directories only, beneath destination."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        total = 0
        for member in members:
            relative = _normalise_member(member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise InstallError(f"Unsupported archive member type: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise InstallError(f"Unsupported archive member: {member.name!r}")
            total += max(0, member.size)
            if total > MAX_ARCHIVE_BYTES:
                raise InstallError("Archive expands beyond the 100 MiB safety limit")
            target = destination / relative
            target_parent = target if member.isdir() else target.parent
            target_parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = package.extractfile(member)
            if source is None:
                raise InstallError(f"Archive member could not be read: {member.name!r}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777 or 0o644)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class ReleaseClient:
    def __init__(self, endpoint: str, token: str) -> None:
        endpoint = endpoint.strip().rstrip("/")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InstallError("Release endpoint must be an http(s) URL")
        if not token:
            raise InstallError("Release token is required")
        self.endpoint = endpoint
        self.token = token

    def _request(self, path_or_url: str) -> urllib.request.Request:
        url = urllib.parse.urljoin(self.endpoint + "/", path_or_url)
        return urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )

    def latest(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self._request("api/v1/van-gogh2/latest.json"), timeout=20) as response:
                if response.status != 200:
                    raise InstallError(f"Release endpoint returned HTTP {response.status}")
                raw = response.read(64 * 1024)
        except OSError as error:
            raise InstallError(f"Release endpoint request failed: {error}") from error
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InstallError("Release endpoint returned invalid JSON") from error
        if isinstance(manifest, dict) and "archive" not in manifest and isinstance(manifest.get("url"), str):
            manifest["archive"] = manifest["url"]
        required = {"product", "version", "sha256", "archive"}
        if not isinstance(manifest, dict) or not required.issubset(manifest):
            raise InstallError("Release manifest is incomplete")
        if manifest["product"] != PRODUCT:
            raise InstallError("Release manifest product is not van-gogh2")
        if not isinstance(manifest["sha256"], str) or len(manifest["sha256"]) != 64:
            raise InstallError("Release manifest SHA-256 is invalid")
        result: dict[str, Any] = {key: str(manifest[key]) for key in required}
        if isinstance(manifest.get("url"), str):
            result["url"] = manifest["url"]
        if isinstance(manifest.get("supported_home_assistant"), dict):
            result["supported_home_assistant"] = manifest["supported_home_assistant"]
        return result

    def download(self, manifest: dict[str, Any], destination: Path) -> None:
        request = self._request(manifest["archive"])
        request.headers["Accept"] = "application/gzip"
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                if response.status != 200:
                    raise InstallError(f"Archive endpoint returned HTTP {response.status}")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise InstallError("Downloaded archive exceeds 100 MiB")
                    output.write(chunk)
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise InstallError(f"Release download failed: {error}") from error
        actual = sha256_file(destination)
        if actual != manifest["sha256"]:
            destination.unlink(missing_ok=True)
            raise InstallError("Downloaded archive SHA-256 does not match the release manifest")


class InstallManager:
    def __init__(self, config_root: Path) -> None:
        self.config_root = config_root
        self.target = config_root / "custom_components" / COMPONENT
        self.state_root = config_root / "van_gogh2_installer"
        self.backups = self.state_root / "backups"
        self.current_receipt = self.state_root / "current.json"
        self.operations = self.state_root / "operations"

    def installed_version(self) -> str | None:
        manifest = self.target / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "invalid"
        value = payload.get("version")
        return str(value) if value is not None else "unknown"

    def _source_component(self, extracted: Path, expected_version: str) -> Path:
        source = extracted / "custom_components" / COMPONENT
        manifest_path = source / "manifest.json"
        if not manifest_path.is_file() or not (source / "__init__.py").is_file():
            raise InstallError("Archive does not contain the Van Gogh 2 integration")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise InstallError("Integration manifest is invalid JSON") from error
        if manifest.get("domain") != COMPONENT:
            raise InstallError("Integration manifest domain is not van_gogh2")
        if manifest.get("version") != expected_version:
            raise InstallError("Integration version does not match the release manifest")
        frontend = source / "frontend" / expected_version / "van-gogh2.js"
        if not frontend.is_file():
            raise InstallError("Integration does not contain the versioned frontend module")
        return source

    def install_archive(self, archive: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        actual_sha = sha256_file(archive)
        if actual_sha != manifest["sha256"]:
            raise InstallError("Archive SHA-256 does not match the release manifest")
        with tempfile.TemporaryDirectory(prefix="van-gogh2-extract-") as temporary:
            extracted = Path(temporary)
            safe_extract(archive, extracted)
            source = self._source_component(extracted, manifest["version"])
            return self._install_component(source, manifest, actual_sha)

    def _install_component(
        self, source: Path, manifest: dict[str, Any], archive_sha256: str
    ) -> dict[str, Any]:
        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_id = f"{now}-{uuid.uuid4().hex[:8]}"
        backup = self.backups / backup_id
        backup.mkdir(parents=True, exist_ok=False)
        previous_present = self.target.is_dir()
        if previous_present:
            shutil.copytree(self.target, backup / COMPONENT)
        previous_version = self.installed_version()
        backup_receipt = {
            "backup_id": backup_id,
            "created_utc": now,
            "previous_present": previous_present,
            "previous_version": previous_version,
        }
        _write_json_atomic(backup / "receipt.json", backup_receipt)

        self.target.parent.mkdir(parents=True, exist_ok=True)
        stage = self.target.parent / f".{COMPONENT}.new.{uuid.uuid4().hex}"
        displaced = self.target.parent / f".{COMPONENT}.old.{uuid.uuid4().hex}"
        shutil.copytree(source, stage)
        moved_old = False
        try:
            if self.target.exists():
                os.replace(self.target, displaced)
                moved_old = True
            os.replace(stage, self.target)
        except Exception:
            if self.target.exists():
                shutil.rmtree(self.target, ignore_errors=True)
            if moved_old and displaced.exists():
                os.replace(displaced, self.target)
            shutil.rmtree(stage, ignore_errors=True)
            raise
        shutil.rmtree(displaced, ignore_errors=True)

        receipt = {
            **backup_receipt,
            "operation": "install",
            "product": PRODUCT,
            "installed_version": manifest["version"],
            "archive_sha256": archive_sha256,
            "restart_required": True,
            "target": str(self.target),
        }
        _write_json_atomic(self.current_receipt, receipt)
        _write_json_atomic(self.operations / f"{now}-install.json", receipt)
        return receipt

    def annotate_current(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Persist post-copy prestate/readiness without weakening the original receipt."""
        if not self.current_receipt.is_file():
            raise InstallError("No current install receipt is available to annotate")
        receipt = json.loads(self.current_receipt.read_text(encoding="utf-8"))
        receipt.update(fields)
        _write_json_atomic(self.current_receipt, receipt)
        backup_id = receipt.get("backup_id")
        if isinstance(backup_id, str) and backup_id:
            backup_receipt = self.backups / backup_id / "receipt.json"
            if backup_receipt.is_file():
                backup_payload = json.loads(backup_receipt.read_text(encoding="utf-8"))
                if "ha_prestate" in fields:
                    backup_payload["ha_prestate"] = fields["ha_prestate"]
                _write_json_atomic(backup_receipt, backup_payload)
        created = receipt.get("created_utc")
        if isinstance(created, str) and created:
            _write_json_atomic(self.operations / f"{created}-install.json", receipt)
        return receipt

    def rollback(self) -> dict[str, Any]:
        if not self.current_receipt.is_file():
            raise InstallError("No successful install receipt is available to roll back")
        current = json.loads(self.current_receipt.read_text(encoding="utf-8"))
        backup_id = current.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id:
            raise InstallError("Current install receipt has no backup identifier")
        backup = self.backups / backup_id
        backup_receipt_path = backup / "receipt.json"
        if not backup_receipt_path.is_file():
            raise InstallError("Rollback backup receipt is missing")
        backup_receipt = json.loads(backup_receipt_path.read_text(encoding="utf-8"))
        previous_present = bool(backup_receipt.get("previous_present"))
        source = backup / COMPONENT
        if previous_present and not source.is_dir():
            raise InstallError("Rollback integration backup is missing")

        self.target.parent.mkdir(parents=True, exist_ok=True)
        stage = self.target.parent / f".{COMPONENT}.rollback.{uuid.uuid4().hex}"
        displaced = self.target.parent / f".{COMPONENT}.before-rollback.{uuid.uuid4().hex}"
        if previous_present:
            shutil.copytree(source, stage)
        moved_current = False
        try:
            if self.target.exists():
                os.replace(self.target, displaced)
                moved_current = True
            if previous_present:
                os.replace(stage, self.target)
        except Exception:
            if self.target.exists():
                shutil.rmtree(self.target, ignore_errors=True)
            if moved_current and displaced.exists():
                os.replace(displaced, self.target)
            shutil.rmtree(stage, ignore_errors=True)
            raise
        shutil.rmtree(displaced, ignore_errors=True)

        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        receipt = {
            "operation": "rollback",
            "rolled_back_backup_id": backup_id,
            "completed_utc": now,
            "restored_present": previous_present,
            "restored_version": backup_receipt.get("previous_version"),
            "restart_required": True,
            "target": str(self.target),
        }
        _write_json_atomic(self.operations / f"{now}-rollback.json", receipt)
        self.current_receipt.unlink(missing_ok=True)
        return receipt
