from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from tests import web_test_env  # noqa: F401

from geng_agent.web.importer import UnsafePdfUrl, _public_addresses


class ImporterSecurityTests(unittest.TestCase):
    @patch("geng_agent.web.importer.socket.getaddrinfo")
    def test_rejects_private_and_loopback_addresses(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(UnsafePdfUrl):
            _public_addresses("example.test", 443)

        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]
        with self.assertRaises(UnsafePdfUrl):
            _public_addresses("example.test", 443)


if __name__ == "__main__":
    unittest.main()
