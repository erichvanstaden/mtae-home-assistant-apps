# Changelog

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
