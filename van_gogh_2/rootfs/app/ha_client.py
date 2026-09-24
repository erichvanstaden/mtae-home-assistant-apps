"""Bounded Home Assistant Core API/WebSocket operations for the installer app."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DOMAIN = "van_gogh2"
MODULE_URL = "/van-gogh2-assets/2.0.0-staging.1/van-gogh2.js"


class HAError(RuntimeError):
    """Safe operational failure without credentials in its text."""


class HARestartPending(HAError):
    """A submitted restart has not yet produced a complete observable cycle."""


def _is_van_gogh_resource(item: dict[str, Any]) -> bool:
    url = str(item.get("url", "")).split("?", 1)[0]
    return url.startswith("/van-gogh2-assets/") and url.endswith("/van-gogh2.js")


def _resource_type(item: dict[str, Any]) -> Any:
    """Normalise HA's response key (`type`) and command key (`res_type`)."""
    return item.get("res_type") or item.get("type")


def resource_plan(resources: list[dict[str, Any]], module_url: str = MODULE_URL) -> list[dict[str, Any]]:
    """Return deterministic create/update/delete actions leaving one exact module resource."""
    owned = [item for item in resources if _is_van_gogh_resource(item)]
    exact = [item for item in owned if item.get("url") == module_url and _resource_type(item) == "module"]
    actions: list[dict[str, Any]] = []
    keeper: dict[str, Any] | None = exact[0] if exact else (owned[0] if owned else None)
    if keeper is None:
        actions.append({"action": "create", "url": module_url, "res_type": "module"})
    elif keeper.get("url") != module_url or _resource_type(keeper) != "module":
        actions.append({
            "action": "update",
            "resource_id": keeper.get("id"),
            "url": module_url,
            "res_type": "module",
        })
    for item in owned:
        if item is keeper:
            continue
        actions.append({"action": "delete", "resource_id": item.get("id")})
    return actions


def restore_resource_plan(
    resources: list[dict[str, Any]], snapshot: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Restore resource semantics while retaining recorded resource IDs where possible."""
    available = [item for item in resources if _is_van_gogh_resource(item)]
    actions: list[dict[str, Any]] = []
    for desired in snapshot:
        desired_id = desired.get("id")
        keeper = next(
            (item for item in available if desired_id and item.get("id") == desired_id),
            available[0] if available else None,
        )
        if keeper is None:
            actions.append({
                "action": "create",
                "url": desired.get("url"),
                "res_type": _resource_type(desired) or "module",
            })
            continue
        available.remove(keeper)
        desired_type = _resource_type(desired) or "module"
        if keeper.get("url") != desired.get("url") or _resource_type(keeper) != desired_type:
            actions.append({
                "action": "update",
                "resource_id": keeper.get("id"),
                "url": desired.get("url"),
                "res_type": desired_type,
            })
    for item in available:
        actions.append({"action": "delete", "resource_id": item.get("id")})
    return actions


class WebSocket:
    """Small RFC 6455 text client sufficient for Home Assistant's JSON API."""

    def __init__(self, host: str, port: int, path: str, token: str, timeout: float = 15.0):
        self.host = host
        self.port = port
        self.path = path
        self.token = token
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.receive_buffer = bytearray()
        self.next_id = 1

    def __enter__(self) -> "WebSocket":
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 64 * 1024:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        header, separator, remainder = bytes(response).partition(b"\r\n\r\n")
        if not separator or not header.startswith(b"HTTP/1.1 101"):
            sock.close()
            raise HAError("Home Assistant WebSocket upgrade failed")
        headers: dict[str, str] = {}
        for line in header.decode("latin-1").split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.lower().strip()] = value.strip()
        if headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise HAError("Home Assistant WebSocket handshake was invalid")
        self.socket = sock
        self.receive_buffer.extend(remainder)
        hello = self.receive_json()
        if hello.get("type") != "auth_required":
            raise HAError("Home Assistant WebSocket did not request authentication")
        self.send_json({"type": "auth", "access_token": self.token})
        auth = self.receive_json()
        if auth.get("type") != "auth_ok":
            raise HAError("Home Assistant WebSocket authentication failed")
        return self

    def __exit__(self, *args: object) -> None:
        if self.socket is not None:
            try:
                self._send_frame(b"", opcode=8)
            except OSError:
                pass
            self.socket.close()
            self.socket = None

    def _read_exact(self, length: int) -> bytes:
        if self.socket is None:
            raise HAError("WebSocket is not connected")
        result = bytearray()
        if self.receive_buffer:
            buffered = min(length, len(self.receive_buffer))
            result.extend(self.receive_buffer[:buffered])
            del self.receive_buffer[:buffered]
        while len(result) < length:
            chunk = self.socket.recv(length - len(result))
            if not chunk:
                raise HAError("Home Assistant WebSocket closed unexpectedly")
            result.extend(chunk)
        return bytes(result)

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        if self.socket is None:
            raise HAError("WebSocket is not connected")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _receive_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > 4 * 1024 * 1024:
            raise HAError("Home Assistant WebSocket frame exceeds 4 MiB")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def receive_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._receive_frame()
            if opcode == 8:
                raise HAError("Home Assistant WebSocket closed")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode != 1:
                continue
            try:
                result = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HAError("Home Assistant WebSocket returned invalid JSON") from error
            if not isinstance(result, dict):
                raise HAError("Home Assistant WebSocket returned an invalid message")
            return result

    def call(self, command: dict[str, Any]) -> Any:
        message_id = self.next_id
        self.next_id += 1
        self.send_json({"id": message_id, **command})
        while True:
            message = self.receive_json()
            if message.get("id") != message_id:
                continue
            if not message.get("success"):
                code = (message.get("error") or {}).get("code", "unknown")
                raise HAError(f"Home Assistant WebSocket command failed ({code})")
            return message.get("result")


