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
7. Select **Check release**, then **Install / update**. Installer 1.1.16 accepts only private candidate Van Gogh 2 `2.0.0-staging.10` with its pinned archive identity and exact Home Assistant `2026.8.3` compatibility record. The first clean install creates the supported `/van-gogh-builder/builder` entry without overwriting an existing owner dashboard.
8. On supported Home Assistant `2026.9.3` systems, select **Migrate Home C-grid** to upgrade a structurally recognised legacy four-C-bay Van Gogh Home dashboard. The target is discovered from product schema markers, not customer or route identity.
9. For an owner review, choose the approved private dashboard JSON in the Web UI and select **Create / refresh Family review**. This bounded action targets only the separate `van-gogh-c-grid-review` dashboard and records its own exact prestate restore.

## What the Installer does

- checks access to the private MTAE release service;
- downloads only an authorised versioned package;
- verifies the release SHA-256 before extraction;
- snapshots Van Gogh-owned state;
- installs or updates `/config/custom_components/van_gogh2`;
- reconciles the Van Gogh Lovelace module to exactly one resource;
- restarts Home Assistant Core when required and verifies readiness;
- performs an explicit, idempotent Home schema migration only for one unambiguous recognised product layout, preserving site-local configuration;
- creates or refreshes the separate Family review dashboard only when an approved private JSON file is supplied locally;
- supports deterministic rollback to the recorded pre-install state.

## Security boundaries

- Release packages remain private and require MTAE authorisation.
- The one-time Install Code is exchanged for a revocable per-site credential and is not stored.
- The issued site credential is stored with mode `0600` inside the Home Assistant App data directory and is never returned by its status API.
- Archive extraction rejects traversal paths, links and special files.
- File writes are restricted to Van Gogh-owned integration and Installer state paths.
- The Installer does not operate Home Assistant entities or equipment.
- Home schema migration changes only the recognised contiguous legacy C-bay slice; unrelated dashboard definitions, cards, views, custom components and resources remain outside its mutation scope.

Security reports: **admin@mtae.com.au**

Copyright © MTAE Pty Ltd. All rights reserved.
