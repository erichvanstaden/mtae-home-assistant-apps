"""Ingress web application for Van Gogh 2 install/update/rollback."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ha_client import HAClient, HAError
from installer import InstallError, InstallManager, ReleaseClient

APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("VAN_GOGH_DATA_ROOT", "/data"))
CONFIG_ROOT = Path(os.environ.get("VAN_GOGH_CONFIG_ROOT", "/homeassistant"))
SETTINGS = DATA_ROOT / "settings.json"
INSTALL_LOCK = threading.Lock()


def _atomic_settings(payload: dict[str, str]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS.with_name(".settings.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, SETTINGS)


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
    return ReleaseClient(str(current.get("endpoint", "")), str(current.get("token", "")))


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
        "release_endpoint_configured": bool(current.get("endpoint")),
        "release_token_configured": bool(current.get("token")),
        "home_assistant_api_available": HAClient.from_environment() is not None,
        "restart_required": bool(receipt and receipt.get("restart_required")),
        "last_install": receipt,
    }


def _restart_home_assistant() -> dict[str, Any]:
    client = HAClient.from_environment()
    if client is None:
        raise InstallError("Supervisor API token is unavailable")
    try:
        client.restart()
    except HAError as error:
        raise InstallError(str(error)) from error
    return {"restart_requested": True}


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
        if length < 0 or length > 64 * 1024:
            raise InstallError("Request body exceeds 64 KiB")
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
        try:
            payload = self._body()
            if path == "/api/settings":
                endpoint = str(payload.get("endpoint", "")).strip().rstrip("/")
                token = str(payload.get("token", ""))
                ReleaseClient(endpoint, token)
                if len(token) < 16:
                    raise InstallError("Release token must be at least 16 characters")
                _atomic_settings({"endpoint": endpoint, "token": token})
                result: dict[str, Any] = {"saved": True, "token_returned": False}
            elif path == "/api/check":
                manifest = _release_client().latest()
                result = {"release": manifest, "token_returned": False}
            elif path == "/api/install":
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another install or rollback is already running")
                try:
                    client = _release_client()
                    manifest = client.latest()
                    manager = InstallManager(CONFIG_ROOT)
                    ha = HAClient.from_environment()
                    prestate = ha.snapshot() if ha is not None else None
                    with tempfile.TemporaryDirectory(prefix="van-gogh2-download-") as temporary:
                        archive = Path(temporary) / "release.tar.gz"
                        client.download(manifest, archive)
                        receipt = manager.install_archive(archive, manifest)
                    if ha is not None and prestate is not None:
                        receipt = manager.annotate_current({"ha_prestate": prestate})
                        try:
                            ha.restart()
                            readiness = ha.finish_setup()
                            receipt = manager.annotate_current({
                                "readiness": readiness,
                                "restart_required": False,
                            })
                        except HAError as error:
                            try:
                                manager.rollback()
                                ha.restart()
                                ha.wait_ready()
                                ha.restore_prestate(prestate)
                            except Exception as rollback_error:
                                raise InstallError(
                                    f"Install readiness failed and automatic rollback failed ({type(rollback_error).__name__})"
                                ) from rollback_error
                            raise InstallError(
                                f"Install readiness failed; automatic rollback completed: {error}"
                            ) from error
                    result = {"installed": True, "receipt": receipt}
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/rollback":
                if payload.get("confirm") != "rollback":
                    raise InstallError("Rollback requires confirm=rollback")
                if not INSTALL_LOCK.acquire(blocking=False):
                    raise InstallError("Another install or rollback is already running")
                try:
                    manager = InstallManager(CONFIG_ROOT)
                    if not manager.current_receipt.is_file():
                        raise InstallError("No successful install receipt is available to roll back")
                    current = json.loads(manager.current_receipt.read_text(encoding="utf-8"))
                    prestate = current.get("ha_prestate")
                    receipt = manager.rollback()
                    ha = HAClient.from_environment()
                    if ha is not None and isinstance(prestate, dict):
                        try:
                            ha.restart()
                            ha.wait_ready()
                            receipt["home_assistant"] = ha.restore_prestate(prestate)
                            receipt["restart_required"] = False
                        except HAError as error:
                            raise InstallError(f"File rollback completed but Home Assistant readback failed: {error}") from error
                    result = {"rolled_back": True, "receipt": receipt}
                finally:
                    INSTALL_LOCK.release()
            elif path == "/api/restart":
                if payload.get("confirm") != "restart":
                    raise InstallError("Restart requires confirm=restart")
                result = _restart_home_assistant()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(HTTPStatus.OK, result)
        except (InstallError, HAError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # fail closed without traceback/secret material in HTTP
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
