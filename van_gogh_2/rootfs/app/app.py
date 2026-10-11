"""Ingress web application for Van Gogh 2 install/update/rollback."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ha_client import HAClient, HAError, HARestartPending, module_url_for_release
from installer import InstallError, InstallManager, ReleaseClient
from product_update import ProductUpdateError, ProductUpdateManager

APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("VAN_GOGH_DATA_ROOT", "/data"))
CONFIG_ROOT = Path(os.environ.get("VAN_GOGH_CONFIG_ROOT", "/homeassistant"))
SETTINGS = DATA_ROOT / "settings.json"
OPERATION_RECEIPT = DATA_ROOT / "operation.json"
SITE_MANIFEST = CONFIG_ROOT / "van_gogh2-site.json"
INSTALL_LOCK = threading.Lock()
OPERATION_LOCK = threading.Lock()
DEFAULT_RELEASE_ENDPOINT = os.environ.get(
    "VAN_GOGH_RELEASE_ENDPOINT", "https://installer.mtae.com.au"
).rstrip("/")
INSTALLER_VERSION = "1.2.2"
PRODUCT_PACKAGE_SCHEMA = "mtae.van-gogh.product-package.v1"


def _load_operation_state() -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "id": 0,
        "operation": None,
        "stage": "idle",
        "state": "idle",
        "message": None,
        "error": None,
    }
    try:
        saved = json.loads(OPERATION_RECEIPT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(saved, dict) or not isinstance(saved.get("id"), int):
        return fallback
    return {**fallback, **{key: saved.get(key) for key in fallback}}


OPERATION_STATE: dict[str, Any] = _load_operation_state()


def _persist_operation_locked() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = OPERATION_RECEIPT.with_name(".operation.tmp")
    temporary.write_text(json.dumps(OPERATION_STATE, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, OPERATION_RECEIPT)


def _operation_begin(operation: str, stage: str) -> int:
    with OPERATION_LOCK:
        operation_id = int(OPERATION_STATE["id"]) + 1
        OPERATION_STATE.update({
            "id": operation_id,
            "operation": operation,
            "stage": stage,
            "state": "running",
            "message": None,
            "error": None,
        })
        _persist_operation_locked()
        return operation_id


def _operation_update(
    operation_id: int,
    stage: str,
    state: str = "running",
    *,
    message: str | None = None,
    error: str | None = None,
) -> None:
    with OPERATION_LOCK:
        if OPERATION_STATE["id"] == operation_id:
            OPERATION_STATE.update({
                "stage": stage,
                "state": state,
                "message": message,
                "error": error,
            })
            _persist_operation_locked()


def _operation_snapshot() -> dict[str, Any]:
    with OPERATION_LOCK:
        return dict(OPERATION_STATE)


def _atomic_settings(payload: dict[str, Any]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS.with_name(".settings.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, SETTINGS)


def _site_manifest_profile() -> str | None:
    """Return the protected existing profile, failing before code exchange if invalid."""
    if not SITE_MANIFEST.is_file():
        return None
    try:
        current = json.loads(SITE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("Existing canonical site manifest is not valid JSON") from error
    profile = current.get("panel", {}).get("profile") if isinstance(current, dict) else None
    if profile not in {"wall-10", "mobile/app"}:
        raise InstallError("Existing canonical site manifest has an unsupported profile")
    return str(profile)


def _commit_activation(activated: dict[str, Any]) -> str:
    """Persist activation and create a neutral manifest only when none exists."""
    supplied_manifest = activated.get("site_manifest")
    if not isinstance(supplied_manifest, dict):
        raise InstallError("Activation did not return a neutral starter manifest")
    site = supplied_manifest.get("site")
    panel = supplied_manifest.get("panel")
    if (
        not isinstance(site, dict)
        or not isinstance(site.get("id"), str)
        or not isinstance(panel, dict)
        or panel.get("profile") not in {"wall-10", "mobile/app"}
    ):
        raise InstallError("Activation returned an invalid neutral starter manifest")
    settings_payload = {key: value for key, value in activated.items() if key != "site_manifest"}
    before_settings = SETTINGS.read_bytes() if SETTINGS.is_file() else None
    created_manifest = not SITE_MANIFEST.exists()
    try:
        _atomic_settings(settings_payload)
        if created_manifest:
            CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
            temporary = SITE_MANIFEST.with_name(".van_gogh2-site.tmp")
            temporary.write_text(
                json.dumps(supplied_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, SITE_MANIFEST)
    except OSError as error:
        if before_settings is None:
            SETTINGS.unlink(missing_ok=True)
        else:
            SETTINGS.write_bytes(before_settings)
            os.chmod(SETTINGS, 0o600)
        if created_manifest:
            SITE_MANIFEST.unlink(missing_ok=True)
        raise InstallError("Activation state could not be persisted safely") from error
    return "created_neutral" if created_manifest else "preserved_existing"


def _validate_product_release_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Accept a compatible package identity; package contents enforce the full policy."""
    if manifest.get("product") != "van-gogh2":
        raise InstallError("Product release identity is not van-gogh2")
    if manifest.get("package_schema") != PRODUCT_PACKAGE_SCHEMA:
        raise InstallError("Product release package schema is unsupported")
    if not isinstance(manifest.get("package_version"), str):
        raise InstallError("Product release package version is missing")
    foundation_version = manifest.get("foundation_version")
    if not isinstance(foundation_version, str) or not foundation_version.strip():
        raise InstallError("Product release Foundation version is missing or invalid")
    if manifest.get("component_version") != manifest.get("version"):
        raise InstallError("Product release component identity is inconsistent")
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise InstallError("Product release SHA-256 is invalid")
    try:
        bytes.fromhex(digest)
    except ValueError as error:
        raise InstallError("Product release SHA-256 is invalid") from error
    module_url = manifest.get("module_url")
    if module_url is None:
        module_url = module_url_for_release(str(manifest["version"]))
    if not isinstance(module_url, str) or re.fullmatch(
        r"/local/van-gogh2/[0-9A-Za-z][0-9A-Za-z.-]{0,127}/van-gogh2\.js",
        module_url,
    ) is None:
        raise InstallError("Product release frontend module URL is invalid")
    manifest["module_url"] = module_url
    return manifest


