# Changelog

## 1.2.2

- Ship corrected Builder.22 successor package `1.0.0-builder.22-deployable.4`, component `2.0.0-builder.3`, Foundation `1.5.0`, and frontend assets `2.0.0-suite.1` as an unpublished candidate with a fail-closed neutral profile contract: Wall-10 remains its canonical seven routes and Mobile remains its canonical eleven routes.
- Select the profile-specific neutral starter only after Foundation generates shared assets, preventing Wall/Mobile cross-profile dispatch while retaining existing-site preservation and rollback behaviour.
- Preserve the protected Builder.22 renderer, component, geometry, interaction and safety source bytes; only allowlisted site identity, route and binding values are neutralised in generated starters.
- Add explicit production/staging selection and Wall-10/Mobile starter profiles to one-time Install Code activation.
- Create a package-owned neutral starter manifest on clean activation while preserving an existing valid canonical site manifest byte-for-byte.
- Keep the component, Foundation files, dashboard, Home Assistant prestate and both install receipts inside one recoverable transaction until all post-install receipt finalisation succeeds.
- Restore the exact pre-call state when current-receipt, operation-receipt, Home Assistant prestate-annotation or readiness-annotation persistence fails.
- Preserve Installer 1.2.1 containment behaviour; no live-system or equipment action is added.

## 1.2.1

- Bind dashboard planning to the canonical site ID so a new site installs only at its supplied path and never upgrades an unrelated Installer-managed or legacy Van Gogh dashboard.
- Persist the canonical site ID in the product marker and reject a conflicting marker at the target path.
- Add the exact captured staging.8 predecessor regression for clean install, no-op, rollback and identical reinstall containment.

## 1.2.0

- Install the site-neutral private product package `1.0.0-builder.23-p0.3`, carrying accepted component `2.0.0-builder.23-p0.3`, through the validated local site-manifest contract.
- Preserve the public `van_gogh_2` App namespace, protected `/data` activation/settings state, local site-manifest bytes, site bindings and unrelated Home Assistant surfaces across clean install and in-place App upgrades.
- Validate the full package and dashboard plan before writing, return an exact-current no-op only for matching managed state, and retain exact pre-install rollback through the existing App controls.

## 1.1.17

- Fix forward from immutable `v1.1.16` by including the reviewed Builder-entry `ha_client.py` change and its regression test; `v1.1.16` is preserved and not rewritten.
- Keep private package `2.0.0-staging.10`, its pinned archive identity and Home Assistant `2026.8.3` support unchanged.

## 1.1.16

- Pin corrected private candidate `2.0.0-staging.10`, which adds the requested Builder card registration after clean-HA browser proof exposed the staging.9 mismatch.
- Preserve Installer 1.1.15 behaviour and Home Assistant `2026.8.3` support; no publication or live mutation.

## 1.1.15

- Pin private candidate `2.0.0-staging.9` and preserve the Home Assistant `2026.8.3` support contract.
- Create a supported `van-gogh-builder` storage-dashboard entry on first install without overwriting an existing owner dashboard.
- Include that Builder dashboard in install prestate and exact rollback.

## 1.1.14

- Add a generic, idempotent Van Gogh Home product-schema migration from the recognised four legacy C-bays to the approved one-parent C1-C8 grid.
- Discover the eligible storage dashboard by Van Gogh view/card schema markers rather than by customer identity, host, activation name, title or dashboard path.
- Preserve site-local bindings, feature choices, routes, unrelated cards and all other views; unsupported or ambiguous schemas fail before any Home Assistant dashboard write.
- Capture exact migration prestate, compare before save, verify exact post-write readback and automatically restore after an unverified write.
- Add explicit migration rollback in the Installer UI and API.

## 1.1.13

- Pin the corrected immutable Van Gogh 2 `2.0.0-staging.8` package identity and archive checksum while preserving the Installer 1.1.12 transport, restart, resource, dashboard and rollback behaviour.

## 1.1.12

- Bypass ambient HTTP proxy settings only for the validated private Home Assistant Core module readiness probe, preserving the existing release, restart, integration, resource and rollback paths.

## 1.1.11

- Omit both unsupported immutable fields, `mode` and `url_path`, when updating or restoring an existing Home Assistant dashboard while preserving `mode: storage` on creation.
- Bind the exact 10-inch Review Home C region to the approved one-parent C1-C8 grid and its ordinary single pencil editor through immutable Van Gogh `2.0.0-staging.7`.

## 1.1.10

- Preserve `mode: storage` when creating a review dashboard while omitting the unsupported immutable `mode` field when refreshing or restoring an existing Home Assistant dashboard.

## 1.1.9

- Bind installation to the immutable Van Gogh 2 `2.0.0-staging.6` archive and Home Assistant `2026.8.3` compatibility record.
- Add a supported, bounded ingress action to create or refresh only the separate Family review dashboard from an approved private JSON file, with exact readback, automatic failure recovery and explicit prestate restore.

## 1.1.8

- Publish the Installer refresh for the immutable Van Gogh 2 `2.0.0-staging.4` private release while preserving the existing activation, checksum, installation, readiness and rollback logic.

## 1.1.7

- Derive post-restart resource reconciliation and module probing from the validated installed release-manifest version so staging.3 installs verify the matching staging.3 frontend asset.

## 1.1.6

- Use the owner-approved MTAE Installer icon for the Home Assistant app card and ingress header.

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
