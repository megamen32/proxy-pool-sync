"""Хранилище инвентаря proxy5: все когда-либо выданные адреса.

Кабинет знает только текущий пул: после ротации прежние адреса из `/ips`
исчезают. Мы помним их сами — с датой первого появления, страной и тем,
действующий адрес сейчас или уже выведен из оборота.

Хранилище — обычный SQLite-файл с атомарной записью (как в
`proxy_pool_sync.storage`): файл принадлежит сервису, посторонние потребители
в него не пишут, а читают уже готовый экспорт.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .core import InventoryEntry, PoolSnapshot, merge_into_inventory

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy5_inventory (
    ip             TEXT PRIMARY KEY,
    country        TEXT,
    country_source TEXT,
    protocol       TEXT NOT NULL DEFAULT 'http',
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    last_present   REAL,
    present        INTEGER NOT NULL DEFAULT 0,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS proxy5_inventory_country ON proxy5_inventory(country);
CREATE INDEX IF NOT EXISTS proxy5_inventory_present ON proxy5_inventory(present);
CREATE TABLE IF NOT EXISTS proxy5_inventory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_PATH = Path("/var/lib/proxy5-registry/inventory.db")


class InventoryStore:
    """Инвентарь proxy5 в SQLite."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
        os.chmod(self.path, 0o640)

    # ---------- чтение ----------

    def entries(self, *, present_only: bool = False) -> list[InventoryEntry]:
        """Все известные адреса (или только действующие)."""
        query = "SELECT * FROM proxy5_inventory"
        params: tuple = ()
        if present_only:
            query += " WHERE present = 1"
        query += " ORDER BY ip"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> InventoryEntry:
        return InventoryEntry(
            ip=row["ip"],
            country=row["country"],
            country_source=row["country_source"],
            protocol=row["protocol"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            last_present=row["last_present"],
            present=bool(row["present"]),
            seen_count=row["seen_count"],
            note=row["note"],
        )

    def meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM proxy5_inventory_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO proxy5_inventory_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---------- запись ----------

    def apply_snapshot(self, snapshot: PoolSnapshot, *, now: float | None = None) -> dict:
        """Внести снимок пула; вернуть статистику изменения."""
        stamp = now if now is not None else time.time()
        inventory = {entry.ip: entry for entry in self.entries()}
        added, vanished = merge_into_inventory(inventory, snapshot, now=stamp)
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO proxy5_inventory("
                "ip, country, country_source, protocol, first_seen, last_seen,"
                " last_present, present, seen_count, note)"
                " VALUES(:ip, :country, :country_source, :protocol, :first_seen,"
                " :last_seen, :last_present, :present, :seen_count, :note)"
                " ON CONFLICT(ip) DO UPDATE SET"
                " country = excluded.country,"
                " country_source = excluded.country_source,"
                " protocol = excluded.protocol,"
                " last_seen = excluded.last_seen,"
                " last_present = excluded.last_present,"
                " present = excluded.present,"
                " seen_count = excluded.seen_count,"
                " note = excluded.note",
                [entry.to_row() for entry in inventory.values()],
            )
        self.set_meta("last_sync_at", json.dumps(stamp))
        self.set_meta("last_snapshot_size", json.dumps(len(snapshot)))
        return {
            "added": added,
            "vanished": vanished,
            "total": len(inventory),
            "present": sum(1 for e in inventory.values() if e.present),
        }

    def enrich_geo(
        self,
        resolver,
        *,
        limit: int = 2000,
        only_missing: bool = True,
    ) -> dict:
        """Дополнить гео из локального GeoIP-файла.

        Возвращает сводку: сколько проверено, сколько сошлось, сколько
        разошлось с гео кабинета.
        """
        entries = self.entries()
        targets = [
            entry for entry in entries
            if not only_missing or not entry.country or not entry.country_source
        ][:limit]
        agree = 0
        conflict = 0
        unknown = 0
        updates: list[dict] = []
        for entry in targets:
            resolved = resolver.resolve(entry.ip)
            geo_country = resolved.get("country")
            if not geo_country:
                unknown += 1
                continue
            if entry.country and entry.country != geo_country:
                conflict += 1
                updates.append({
                    "ip": entry.ip,
                    "country": entry.country,
                    "country_source": "geoip",
                    "protocol": entry.protocol,
                    "first_seen": entry.first_seen,
                    "last_seen": entry.last_seen,
                    "last_present": entry.last_present,
                    "present": 1 if entry.present else 0,
                    "seen_count": entry.seen_count,
                    "note": f"geoip conflict: cabinet={entry.country} geoip={geo_country}",
                })
            else:
                agree += 1
                updates.append({
                    "ip": entry.ip,
                    "country": geo_country,
                    "country_source": "geoip",
                    "protocol": entry.protocol,
                    "first_seen": entry.first_seen,
                    "last_seen": entry.last_seen,
                    "last_present": entry.last_present,
                    "present": 1 if entry.present else 0,
                    "seen_count": entry.seen_count,
                    "note": entry.note,
                })
        if updates:
            with self._connect() as connection:
                connection.executemany(
                    "UPDATE proxy5_inventory SET country = :country,"
                    " country_source = :country_source, note = :note WHERE ip = :ip",
                    updates,
                )
        return {
            "checked": len(targets),
            "agree": agree,
            "conflict": conflict,
            "unknown": unknown,
        }
