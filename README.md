# MTAE Home Assistant Apps

Public Home Assistant App repository maintained by **MTAE Pty Ltd**.

This repository contains the **MTAE Van Gogh Installer** application shell. It does **not** contain the proprietary Van Gogh 2 product package, customer information, site credentials, release signing material, or private MTAE release infrastructure.

> **Status:** Internal beta. MTAE project use only while field acceptance is completed.

## Add this repository to Home Assistant

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open the three-dot menu and choose **Repositories**.
3. Add:

   ```text
   https://github.com/erichvanstaden/mtae-home-assistant-apps
   ```

4. Install **MTAE Van Gogh Installer**.
5. Start the app and open its Web UI.
6. Enter the one-time MTAE Install Code, then select **Activate site**. Activation uses the approved production bootstrap; there is no Installer profile or dashboard-target selector.
7. On a clean system, activation creates `/config/van_gogh2-site.json` from the package-owned neutral starter. An existing valid manifest remains authoritative and is preserved byte-for-byte. Select **Check release**, **Install / update**, then **Launch Builder**. Installer `1.2.13` is isolated-install, exact-upgrade and exact-rollback verified with private product package `2.0.0-suite.2-owner-corrections.65`, carrying component `2.0.0-suite.2-owner-corrections.65` and Foundation `1.5.0`. It preserves the selected Lovelace storage registry path, accepted Builder profile route identity, all eight managed Foundation views and native owner edits during the exact Product `.59` to `.65` upgrade. It retains the dashboard-owned background, page order, site data, integration-owned frontend transport, Builder lifecycle ownership, site bindings and fail-closed rollback.

## Ownership boundary

The Installer activates the site, verifies and installs the compatible runtime package, protects rollback state, activates the integration-owned frontend transport, removes only its obsolete local Lovelace loader, and creates only the `/van-gogh-builder/builder` control entry. Builder owns dashboard creation, selection, page changes, editing and deletion. The Installer has no dashboard-target API.

## What the Installer does

- checks access to the private MTAE release service;
- downloads only an authorised, versioned product package;
- verifies the outer release SHA-256 and the package's complete internal inventory before extraction;
- validates the site-neutral package and the commissioned local site manifest before any write;
- snapshots the affected Van Gogh-owned component, frontend resource, config entry and Builder entry state;
- installs or updates `/config/custom_components/van_gogh2` and its frontend module;
- creates the protected Builder control route without overwriting an existing route;
- restarts Home Assistant Core when required and verifies runtime, resource and Builder readiness;
- leaves every owner dashboard untouched until Builder performs an explicit owner action;
- returns an exact-current no-op only when all managed runtime bytes match the authorised package;
- supports deterministic rollback to the recorded pre-install state.

## Security boundaries

- Release packages remain private and require MTAE authorisation.
- The one-time Install Code is exchanged for a revocable per-site credential and is not stored.
- The issued site credential is stored with mode `0600` inside the Home Assistant App data directory and is never returned by its status API.
- Archive extraction rejects traversal paths, links and special files.
- File writes are restricted to Van Gogh-owned integration and Installer state paths.
- The Installer does not operate Home Assistant entities or equipment.
- The Installer creates a neutral local site manifest only when none exists; an existing valid manifest is never rewritten. Unrelated dashboard definitions, cards, views, custom components and resources remain outside its mutation scope.

Security reports: **admin@mtae.com.au**

Copyright © MTAE Pty Ltd. All rights reserved.
