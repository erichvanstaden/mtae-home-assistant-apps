"""Ingress web application for Van Gogh 2 install/update/rollback."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ha_client import HAClient, HADashboardWriteError, HAError, HARestartPending, module_url_for_release
from installer import InstallError, InstallManager, ReleaseClient
from dashboard_migration import MigrationError, discover_dashboard

APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("VAN_GOGH_DATA_ROOT", "/data"))
CONFIG_ROOT = Path(os.environ.get("VAN_GOGH_CONFIG_ROOT", "/homeassistant"))
SETTINGS = DATA_ROOT / "settings.json"
OPERATION_RECEIPT = DATA_ROOT / "operation.json"
REVIEW_DASHBOARD_PRESTATE = DATA_ROOT / "review-dashboard-prestate.json"
HOME_MIGRATION_PRESTATE = DATA_ROOT / "home-c-grid-migration-prestate.json"
INSTALL_LOCK = threading.Lock()
OPERATION_LOCK = threading.Lock()
DEFAULT_RELEASE_ENDPOINT = os.environ.get(
    "VAN_GOGH_RELEASE_ENDPOINT", "https://installer.mtae.com.au"
).rstrip("/")
EXPECTED_RELEASE = "2.0.0-staging.10"
EXPECTED_RELEASE_SHA256 = "b86ffe4cb6f5fae82b16faedbb2e1f2ce2ca3b4c617c29e237b42f1c0063c626"
EXPECTED_HOME_ASSISTANT = "2026.8.3"
MIGRATION_HOME_ASSISTANT = "2026.9.3"
REVIEW_DASHBOARD_PATH = "van-gogh-c-grid-review"


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


def _atomic_settings(payload: dict[str, str]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS.with_name(".settings.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, SETTINGS)


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _validate_release_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    supported = manifest.get("supported_home_assistant")
    if manifest.get("version") != EXPECTED_RELEASE:
        raise InstallError(f"Installer 1.1.17 requires Van Gogh {EXPECTED_RELEASE}")
    if manifest.get("sha256") != EXPECTED_RELEASE_SHA256:
        raise InstallError("Van Gogh staging.10 archive identity did not match Installer 1.1.17")
    if not isinstance(supported, dict) or supported.get("minimum") != EXPECTED_HOME_ASSISTANT or supported.get("tested") != EXPECTED_HOME_ASSISTANT:
        raise InstallError(f"Installer 1.1.17 requires Home Assistant {EXPECTED_HOME_ASSISTANT} release metadata")
    return manifest


def _review_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = payload.get("config")
    if not isinstance(config, dict):
        raise InstallError("Review dashboard JSON must contain a config object")
    if not isinstance(config.get("views"), list) or not config["views"]:
        raise InstallError("Review dashboard config must contain at least one view")
    return config


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _home_c_grid_migration() -> dict[str, Any]:
    """Discover and migrate one supported Van Gogh Home schema, or write nothing."""
    manager = InstallManager(CONFIG_ROOT)
    if manager.installed_version() != EXPECTED_RELEASE:
        raise InstallError(f"Install and verify Van Gogh {EXPECTED_RELEASE} before migrating Home")
    ha = HAClient.from_environment()
    if ha is None:
        raise InstallError("Supervisor API token is unavailable")
    core = ha.wait_ready()
    if core.get("version") != MIGRATION_HOME_ASSISTANT:
        raise InstallError(f"Home schema migration requires Home Assistant {MIGRATION_HOME_ASSISTANT}")
    try:
        plan = discover_dashboard(ha.storage_dashboard_inventory())
    except MigrationError as error:
        raise InstallError(str(error)) from error
    if not plan.changed:
        return {
            "migrated": False,
            "already_current": True,
            "dashboard_path": plan.url_path,
            "config_sha256": _json_sha256(plan.before),
            "dashboard_writes": 0,
            "readback_verified": True,
        }
    prestate = {
        "url_path": plan.url_path,
        "exists": True,
        "registry": plan.registry,
        "config": plan.before,
    }
    _atomic_private_json(HOME_MIGRATION_PRESTATE, prestate)
    try:
        result = ha.save_existing_dashboard(plan.url_path, plan.before, plan.after)
    except HADashboardWriteError:
        ha.restore_dashboard(prestate)
        raise
    try:
        verified = discover_dashboard(ha.storage_dashboard_inventory())
        if verified.url_path != plan.url_path or verified.state != "current" or verified.before != plan.after:
            raise HAError("Home schema migration post-change discovery did not match the exact candidate")
    except Exception:
        ha.restore_dashboard(prestate)
        raise
    return {
        "migrated": True,
        "already_current": False,
        "dashboard_path": plan.url_path,
        "before_sha256": _json_sha256(plan.before),
        "config_sha256": _json_sha256(plan.after),
        **result,
    }


def _settings() -> dict[str, str]:
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
        "supported_release": EXPECTED_RELEASE,
        "supported_home_assistant": EXPECTED_HOME_ASSISTANT,
        "home_schema_migration_home_assistant": MIGRATION_HOME_ASSISTANT,
        "home_schema_migration_prestate_available": HOME_MIGRATION_PRESTATE.is_file(),
        "review_dashboard_path": REVIEW_DASHBOARD_PATH,
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
        limit = 1024 * 1024 if self.path.split("?", 1)[0] == "/api/review-dashboard" else 64 * 1024
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
                activated = ReleaseClient.activate(DEFAULT_RELEASE_ENDPOINT, install_code)
                _atomic_settings(activated)
                result: dict[str, Any] = {
                    "activated": True,
                    "site_id": activated["site_id"],
                    "site_name": activated["site_name"],
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
                manifest = _validate_release_manifest(_release_client().latest())
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
                    manifest = _validate_release_manifest(client.latest())
                    module_url_for_release(manifest["version"])
                    manager = InstallManager(CONFIG_ROOT)
                    ha = HAClient.from_environment()
                    prestate = ha.snapshot() if ha is not None else None
                    _operation_update(operation_id, "downloading_release")
                    with tempfile.TemporaryDirectory(prefix="van-gogh2-download-") as temporary:
                        archive = Path(temporary) / "release.tar.gz"
                        client.download(manifest, archive)
                        _operation_update(operation_id, "installing_files")
                        receipt = manager.install_archive(archive, manifest)
                    if ha is not None and prestate is not None:
                        receipt = manager.annotate_current({"ha_prestate": prestate})
                        try:
                            _operation_update(operation_id, "requesting_restart")
                            restart_cycle = ha.restart_and_wait_ready(
                                progress=lambda stage: _operation_update(operation_id, stage)
                            )
                            _operation_update(operation_id, "verifying_readiness")
                            readiness = ha.finish_setup(manifest["version"])
                            readiness["restart_cycle"] = restart_cycle
                            receipt = manager.annotate_current({
                                "readiness": readiness,
                                "restart_required": False,
                            })
                        except HARestartPending:
                            raise
                        except HAError as error:
                            try:
                                _operation_update(operation_id, "automatic_rollback")
                                manager.rollback()
                                ha.restart_and_wait_ready(
                                    progress=lambda stage: _operation_update(operation_id, stage)
                                )
                                ha.restore_prestate(prestate)
                            except HARestartPending as pending:
                                raise HARestartPending(
                                    "Install readiness failed; files were restored but the rollback "
                                    f"restart remains pending: {pending}"
                                ) from pending
                            except Exception as rollback_error:
                                raise InstallError(
                                    f"Install readiness failed and automatic rollback failed ({type(rollback_error).__name__})"
                                ) from rollback_error
                            raise InstallError(
                                f"Install readiness failed; automatic rollback completed: {error}"
                            ) from error
                    result = {"installed": True, "receipt": receipt}
                    _operation_update(
                        operation_id,
                        "complete",
                        "complete",
                        message=(
                            "Installed and verified"
                            if ha is not None
                            else "Installed; Home Assistant restart required"
                        ),
                    )
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/home-schema/migrate":
                if payload.get("confirm") != "migrate":
                    raise InstallError("Home schema migration requires confirm=migrate")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another installer operation is already running")
                try:
                    operation_id = _operation_begin("home_schema_migration", "discovering_home_schema")
                    result = _home_c_grid_migration()
                    _operation_update(
                        operation_id,
                        "complete",
                        "complete",
                        message=(
                            "Home C-grid schema migrated and verified"
                            if result["migrated"]
                            else "Home C-grid schema is already current"
                        ),
                    )
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/home-schema/rollback":
                if payload.get("confirm") != "rollback":
                    raise InstallError("Home schema rollback requires confirm=rollback")
                if not HOME_MIGRATION_PRESTATE.is_file():
                    raise InstallError("No Home schema migration prestate is available")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another installer operation is already running")
                try:
                    operation_id = _operation_begin("home_schema_rollback", "restoring_home_schema")
                    ha = HAClient.from_environment()
                    if ha is None:
                        raise InstallError("Supervisor API token is unavailable")
                    prestate = json.loads(HOME_MIGRATION_PRESTATE.read_text(encoding="utf-8"))
                    result = {"home_schema": ha.restore_dashboard(prestate), "dashboard_writes": 1}
                    _operation_update(operation_id, "complete", "complete", message="Home schema prestate restored")
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/review-dashboard":
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another installer operation is already running")
                try:
                    operation_id = _operation_begin("review_dashboard", "saving_review_dashboard")
                    manager = InstallManager(CONFIG_ROOT)
                    if manager.installed_version() != EXPECTED_RELEASE:
                        raise InstallError(f"Install and verify Van Gogh {EXPECTED_RELEASE} before refreshing the review dashboard")
                    ha = HAClient.from_environment()
                    if ha is None:
                        raise InstallError("Supervisor API token is unavailable")
                    core = ha.wait_ready()
                    if core.get("version") != EXPECTED_HOME_ASSISTANT:
                        raise InstallError(f"Review dashboard refresh requires Home Assistant {EXPECTED_HOME_ASSISTANT}")
                    prestate = ha.dashboard_snapshot(REVIEW_DASHBOARD_PATH)
                    _atomic_private_json(REVIEW_DASHBOARD_PRESTATE, prestate)
                    try:
                        dashboard = ha.save_review_dashboard(
                            REVIEW_DASHBOARD_PATH,
                            _review_config(payload),
                            title="Van Gogh Family staging.8 review",
                        )
                    except Exception:
                        ha.restore_dashboard(prestate)
                        raise
                    result = {"review_dashboard": dashboard, "owner_route": f"/{REVIEW_DASHBOARD_PATH}/home"}
                    _operation_update(operation_id, "complete", "complete", message="Family review dashboard refreshed and verified")
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/review-dashboard/rollback":
                if payload.get("confirm") != "rollback":
                    raise InstallError("Review dashboard rollback requires confirm=rollback")
                if not REVIEW_DASHBOARD_PRESTATE.is_file():
                    raise InstallError("No review dashboard prestate is available")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another installer operation is already running")
                try:
                    operation_id = _operation_begin("review_dashboard_rollback", "restoring_review_dashboard")
                    ha = HAClient.from_environment()
                    if ha is None:
                        raise InstallError("Supervisor API token is unavailable")
                    prestate = json.loads(REVIEW_DASHBOARD_PRESTATE.read_text(encoding="utf-8"))
                    result = {"review_dashboard": ha.restore_dashboard(prestate)}
                    _operation_update(operation_id, "complete", "complete", message="Family review dashboard restored")
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
                    receipt = manager.rollback()
                    ha = HAClient.from_environment()
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
        except (InstallError, HAError) as error:
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
