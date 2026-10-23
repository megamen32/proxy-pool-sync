"""Гео-фильтр выдачи: /pool?country= отдаёт только адреса нужных стран."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_pool_sync.proxy5 import serve  # noqa: E402


class CountryFilterTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.pool = root / "pool.txt"
        self.json_path = root / "pool.json"
        self.pool.write_text(
            "http://1.1.1.1:8085\nhttp://2.2.2.2:8085\nhttp://3.3.3.3:8085\n"
        )
        self.json_path.write_text(json.dumps({
            "addresses": [
                {"ip": "1.1.1.1", "country": "US", "port": 8085, "protocol": "http"},
                {"ip": "2.2.2.2", "country": "RU", "port": 8085, "protocol": "http"},
                {"ip": "3.3.3.3", "country": "SC", "port": 8085, "protocol": "http"},
            ]
        }))
        self.port = 0  # эфемерный порт
        self._server = serve.ThreadingHTTPServer(
            ("127.0.0.1", self.port), serve.PoolHandler
        )
        self.port = self._server.server_address[1]
        self._server.pool_path = self.pool
        self._server.json_path = self.json_path
        self._server.store = None
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._dir.cleanup()

    def _get(self, query: str = ""):
        url = f"http://127.0.0.1:{self.port}/pool{query}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_without_filter_returns_everything(self):
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(len([line for line in body.splitlines() if line.strip()]), 3)

    def test_country_filter_selects_matching_addresses(self):
        status, body = self._get("?country=US")
        self.assertEqual(status, 200)
        lines = [line for line in body.splitlines() if line.strip()]
        self.assertEqual(lines, ["http://1.1.1.1:8085"])

    def test_country_filter_accepts_several_countries(self):
        status, body = self._get("?country=US,SC")
        self.assertEqual(status, 200)
        lines = {line for line in body.splitlines() if line.strip()}
        self.assertEqual(lines, {"http://1.1.1.1:8085", "http://3.3.3.3:8085"})

    def test_unknown_country_is_not_found(self):
        status, body = self._get("?country=XX")
        self.assertEqual(status, 404)
        self.assertIn("no addresses", body)


if __name__ == "__main__":
    unittest.main()
