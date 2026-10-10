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
6. Enter the one-time MTAE Install Code supplied through the authorised MTAE channel, then select **Activate site**. The permanent HTTPS endpoint is built into the App.
7. Ensure the commissioned site manifest exists at `/config/van_gogh2-site.json`, then select **Check release** and **Install / update**. Installer `1.2.0` accepts the private product package `1.0.0-builder.23-p0.3`, carrying component `2.0.0-builder.23-p0.3`, only when its pinned identity and the local site manifest both validate.

## What the Installer does

- checks access to the private MTAE release service;
- downloads only an authorised, versioned product package;
- verifies the outer release SHA-256 and the package's complete internal inventory before extraction;
- validates the site-neutral package and the commissioned local site manifest before any write;
- snapshots the affected Van Gogh-owned component, resource and dashboard state;
- installs or updates `/config/custom_components/van_gogh2` and its frontend module;
- applies the supported dashboard plan while preserving local names, bindings, routes and unrelated views/cards;
- restarts Home Assistant Core when required and verifies readiness;
- returns an exact-current no-op only when all managed bytes and dashboard state match the authorised package;
- supports deterministic rollback to the recorded pre-install state.

## Security boundaries

- Release packages remain private and require MTAE authorisation.
- The one-time Install Code is exchanged for a revocable per-site credential and is not stored.
- The issued site credential is stored with mode `0600` inside the Home Assistant App data directory and is never returned by its status API.
- Archive extraction rejects traversal paths, links and special files.
- File writes are restricted to Van Gogh-owned integration and Installer state paths.
- The Installer does not operate Home Assistant entities or equipment.
- The local site manifest is never written by the Installer; unrelated dashboard definitions, cards, views, custom components and resources remain outside its mutation scope.

Security reports: **admin@mtae.com.au**

Copyright © MTAE Pty Ltd. All rights reserved.