@dataclass
class HAClient:
    token: str
    host: str = "supervisor"
    port: int = 80

    @classmethod
    def from_environment(cls) -> "HAClient | None":
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        return cls(token=token, host=os.environ.get("SUPERVISOR_HOST", "supervisor")) if token else None

    @property
    def api_base(self) -> str:
        return f"http://{self.host}:{self.port}/core/api"

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.api_base + path,
            method=method,
            data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise HAError(f"Home Assistant API {path} returned HTTP {error.code}") from error
        except OSError as error:
            raise HAError(f"Home Assistant API {path} is unavailable") from error
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HAError(f"Home Assistant API {path} returned invalid JSON") from error

    def request_supervisor_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> Any:
        """Call the Supervisor API and unwrap its standard data envelope."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}",
            method=method,
            data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise HAError(f"Supervisor API {path} returned HTTP {error.code}") from error
        except OSError as error:
            raise HAError(f"Supervisor API {path} is unavailable") from error
        try:
            result = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HAError(f"Supervisor API {path} returned invalid JSON") from error
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return result["data"]
        return result

    def websocket(self) -> WebSocket:
        return WebSocket(self.host, self.port, "/core/websocket", self.token)

    def core_asset_origin(self) -> str:
        """Resolve the direct Core origin for browser assets from Supervisor facts."""
        info = self.request_supervisor_json("GET", "/core/info", timeout=8)
        address_value = info.get("ip_address") if isinstance(info, dict) else None
        port = info.get("port") if isinstance(info, dict) else None
        try:
            address = ipaddress.ip_address(address_value) if isinstance(address_value, str) else None
        except ValueError:
            address = None
        if address is None or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise HAError("Supervisor Core asset origin was invalid")
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        return f"http://{host}:{port}"

    def wait_ready(self, timeout: float = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "not ready"
        while time.monotonic() < deadline:
            try:
                config = self.request_json("GET", "/config", timeout=8)
                if isinstance(config, dict) and config.get("version"):
                    return config
            except HAError as error:
                last_error = str(error)
            time.sleep(2)
        raise HAError(f"Home Assistant Core did not become ready: {last_error}")

    def wait_stable_ready(
        self,
        timeout: float = 180,
        stable_reads: int = 2,
        poll_interval: float = 2,
    ) -> dict[str, Any]:
        """Require repeated matching API/version readback after Core returns."""
        deadline = time.monotonic() + timeout
        last_error = "not ready"
        last_version: str | None = None
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                config = self.request_json("GET", "/config", timeout=8)
                version = config.get("version") if isinstance(config, dict) else None
                if isinstance(version, str) and version:
                    consecutive = consecutive + 1 if version == last_version else 1
                    last_version = version
                    if consecutive >= stable_reads:
                        return config
                else:
                    last_error = "Home Assistant API returned no version"
                    consecutive = 0
                    last_version = None
            except HAError as error:
                last_error = str(error)
                consecutive = 0
                last_version = None
            time.sleep(poll_interval)
        raise HAError(f"Home Assistant Core did not become stably ready: {last_error}")

    def wait_supervisor_core_running(
        self,
        timeout: float = 180,
        poll_interval: float = 2,
    ) -> dict[str, Any]:
        """Require Supervisor Core identity and structurally valid live metrics.

        Current Supervisor releases omit ``state`` from ``/core/info``.  When a
        state is supplied it must still explicitly be RUNNING.
        """
        deadline = time.monotonic() + timeout
        last_state = "unavailable"
        while time.monotonic() < deadline:
            try:
                info = self.request_supervisor_json("GET", "/core/info", timeout=8)
                if not isinstance(info, dict):
                    last_state = "Core info was not an object"
                else:
                    state = info.get("state")
                    if state is not None and str(state).lower() != "running":
                        last_state = f"state {state}"
                    elif not all(
                        isinstance(info.get(key), str) and bool(info[key])
                        for key in ("version", "image", "machine", "arch")
                    ):
                        last_state = "Core info omitted version or identity"
                    else:
                        stats = self.request_supervisor_json("GET", "/core/stats", timeout=8)
                        required = (
                            ("online_cpus", 1, False),
                            ("memory_limit", 0, True),
                            ("memory_usage", 0, False),
                            ("cpu_percent", 0, False),
                        )
                        if not isinstance(stats, dict) or not all(
                            isinstance(stats.get(key), (int, float))
                            and not isinstance(stats[key], bool)
                            and math.isfinite(float(stats[key]))
                            and (stats[key] > minimum if strict else stats[key] >= minimum)
                            for key, minimum, strict in required
                        ):
                            last_state = "Core stats were not structurally valid live metrics"
                        else:
                            return {"core_info": info, "core_stats": stats}
            except HAError as error:
                last_state = str(error)
            time.sleep(poll_interval)
        raise HAError(f"Supervisor did not report Core RUNNING: {last_state}")

    def restart(self) -> dict[str, Any]:
        """Request a Core restart, retaining transport ambiguity for cycle verification.

        Supervisor can close the connection while carrying out an accepted restart.
        An explicit HTTP error is a rejection; a transport failure is not success by
        itself and must be followed by an observed down/up cycle.
        """
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/core/restart",
            method="POST",
            data=b"{}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status not in {200, 201}:
                    raise HAError(f"Home Assistant restart returned HTTP {response.status}")
                return {"request_state": "accepted", "http_status": response.status}
        except urllib.error.HTTPError as error:
            raise HAError(f"Home Assistant restart returned HTTP {error.code}") from error
        except OSError:
            return {"request_state": "transport_ambiguous", "http_status": None}

    def restart_and_wait_ready(
        self,
        down_timeout: float = 180,
        core_running_timeout: float = 180,
        ready_timeout: float = 180,
        poll_interval: float = 1,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Require a proven Core down→up cycle after requesting a restart."""
        request = self.restart()
        if progress is not None:
            progress("waiting_for_core_stop")
        down_deadline = time.monotonic() + down_timeout
        while time.monotonic() < down_deadline:
            try:
                self.request_json("GET", "/config", timeout=2)
            except HAError:
                break
            time.sleep(poll_interval)
        else:
            raise HARestartPending("Restart request did not observe Home Assistant Core stop")

        if progress is not None:
            progress("waiting_for_core_start")
        try:
            supervisor = self.wait_supervisor_core_running(
                timeout=core_running_timeout,
                poll_interval=poll_interval,
            )
            core_info = supervisor["core_info"]
            config = self.wait_stable_ready(
                timeout=ready_timeout,
                stable_reads=2,
                poll_interval=poll_interval,
            )
            supervisor_version = core_info.get("version")
            if supervisor_version and config.get("version") != supervisor_version:
                raise HARestartPending(
                    "Supervisor Core version and stable Home Assistant API version did not match"
                )
            inventory = self.entries()
        except HARestartPending:
            raise
        except HAError as error:
            raise HARestartPending(
                f"Home Assistant Core stopped but did not return to stable readiness: {error}"
            ) from error
        return {
            **request,
            "down_observed": True,
            "supervisor_state": core_info.get("state") or "live_metrics_verified",
            "supervisor_version": supervisor_version,
            "core_info_verified": True,
            "core_stats_verified": True,
            "ready_observed": True,
            "core_version": config.get("version"),
            "websocket_inventory_verified": True,
            "websocket_inventory_entries": len(inventory),
        }

    def entries(self) -> list[dict[str, Any]]:
        try:
            with self.websocket() as connection:
                result = connection.call({"type": "config_entries/get"})
        except OSError as error:
            raise HAError("Home Assistant WebSocket transport failed") from error
        if not isinstance(result, list):
            raise HAError("Home Assistant WebSocket returned an invalid config entry list")
        return result

    def resources(self) -> list[dict[str, Any]]:
        with self.websocket() as connection:
            result = connection.call({"type": "lovelace/resources"})
        return result if isinstance(result, list) else []

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": [
                {key: item.get(key) for key in ("entry_id", "domain", "title", "state", "source")}
                for item in self.entries()
                if item.get("domain") == DOMAIN
            ],
            "resources": [
                {"id": item.get("id"), "url": item.get("url"), "res_type": _resource_type(item)}
                for item in self.resources()
                if _is_van_gogh_resource(item)
            ],
        }

    def ensure_config_entry(self) -> dict[str, Any]:
        existing = [item for item in self.entries() if item.get("domain") == DOMAIN]
        if existing:
            return {"created": False, "entry_id": existing[0].get("entry_id"), "state": existing[0].get("state")}
        result = self.request_json("POST", "/config/config_entries/flow", {"handler": DOMAIN, "show_advanced_options": False})
        for _ in range(3):
            if not isinstance(result, dict):
                raise HAError("Van Gogh config flow returned an invalid result")
            result_type = result.get("type")
            if result_type == "create_entry":
                break
            if result_type == "abort" and result.get("reason") == "already_configured":
                break
            if result_type != "form" or not result.get("flow_id"):
                raise HAError(f"Van Gogh config flow stopped at {result_type or 'unknown'}")
            flow_id = urllib.parse.quote(str(result["flow_id"]), safe="")
            result = self.request_json("POST", f"/config/config_entries/flow/{flow_id}", {})
        else:
            raise HAError("Van Gogh config flow did not finish")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            entries = [item for item in self.entries() if item.get("domain") == DOMAIN]
            if entries and entries[0].get("state") == "loaded":
                return {"created": True, "entry_id": entries[0].get("entry_id"), "state": "loaded"}
            time.sleep(2)
        raise HAError("Van Gogh integration entry did not reach loaded state")

    def ensure_resource(self, module_url: str = MODULE_URL) -> dict[str, Any]:
        with self.websocket() as connection:
            resources = connection.call({"type": "lovelace/resources"})
            if not isinstance(resources, list):
                raise HAError("Home Assistant returned an invalid resource list")
            for action in resource_plan(resources, module_url):
                kind = action.pop("action")
                connection.call({"type": f"lovelace/resources/{kind}", **action})
            result = connection.call({"type": "lovelace/resources"})
        exact = [
            item for item in result
            if item.get("url") == module_url and _resource_type(item) == "module"
        ]
        owned = [item for item in result if _is_van_gogh_resource(item)]
        if len(exact) != 1 or len(owned) != 1:
            raise HAError("Van Gogh Lovelace resource did not reconcile to exactly one module")
        return {"resource_id": exact[0].get("id"), "url": module_url, "count": 1}

    def probe_module(
        self,
        module_url: str = MODULE_URL,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.core_asset_origin()}{module_url}")
        wait_seconds = max(0.0, min(timeout, 60.0))
        deadline = time.monotonic() + wait_seconds
        last_failure = "transient request failure"
        while True:
            request_timeout = min(20.0, max(0.001, deadline - time.monotonic()))
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    sample = response.read(512)
                    status = response.status
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise HAError(f"Van Gogh module returned HTTP {error.code}") from error
                last_failure = "HTTP 404"
            except OSError:
                last_failure = "transport failure"
            else:
                if status != 200:
                    raise HAError(f"Van Gogh module returned HTTP {status}")
                if not sample:
                    raise HAError("Van Gogh module returned an empty response")
                return {"status": status, "nonempty": True, "url": module_url}

            now = time.monotonic()
            if now >= deadline:
                raise HAError(
                    f"Van Gogh module was not ready within {wait_seconds:g} seconds "
                    f"(last transient failure: {last_failure})"
                )
            time.sleep(min(poll_interval, deadline - now))

    def finish_setup(self) -> dict[str, Any]:
        config = self.wait_ready()
        entry = self.ensure_config_entry()
        resource = self.ensure_resource()
        module = self.probe_module()
        return {
            "core_version": config.get("version"),
            "integration": entry,
            "resource": resource,
            "module": module,
        }

    def restore_resources(self, snapshot: list[dict[str, Any]]) -> dict[str, Any]:
        with self.websocket() as connection:
            current = connection.call({"type": "lovelace/resources"})
            for action in restore_resource_plan(
                current if isinstance(current, list) else [], snapshot
            ):
                kind = action.pop("action")
                if kind == "delete":
                    connection.call({"type": "lovelace/resources/delete", **action})
                else:
                    connection.call({"type": f"lovelace/resources/{kind}", **action})
            result = connection.call({"type": "lovelace/resources"})
        owned = [item for item in result if _is_van_gogh_resource(item)]
        if [(item.get("url"), _resource_type(item)) for item in owned] != [
            (item.get("url"), _resource_type(item)) for item in snapshot
        ]:
            raise HAError("Van Gogh resource rollback readback did not match prestate")
        expected_ids = {str(item.get("id")) for item in snapshot if item.get("id")}
        actual_ids = {str(item.get("id")) for item in owned if item.get("id")}
        return {
            "resource_count": len(owned),
            "resource_ids_preserved": expected_ids.issubset(actual_ids),
            "readback_verified": True,
        }

    def restore_prestate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Restore Van Gogh-owned registry/resource state and verify the readback."""
        expected_entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        expected_resources = snapshot.get("resources", []) if isinstance(snapshot, dict) else []
        if not isinstance(expected_entries, list) or not isinstance(expected_resources, list):
            raise HAError("Recorded Home Assistant prestate is invalid")
        current_entries = [item for item in self.entries() if item.get("domain") == DOMAIN]
        if not expected_entries:
            for item in current_entries:
                entry_id = item.get("entry_id")
                if not isinstance(entry_id, str) or not entry_id:
                    raise HAError("Van Gogh config entry has no rollback identifier")
                self.request_json("DELETE", f"/config/config_entries/entry/{urllib.parse.quote(entry_id, safe='')}")
        expected_ids = sorted(str(item.get("entry_id")) for item in expected_entries)
        actual_ids = sorted(str(item.get("entry_id")) for item in self.entries() if item.get("domain") == DOMAIN)
        if actual_ids != expected_ids:
            raise HAError("Van Gogh config entry rollback readback did not match prestate")
        resources = self.restore_resources(expected_resources)
        return {
            "config_entry_ids": actual_ids,
            "resources": resources,
            "readback_verified": True,
        }
