"""Зеркалирование снимка пула в файлы потребителей.

Потребители (TGC, ATV) читают пул из файла. Реестр раскладывает ту же выдачу
в эти файлы, чтобы при падении HTTP-сервиса у потребителя остался последний
рабочий снимок на диске.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_pool_sync.proxy5 import snapshot as snap  # noqa: E402
from proxy_pool_sync.proxy5.core import (  # noqa: E402
    InventoryEntry,
    PoolSnapshot,
    ProxyEndpoint,
)
from proxy_pool_sync.proxy5.inventory import InventoryStore  # noqa: E402


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("PROXY5_POOL_MIRRORS", None)

    def tearDown(self):
        os.environ.pop("PROXY5_POOL_MIRRORS", None)
        if self._saved is not None:
            os.environ["PROXY5_POOL_MIRRORS"] = self._saved

    def _store(self, directory: str) -> InventoryStore:
        store = InventoryStore(Path(directory) / "inv.db")
        store.apply_snapshot(
            PoolSnapshot(endpoints=(
                ProxyEndpoint(ip="10.0.0.2", port=8085, protocol="http", country="RU"),
                ProxyEndpoint(ip="10.0.0.1", port=8085, protocol="http", country="US"),
            )),
            now=100.0,
        )
        return store

    def test_mirror_receives_same_pool_text(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "consumer" / "proxies.txt"
            os.environ["PROXY5_POOL_MIRRORS"] = str(mirror)
            store = self._store(directory)
            result = snap.write_snapshot(store, path=Path(directory) / "pool.txt", now=200.0)
            self.assertTrue(mirror.exists())
            self.assertEqual(mirror.read_text(), (Path(directory) / "pool.txt").read_text())
            self.assertIn("http://10.0.0.1:8085", mirror.read_text())
            self.assertIn(str(mirror), result["mirrors"])

    def test_mirror_is_world_readable_for_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "proxies.txt"
            os.environ["PROXY5_POOL_MIRRORS"] = str(mirror)
            store = self._store(directory)
            snap.write_snapshot(store, path=Path(directory) / "pool.txt", now=200.0)
            self.assertEqual(mirror.stat().st_mode & 0o777, 0o644)

    def test_broken_mirror_does_not_fail_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file.txt"
            blocked.write_text("x")
            os.environ["PROXY5_POOL_MIRRORS"] = str(blocked / "nested" / "proxies.txt")
            store = self._store(directory)
            result = snap.write_snapshot(store, path=Path(directory) / "pool.txt", now=200.0)
            self.assertTrue(result["ok"])
            self.assertTrue((Path(directory) / "pool.txt").exists())


if __name__ == "__main__":
    unittest.main()
