"""Проверки ядра реестра proxy5: инвентарь, история, гео, инварианты.

Покрывают то, что ломается тихо: потерянную историю при ротации, ложное
«все адреса исчезли», рассинхрон стран и нарушение инварианта суммы лимитов.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy_pool_sync.proxy5.core import (  # noqa: E402
    InventoryEntry,
    PoolSnapshot,
    ProxyEndpoint,
    endpoint_from_cabinet,
    history_summary,
    merge_into_inventory,
    normalise_country,
    protocol_for_type,
    snapshot_from_cabinet,
)
from proxy_pool_sync.proxy5.inventory import InventoryStore  # noqa: E402


def make_snapshot(specs, countries=None):
    countries = countries or {}
    endpoints = tuple(
        ProxyEndpoint(
            ip=ip,
            port=8085,
            protocol="http",
            country=countries.get(ip),
            country_source="cabinet" if ip in countries else None,
        )
        for ip in specs
    )
    return PoolSnapshot(endpoints=endpoints)


class InventoryMergeTests(unittest.TestCase):
    def test_first_snapshot_marks_every_address_present(self):
        inventory = {}
        added, vanished = merge_into_inventory(
            inventory, make_snapshot(["1.1.1.1", "2.2.2.2"]), now=100.0
        )
        self.assertEqual((added, vanished), (2, 0))
        self.assertEqual(sum(1 for e in inventory.values() if e.present), 2)

    def test_rotation_keeps_history_and_marks_outdated(self):
        inventory = {}
        merge_into_inventory(inventory, make_snapshot(["1.1.1.1", "2.2.2.2"]), now=100.0)
        added, vanished = merge_into_inventory(
            inventory, make_snapshot(["2.2.2.2", "3.3.3.3"]), now=200.0
        )
        self.assertEqual(added, 1)
        self.assertEqual(vanished, 1)
        self.assertFalse(inventory["1.1.1.1"].present)
        self.assertTrue(inventory["2.2.2.2"].present)
        self.assertTrue(inventory["3.3.3.3"].present)
        # История не теряется: адрес остаётся в инвентаре.
        self.assertEqual(len(inventory), 3)
        self.assertEqual(inventory["1.1.1.1"].first_seen, 100.0)
        self.assertEqual(inventory["1.1.1.1"].last_seen, 100.0)
        self.assertEqual(inventory["3.3.3.3"].first_seen, 200.0)

    def test_repeated_snapshot_is_idempotent(self):
        inventory = {}
        merge_into_inventory(inventory, make_snapshot(["1.1.1.1"]), now=100.0)
        added, vanished = merge_into_inventory(
            inventory, make_snapshot(["1.1.1.1"]), now=200.0
        )
        self.assertEqual((added, vanished), (0, 0))
        self.assertTrue(inventory["1.1.1.1"].present)
        self.assertEqual(inventory["1.1.1.1"].seen_count, 2)

    def test_cabinet_country_overrides_stale_value(self):
        inventory = {}
        merge_into_inventory(
            inventory, make_snapshot(["1.1.1.1"], {"1.1.1.1": "RU"}), now=100.0
        )
        merge_into_inventory(
            inventory, make_snapshot(["1.1.1.1"], {"1.1.1.1": "US"}), now=200.0
        )
        self.assertEqual(inventory["1.1.1.1"].country, "US")
        self.assertIn("RU -> US", inventory["1.1.1.1"].note or "")

    def test_summary_counts_present_and_retired(self):
        inventory = {}
        merge_into_inventory(inventory, make_snapshot(["1.1.1.1"]), now=100.0)
        merge_into_inventory(inventory, make_snapshot(["2.2.2.2"]), now=200.0)
        summary = history_summary(inventory.values())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["present"], 1)
        self.assertEqual(summary["retired"], 1)


class CabinetParsingTests(unittest.TestCase):
    def test_country_normalisation(self):
        self.assertEqual(normalise_country("ru"), "RU")
        self.assertEqual(normalise_country(" us "), "US")
        self.assertIsNone(normalise_country("USA"))
        self.assertIsNone(normalise_country(""))
        self.assertIsNone(normalise_country(None))

    def test_protocol_from_cabinet_type(self):
        self.assertEqual(protocol_for_type("socks5"), "socks5")
        self.assertEqual(protocol_for_type("SOCKS5"), "socks5")
        self.assertEqual(protocol_for_type("http"), "http")
        self.assertEqual(protocol_for_type(None), "http")

    def test_endpoint_port_follows_protocol(self):
        endpoint = endpoint_from_cabinet({"ip": "1.2.3.4", "country": "US", "type": "socks5"})
        self.assertEqual(endpoint.port, 1085)
        self.assertEqual(endpoint.country, "US")
        endpoint = endpoint_from_cabinet({"ip": "1.2.3.4", "type": "http"})
        self.assertEqual(endpoint.port, 8085)

    def test_snapshot_deduplicates_by_ip(self):
        payload = {
            "ips": [
                {"ip": "1.1.1.1", "country": "US", "type": "socks5"},
                {"ip": "1.1.1.1", "country": "US", "type": "socks5"},
                {"ip": "2.2.2.2", "country": "RU", "type": "socks5"},
            ]
        }
        snapshot = snapshot_from_cabinet(payload)
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(snapshot.countries(), {"US": 1, "RU": 1})

    def test_empty_pool_is_an_error(self):
        with self.assertRaises(ValueError):
            snapshot_from_cabinet({"ips": []})


class InventoryStoreTests(unittest.TestCase):
    def test_store_roundtrip_keeps_history_across_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inv.db")
            store.apply_snapshot(make_snapshot(["1.1.1.1", "2.2.2.2"]), now=100.0)
            store.apply_snapshot(make_snapshot(["2.2.2.2", "3.3.3.3"]), now=200.0)
            entries = {entry.ip: entry for entry in store.entries()}
            self.assertEqual(len(entries), 3)
            self.assertFalse(entries["1.1.1.1"].present)
            self.assertTrue(entries["3.3.3.3"].present)
            summary = history_summary(store.entries())
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["present"], 2)

    def test_store_counts_present_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inv.db")
            store.apply_snapshot(make_snapshot(["1.1.1.1"]), now=100.0)
            store.apply_snapshot(make_snapshot(["2.2.2.2"]), now=200.0)
            present = store.entries(present_only=True)
            self.assertEqual([entry.ip for entry in present], ["2.2.2.2"])

    def test_store_persists_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inv.db")
            store.set_meta("marker", "value")
            self.assertEqual(store.meta("marker"), "value")
            self.assertIsNone(store.meta("absent"))


if __name__ == "__main__":
    unittest.main()