def _settings() -> dict[str, Any]:
    if not SETTINGS.is_file():
        return {}
    try:
        value = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _release_client() -> ReleaseClient:
    current = _settings()
    credential = str(current.get("credential") or current.get("token") or "")
    return ReleaseClient(DEFAULT_RELEASE_ENDPOINT, credential)


def _status() -> dict[str, Any]:
    manager = InstallManager(CONFIG_ROOT)
    current = _settings()
    receipt: dict[str, Any] | None = None
    if manager.current_receipt.is_file():
        try:
            receipt = json.loads(manager.current_receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {"state": "invalid"}
    return {
        "application": "MTAE Van Gogh Installer",
        "installed_version": manager.installed_version(),
        "release_endpoint_configured": True,
        "site_credential_configured": bool(current.get("credential") or current.get("token")),
        "site_id": current.get("site_id"),
        "site_name": current.get("site_name"),
        "home_assistant_api_available": HAClient.from_environment() is not None,
        "restart_required": bool(receipt and receipt.get("restart_required")),
        "last_install": receipt,
        "operation": _operation_snapshot(),
        "installer_version": INSTALLER_VERSION,
        "product_package_schema": PRODUCT_PACKAGE_SCHEMA,
        "site_manifest_path": str(SITE_MANIFEST),
        "site_manifest_available": SITE_MANIFEST.is_file(),
    }


def _restart_home_assistant(progress: Any = None) -> dict[str, Any]:
    client = HAClient.from_environment()
    if client is None:
        raise InstallError("Supervisor API token is unavailable")
    try:
        cycle = client.restart_and_wait_ready(progress=progress)
    except HARestartPending:
        raise
    except HAError as error:
        raise InstallError(str(error)) from error
    return {"restart_completed": True, "restart_cycle": cycle}


class Handler(BaseHTTPRequestHandler):
    server_version = "VanGogh2App/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if content_type.startswith("text/html") else "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise InstallError("Invalid request length") from error
        limit = 64 * 1024
        if length < 0 or length > limit:
            raise InstallError(f"Request body exceeds {limit // 1024} KiB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InstallError("Request body is not valid JSON") from error
        if not isinstance(payload, dict):
            raise InstallError("Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._file(APP_ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/static/mtae-installer.png":
            self._file(APP_ROOT / "static" / "mtae-installer.png", "image/png")
        elif path == "/static/van-gogh-2.png":
            self._file(APP_ROOT / "static" / "van-gogh-2.png", "image/png")
        elif path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/api/status":
            self._json(HTTPStatus.OK, _status())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        operation_id: int | None = None
        try:
            payload = self._body()
            if _operation_snapshot().get("state") == "pending":
                raise InstallError(
                    "A restart cycle is still pending; no second operation will be submitted"
                )
            if path == "/api/activate":
                operation_id = _operation_begin("activation", "activating")
                install_code = str(payload.get("install_code", ""))
                channel = str(payload.get("channel", "production"))
                profile = _site_manifest_profile() or str(payload.get("profile", "wall-10"))
                activated = ReleaseClient.activate(
                    DEFAULT_RELEASE_ENDPOINT,
                    install_code,
                    channel,
                    profile,
                )
                starter_manifest_state = _commit_activation(activated)
                result: dict[str, Any] = {
                    "activated": True,
                    "site_id": activated["site_id"],
                    "site_name": activated["site_name"],
                    "channel": activated["channel"],
                    "profile": activated["profile"],
                    "starter_manifest": starter_manifest_state,
                    "credential_returned": False,
                    "install_code_stored": False,
                }
                _operation_update(
                    operation_id,
                    "activated",
                    "complete",
                    message=(
                        "Code accepted — this Home Assistant is activated as "
                        f"{activated['site_name']}"
                    ),
                )
            elif path == "/api/check":
                operation_id = _operation_begin("release_check", "checking_release")
                manifest = _validate_product_release_manifest(_release_client().latest())
                result = {"release": manifest, "credential_returned": False}
                _operation_update(
                    operation_id,
                    "complete",
                    "complete",
                    message="Release checked",
                )
            elif path == "/api/install":
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another install or rollback is already running")
                try:
                    operation_id = _operation_begin("install", "checking_release")
                    client = _release_client()
                    manifest = _validate_product_release_manifest(client.latest())
                    module_url_for_release(manifest["version"])
                    manager = InstallManager(CONFIG_ROOT)
                    ha = HAClient.from_environment()
                    if ha is None:
                        raise InstallError("Supervisor API token is unavailable")
                    if not SITE_MANIFEST.is_file():
                        raise InstallError(f"Local site manifest is missing: {SITE_MANIFEST}")
                    core = ha.wait_ready()
                    core_version = str(core.get("version") or "")
                    prestate = ha.snapshot()
                    _operation_update(operation_id, "downloading_release")
                    with tempfile.TemporaryDirectory(prefix="van-gogh2-download-") as temporary:
                        archive = Path(temporary) / "release.tar.gz"
                        client.download(manifest, archive)
                        _operation_update(operation_id, "applying_product_update")
                        updater = ProductUpdateManager(manager, ha, INSTALLER_VERSION)
                        product_result = updater.apply(
                            archive,
                            manifest,
                            SITE_MANIFEST,
                            core_version,
                            defer_commit=True,
                        )
                    receipt: dict[str, Any] | None = None
                    if product_result["file_writes"]:
                        restart_requested = False
                        try:
                            receipt = manager.annotate_current({"ha_prestate": prestate})
                            _operation_update(operation_id, "requesting_restart")
                            restart_requested = True
                            restart_cycle = ha.restart_and_wait_ready(
                                progress=lambda stage: _operation_update(operation_id, stage)
                            )
                            _operation_update(operation_id, "verifying_readiness")
                            readiness = ha.finish_setup(manifest["module_url"])
                            readiness["restart_cycle"] = restart_cycle
                            receipt = manager.annotate_current({
                                "readiness": readiness,
                                "restart_required": False,
                            })
                            updater.commit_pending_apply()
                        except HARestartPending:
                            updater.commit_pending_apply()
                            raise
                        except Exception as error:
                            try:
                                _operation_update(operation_id, "automatic_rollback")
                                updater.recover_pending_apply()
                                if restart_requested:
                                    ha.restart_and_wait_ready(
                                        progress=lambda stage: _operation_update(operation_id, stage)
                                    )
                                ha.restore_prestate(prestate)
                            except HARestartPending as pending:
                                raise HARestartPending(
                                    "Install finalisation failed; files were restored but the rollback "
                                    f"restart remains pending: {pending}"
                                ) from pending
                            except Exception as rollback_error:
                                raise InstallError(
                                    "Install finalisation failed and automatic rollback failed "
                                    f"({type(rollback_error).__name__})"
                                ) from rollback_error
                            raise InstallError(
                                f"Install finalisation failed; automatic rollback completed: {error}"
                            ) from error
                    elif manager.current_receipt.is_file():
                        receipt = json.loads(manager.current_receipt.read_text(encoding="utf-8"))
                    result = {
                        "installed": True,
                        "product_update": product_result,
                        "receipt": receipt,
                    }
                    _operation_update(
                        operation_id,
                        "complete",
                        "complete",
                        message=(
                            "Product package, local configuration and readiness verified"
                            if product_result["operation"] != "noop"
                            else "Product package and local configuration are already current"
                        ),
                    )
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/rollback":
                if payload.get("confirm") != "rollback":
                    raise InstallError("Rollback requires confirm=rollback")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another install or rollback is already running")
                try:
                    operation_id = _operation_begin("rollback", "restoring_files")
                    manager = InstallManager(CONFIG_ROOT)
                    if not manager.current_receipt.is_file():
                        raise InstallError("No successful install receipt is available to roll back")
                    current = json.loads(manager.current_receipt.read_text(encoding="utf-8"))
                    prestate = current.get("ha_prestate")
                    ha = HAClient.from_environment()
                    if ha is not None and isinstance(current.get("dashboard_prestate"), dict):
                        product_rollback = ProductUpdateManager(
                            manager,
                            ha,
                            INSTALLER_VERSION,
                        ).rollback()
                        receipt = product_rollback["files"]
                        receipt["dashboard"] = product_rollback["dashboard"]
                    else:
                        receipt = manager.rollback()
                    if ha is not None and isinstance(prestate, dict):
                        try:
                            _operation_update(operation_id, "requesting_restart")
                            restart_cycle = ha.restart_and_wait_ready(
                                progress=lambda stage: _operation_update(operation_id, stage)
                            )
                            _operation_update(operation_id, "restoring_home_assistant")
                            receipt["home_assistant"] = ha.restore_prestate(prestate)
                            receipt["restart_cycle"] = restart_cycle
                            receipt["restart_required"] = False
                        except HARestartPending:
                            raise
                        except HAError as error:
                            raise InstallError(f"File rollback completed but Home Assistant readback failed: {error}") from error
                    result = {"rolled_back": True, "receipt": receipt}
                    _operation_update(
                        operation_id,
                        "complete",
                        "complete",
                        message="Rolled back safely",
                    )
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/restart":
                if payload.get("confirm") != "restart":
                    raise InstallError("Restart requires confirm=restart")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another install, rollback or restart is already running")
                try:
                    operation_id = _operation_begin("restart", "requesting_restart")
                    result = _restart_home_assistant(
                        progress=lambda stage: _operation_update(operation_id, stage)
                    )
                    _operation_update(
                        operation_id,
                        "complete",
                        "complete",
                        message="Restart completed and verified",
                    )
                finally:
                    INSTALL_LOCK.release()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(HTTPStatus.OK, result)
        except (InstallError, ProductUpdateError, HAError) as error:
            if operation_id is not None:
                if isinstance(error, HARestartPending):
                    _operation_update(
                        operation_id,
                        "restart_pending",
                        "pending",
                        error=str(error),
                    )
                    self._json(HTTPStatus.CONFLICT, {"error": str(error), "pending": True})
                    return
                _operation_update(operation_id, "failed", "error", error=str(error))
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # fail closed without traceback/secret material in HTTP
            if operation_id is not None:
                _operation_update(
                    operation_id,
                    "failed",
                    "error",
                    error="Internal operation failure",
                )
            print(f"Request failed: {type(error).__name__}: {error}", flush=True)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal operation failure"})


def main() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("VAN_GOGH_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Van Gogh 2 app listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
