#!/usr/bin/env python3
"""Focused regression for direct Core module readiness transport."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from ha_client import HAClient  # noqa: E402


class TestDirectCoreModuleTransport(unittest.TestCase):
    def test_module_probe_bypasses_ambient_http_proxy(self) -> None:
        """The private Supervisor Core origin must never use process proxy settings."""
        client = HAClient(token="test-token", host="supervisor")
        client.request_supervisor_json = mock.Mock(  # type: ignore[method-assign]
            return_value={"ip_address": "172.30.32.1", "port": 8123}
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"module"
        direct_opener = mock.Mock()
        direct_opener.open.return_value = response

        with (
            mock.patch(
                "ha_client.urllib.request.urlopen",
                side_effect=OSError("ambient proxy intercepted direct Core request"),
            ) as ambient_urlopen,
            mock.patch(
                "ha_client.urllib.request.build_opener", return_value=direct_opener
            ) as build_opener,
        ):
            result = client.probe_module(timeout=0)

        self.assertEqual(result["status"], 200)
        ambient_urlopen.assert_not_called()
        build_opener.assert_called_once()
        handler = build_opener.call_args.args[0]
        self.assertEqual(handler.proxies, {})
        direct_opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
