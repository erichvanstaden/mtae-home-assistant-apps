#!/usr/bin/env python3
"""Focused regression for direct Core module readiness transport."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "van_gogh_2" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from ha_client import HAClient, HAError, module_url_for_release, validate_module_url  # noqa: E402


CACHE_SAFE_URL = "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js?v=2.0.0-builder.5"


class TestDirectCoreModuleTransport(unittest.TestCase):
    def test_environment_supports_isolated_supervisor_port(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SUPERVISOR_TOKEN": "test-token",
                "SUPERVISOR_HOST": "127.0.0.1",
                "SUPERVISOR_PORT": "18220",
            },
            clear=True,
        ):
            client = HAClient.from_environment()
        self.assertIsNotNone(client)
        self.assertEqual(client.host, "127.0.0.1")
        self.assertEqual(client.port, 18220)

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
            result = client.probe_module(CACHE_SAFE_URL, timeout=0)

        self.assertEqual(result["status"], 200)
        ambient_urlopen.assert_not_called()
        build_opener.assert_called_once()
        handler = build_opener.call_args.args[0]
        self.assertEqual(handler.proxies, {})
        direct_opener.open.assert_called_once()

    def test_component_version_becomes_exact_cache_key(self) -> None:
        self.assertEqual(module_url_for_release("2.0.0-builder.5"), CACHE_SAFE_URL)
        self.assertEqual(validate_module_url(CACHE_SAFE_URL), CACHE_SAFE_URL)

    def test_unsafe_or_unversioned_urls_are_rejected(self) -> None:
        rejected = (
            "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js",
            "/local/van-gogh2/../van-gogh2.js?v=2.0.0-builder.5",
            "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js?v=2.0.0-builder.5&x=1",
            "/local/van-gogh2/2.0.0-suite.1/van-gogh2.js?v=../../escape",
            "https://example.invalid/van-gogh2.js?v=2.0.0-builder.5",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(HAError):
                validate_module_url(value)


if __name__ == "__main__":
    unittest.main()
