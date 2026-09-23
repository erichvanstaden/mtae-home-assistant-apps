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
6. Enter the private release endpoint and MTAE Install Code supplied through the authorised MTAE channel.

## What the Installer does

- checks access to the private MTAE release service;
- downloads only an authorised versioned package;
- verifies the release SHA-256 before extraction;
- snapshots Van Gogh-owned state;
- installs or updates `/config/custom_components/van_gogh2`;
- reconciles the Van Gogh Lovelace module to exactly one resource;
- restarts Home Assistant Core when required and verifies readiness;
- supports deterministic rollback to the recorded pre-install state.

## Security boundaries

- Release packages remain private and require MTAE authorisation.
- Access credentials are stored inside the Home Assistant App data directory and are never returned by its status API.
- Archive extraction rejects traversal paths, links and special files.
- File writes are restricted to Van Gogh-owned integration and Installer state paths.
- The Installer does not operate Home Assistant entities or equipment.
- Dashboard definitions and unrelated custom components/resources are outside its mutation scope.

Security reports: **admin@mtae.com.au**

Copyright © MTAE Pty Ltd. All rights reserved.
