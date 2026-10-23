"""Проверки snapshot-файла пула proxy5 и его HTTP-выдачи.

Без сети и без БД: инвентарь подменяется заглушкой, HTTP-сервер
поднимается на loopback в том же процессе.
"""

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_pool_sync.proxy5.core import InventoryEntry  # noqa: E402
from proxy_pool_sync.proxy5.serve import build_server  # noqa: E402
from proxy_pool_sync.proxy5.snapshot import (  # noqa: E402
    build_snapshot,
    json_path,
    snapshot_path,
    write_snapshot,
)


class FakeStore:
    """Инвентарь-заглушка: те же методы, что у InventoryStore."""

    def __init__(self, entries, meta=None):
        self._entries = list(entries)
        self._meta = dict(meta or {})

    def entries(self, *, present_only=False):
        if present_only:
            return [entry for entry in self._entries if entry.present]
        return list(self._entries)

    def meta(self, key):
        return self._meta.get(key)


def make_entry(ip, *, present=True, country="US", source="cabinet"):
    return InventoryEntry(
        ip=ip,
        country=country,
        country_source=source,
        protocol="socks5",
        first_seen=100.0,
        last_seen=200.0,
        last_present=200.0 if present else None,
        present=present,
        seen_count=1,
    )


def request(server, path, method="GET"):
    host, port = server.server_address[0], server.server_address[1]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, headers, body
    finally:
        connection.close()


class SnapshotContentTests(unittest.TestCase):
    def test_snapshot_contains_only_active_addresses(self):
        store = FakeStore([
            make_entry("1.1.1.1", present=True),
            make_entry("2.2.2.2", present=False),
            make_entry("3.3.3.3", present=True),
        ])
        payload = build_snapshot(store, now=1000.0)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([item["ip"] for item in payload["addresses"]], ["1.1.1.1", "3.3.3.3"])
        self.assertEqual(payload["total_inventory"], 3)
        self.assertEqual(payload["retired"], 1)

    def test_order_is_numeric_by_ip_and_stable(self):
        store = FakeStore([
            make_entry("10.0.0.1", present=True),
            make_entry("9.0.0.1", present=True),
            make_entry("100.0.0.1", present=True),
        ])
        payload = build_snapshot(store, now=1000.0)
        self.assertEqual(
            [item["ip"] for item in payload["addresses"]],
            ["9.0.0.1", "10.0.0.1", "100.0.0.1"],
        )
        # Повторный снимок того же инвентаря даёт тот же порядок.
        again = build_snapshot(store, now=2000.0)
        self.assertEqual(
            [item["ip"] for item in payload["addresses"]],
            [item["ip"] for item in again["addresses"]],
        )

    def test_pool_lines_use_expected_consumer_format(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.txt"
            store = FakeStore([make_entry("1.2.3.4"), make_entry("5.6.7.8")])
            write_snapshot(store, path=pool)
            self.assertEqual(
                pool.read_text(encoding="utf-8"),
                "http://1.2.3.4:8085\nhttp://5.6.7.8:8085\n",
            )

    def test_json_snapshot_carries_geo_and_metadata(self):
        store = FakeStore([make_entry("1.2.3.4", country="DE", source="geoip")])
        payload = build_snapshot(store, now=1000.0)
        address = payload["addresses"][0]
        self.assertEqual(address["ip"], "1.2.3.4")
        self.assertEqual(address["port"], 8085)
        self.assertEqual(address["protocol"], "http")
        self.assertEqual(address["country"], "DE")
        self.assertEqual(address["country_source"], "geoip")
        self.assertEqual(address["first_seen"], 100.0)
        self.assertEqual(address["last_seen"], 200.0)
        self.assertTrue(address["present"])
        self.assertEqual(payload["countries"], {"DE": 1})

    def test_empty_inventory_gives_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.txt"
            write_snapshot(FakeStore([]), path=pool)
            self.assertEqual(pool.read_text(encoding="utf-8"), "")


class SnapshotWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_permissions_are_0640(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.txt"
            store = FakeStore([make_entry("1.1.1.1"), make_entry("2.2.2.2")])
            write_snapshot(store, path=pool)

            self.assertEqual(stat.S_IMODE(pool.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(json_path(pool).stat().st_mode), 0o640)
            # Временных файлов не осталось — запись шла через os.replace.
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["pool.json", "pool.txt"],
            )
            self.assertEqual(
                pool.read_text(encoding="utf-8"),
                "http://1.1.1.1:8085\nhttp://2.2.2.2:8085\n",
            )
            payload = json.loads(json_path(pool).read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 2)

    def test_rewrite_replaces_file_completely(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "pool.txt"
            write_snapshot(FakeStore([make_entry("1.1.1.1")]), path=pool)
            write_snapshot(FakeStore([make_entry("2.2.2.2")]), path=pool)
            self.assertEqual(pool.read_text(encoding="utf-8"), "http://2.2.2.2:8085\n")

    def test_env_override_selects_snapshot_path(self):
        original = os.environ.get("PROXY5_POOL_SNAPSHOT")
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "custom.txt"
                os.environ["PROXY5_POOL_SNAPSHOT"] = str(target)
                self.assertEqual(snapshot_path(), target)
                self.assertEqual(json_path(), Path(directory) / "pool.json")
        finally:
            if original is None:
                os.environ.pop("PROXY5_POOL_SNAPSHOT", None)
            else:
                os.environ["PROXY5_POOL_SNAPSHOT"] = original


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.pool = Path(self.directory.name) / "pool.txt"
        self.store = FakeStore(
            [make_entry("1.1.1.1"), make_entry("2.2.2.2"), make_entry("3.3.3.3", present=False)],
            meta={"last_sync_at": "1750000000.0"},
        )
        write_snapshot(self.store, path=self.pool)
        self.server = build_server(
            "127.0.0.1", 0, pool_path=self.pool, store=self.store
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def test_pool_returns_exact_file_content(self):
        status, headers, body = request(self.server, "/pool")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.pool.read_bytes())
        self.assertEqual(headers["x-proxy-pool-count"], "2")

    def test_pool_json_returns_snapshot(self):
        status, headers, body = request(self.server, "/pool.json")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["count"], 2)
        self.assertEqual([item["ip"] for item in payload["addresses"]], ["1.1.1.1", "2.2.2.2"])

    def test_health_reports_counters_and_sync_time(self):
        status, headers, body = request(self.server, "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["inventory_total"], 3)
        self.assertEqual(payload["inventory_present"], 2)
        self.assertEqual(payload["last_sync_at"], 1750000000.0)

    def test_index_explains_service(self):
        status, headers, body = request(self.server, "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("/pool", text)
        self.assertIn(str(self.pool), text)

    def test_unknown_path_is_404(self):
        status, headers, body = request(self.server, "/nope")
        self.assertEqual(status, 404)


class ServeDegradedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.pool = Path(self.directory.name) / "pool.txt"
        self.server = build_server(
            "127.0.0.1", 0, pool_path=self.pool, store=FakeStore([])
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def test_missing_snapshot_file_is_503(self):
        status, headers, body = request(self.server, "/pool")
        self.assertEqual(status, 503)
        self.assertIn(str(self.pool), body.decode("utf-8"))

    def test_missing_json_snapshot_is_503(self):
        status, headers, body = request(self.server, "/pool.json")
        self.assertEqual(status, 503)

    def test_health_is_200_but_degraded_on_empty_inventory(self):
        status, headers, body = request(self.server, "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["inventory_total"], 0)


if __name__ == "__main__":
    unittest.main()
