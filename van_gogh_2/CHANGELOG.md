# Changelog

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
