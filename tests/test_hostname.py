from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from petcam.infrastructure.system.hostname import advertised_hostname


class AdvertisedHostnameTests(unittest.TestCase):
    def test_short_system_hostname_is_advertised_as_local_mdns_name(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("petcam.infrastructure.system.hostname.socket.gethostname", return_value="KittyCam"),
        ):
            self.assertEqual(advertised_hostname(), "kittycam.local")

    def test_existing_local_suffix_is_not_duplicated(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("petcam.infrastructure.system.hostname.socket.gethostname", return_value="kittycam.local"),
        ):
            self.assertEqual(advertised_hostname(), "kittycam.local")

    def test_existing_fully_qualified_hostname_is_preserved(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("petcam.infrastructure.system.hostname.socket.gethostname", return_value="kittycam.example.net"),
        ):
            self.assertEqual(advertised_hostname(), "kittycam.example.net")

    def test_explicit_advertised_host_overrides_system_hostname(self):
        with patch.dict(os.environ, {"PETCAM_ADVERTISE_HOST": "camera.example.net"}):
            with patch("petcam.infrastructure.system.hostname.socket.gethostname", return_value="kittycam"):
                self.assertEqual(advertised_hostname(), "camera.example.net")


if __name__ == "__main__":
    unittest.main()
