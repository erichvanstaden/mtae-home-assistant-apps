## 1.1.6

- Use the owner-approved MTAE Installer icon for the Home Assistant app card and ingress header.

# Changelog

## 1.1.5

- Expose the additive Van Gogh 2 `2.0.0-staging.2` release and verify the matching frontend module after Home Assistant restarts.

## 1.1.4

- Probe the installed browser module at the validated direct Home Assistant Core origin reported by Supervisor instead of an unsupported Supervisor static-asset path.

## 1.1.3

- Wait up to 60 seconds for the newly loaded Van Gogh module route when HTTP 404 or a transport failure indicates that route registration is still in progress.

## 1.1.2

- Prove post-restart Core readiness against real Supervisor responses that omit `/core/info.state`, using Core identity, live `/core/stats`, two stable matching API reads and authenticated WebSocket inventory.
- Continue to fail closed when Supervisor supplies an explicit non-running state or when identity, live metrics, API version or WebSocket proof is invalid.

## 1.1.1

- Treat a closed restart transport as ambiguous, then require an observed Home Assistant Core stop, a post-stop Supervisor `RUNNING` state and stable repeated API/version readback before reporting readiness.
- Keep install and rollback single-flight while exposing persistent activation, download, install, restart and verification stages in the ingress UI.
- Record restart-cycle evidence in successful install and rollback receipts.

## 1.1.0

- Replace manual permanent release-token entry with one-time MTAE Install Code activation.
- Persist only the issued revocable per-site credential in protected App data.
- Preserve checksum verification, safe extraction, readiness gates and deterministic rollback.

## 1.0.2

- Declare the least-privilege Home Assistant Supervisor role explicitly so the
  installer receives its scoped token for API-managed setup and restart.

## 1.0.1

- Rebuild the Supervisor app with the verified Home Assistant restart, readiness,
  resource-response compatibility, and deterministic rollback fixes.

## 1.0.0

- Initial owner-branded Van Gogh 2 Home Assistant app.
- Token-gated release checks and checksum-verified installation.
- Exact pre-change backup and deterministic rollback.
- Ingress workflow requiring no shell access.
