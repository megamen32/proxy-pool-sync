"""Единый клиент кабинета proxy5: пул, whitelist, гео, история.

Кабинет proxy5 отдаёт текущий пул (`/ips`) с готовой гео-страной и типом
протокола, а также whitelist egress-IP с лимитами соединений. Это единственный
источник правды о том, какие адреса нам выдали и с какой страны.

Историю «всех когда-либо выданных» адресов кабинет не хранит: при ротации
пул меняется, а предыдущие адреса пропадают из `/ips`. Поэтому её ведём мы
сами: `ProxyInventory` помнит все встреченные адреса, страну, даты появления
и исчезновения.

Модуль не зависит от БД и не делает сетевых вызовов сам — сетевой доступ
разделён на `cabinet.py` (HTTP/MCP к кабинету) и `inventory.py` (хранилище).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Iterable

# Порты пула proxy5: 8085 — HTTP, 1085 — SOCKS5 (см. proxy_pack_meta.py
# в video_watching). В пуле кабинета у каждого адреса указан type.
HTTP_PORT = 8085
SOCKS5_PORT = 1085

PROTOCOL_PORTS = {"http": HTTP_PORT, "socks5": SOCKS5_PORT}


@dataclass(frozen=True)
class ProxyEndpoint:
    """Один адрес пула в виде, пригодном для выдачи потребителю."""

    ip: str
    port: int
    protocol: str
    country: str | None = None
    country_source: str | None = None

    @property
    def host_port(self) -> str:
        return f"{self.ip}:{self.port}"

    def with_credentials(self, username: str, password: str) -> str:
        """Строка в формате, который понимают потребители (proxy_pool_sync)."""
        return f"{self.protocol}://{username}:{password}@{self.host_port}"

    def with_credentials_on_ports(self, username: str, password: str) -> list[str]:
        """Обе пары портов одного адреса: HTTP и SOCKS5.

        Пул выдаёт один IP двумя протоколами; TGC держит парный порт 8085↔1085
        (proxy_managerv2._proxy_candidates_for_account), AutoFindClient — тоже.
        """
        out = []
        for protocol, port in PROTOCOL_PORTS.items():
            out.append(
                f"{protocol}://{username}:{password}@{self.ip}:{port}"
            )
        return out


@dataclass
class PoolSnapshot:
    """Состояние пула на момент снятия."""

    endpoints: tuple[ProxyEndpoint, ...] = ()
    taken_at: float = 0.0
    source: str = "cabinet"

    def __len__(self) -> int:
        return len(self.endpoints)

    def by_country(self) -> dict[str, list[ProxyEndpoint]]:
        out: dict[str, list[ProxyEndpoint]] = {}
        for endpoint in self.endpoints:
            out.setdefault(endpoint.country or "??", []).append(endpoint)
        return out

    def countries(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for endpoint in self.endpoints:
            key = endpoint.country or "??"
            out[key] = out.get(key, 0) + 1
        return out


@dataclass
class InventoryEntry:
    """Адрес, который нам когда-либо выдавали."""

    ip: str
    country: str | None = None
    country_source: str | None = None
    protocol: str = "http"
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_present: float | None = None
    present: bool = False
    seen_count: int = 0
    note: str | None = None
    extra: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "ip": self.ip,
            "country": self.country,
            "country_source": self.country_source,
            "protocol": self.protocol,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_present": self.last_present,
            "present": self.present,
            "seen_count": self.seen_count,
            "note": self.note,
        }


def normalise_country(value: str | None) -> str | None:
    """Двухбуквенный код страны в верхнем регистре, иначе None."""
    if not value:
        return None
    code = str(value).strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return code


def protocol_for_type(type_name: str | None) -> str:
    """Тип из кабинета ('socks5') → протокол для выдачи ('socks5'/'http')."""
    value = (type_name or "").strip().lower()
    return value if value in PROTOCOL_PORTS else "http"


def endpoint_from_cabinet(item: dict) -> ProxyEndpoint:
    """Один элемент ответа `/ips` → ProxyEndpoint."""
    ip = str(ipaddress.IPv4Address(str(item["ip"])))
    protocol = protocol_for_type(item.get("type"))
    return ProxyEndpoint(
        ip=ip,
        port=PROTOCOL_PORTS[protocol],
        protocol=protocol,
        country=normalise_country(item.get("country")),
        country_source="cabinet",
    )


def snapshot_from_cabinet(payload: dict) -> PoolSnapshot:
    """Ответ `/ips` кабинета → PoolSnapshot."""
    items = payload.get("ips") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("provider pool response has no ips")
    endpoints = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or "ip" not in item:
            continue
        endpoint = endpoint_from_cabinet(item)
        if endpoint.ip in seen:
            continue
        seen.add(endpoint.ip)
        endpoints.append(endpoint)
    return PoolSnapshot(endpoints=tuple(endpoints))


def merge_into_inventory(
    inventory: dict[str, InventoryEntry],
    snapshot: PoolSnapshot,
    *,
    now: float,
) -> tuple[int, int]:
    """Внести снимок пула в инвентарь.

    Возвращает (новых адресов, адресов пропавших из пула). Адрес, исчезнувший
    из пула, остаётся в инвентаре с present=False — это и есть история
    «всех когда-либо выданных».
    """
    added = 0
    # Кто был действующим ДО снимка — по нему считаем исчезнувшие.
    previously_present = {
        ip for ip, entry in inventory.items() if entry.present
    }
    for endpoint in snapshot.endpoints:
        entry = inventory.get(endpoint.ip)
        if entry is None:
            inventory[endpoint.ip] = InventoryEntry(
                ip=endpoint.ip,
                country=endpoint.country,
                country_source=endpoint.country_source,
                protocol=endpoint.protocol,
                first_seen=now,
                last_seen=now,
                last_present=now,
                present=True,
                seen_count=1,
            )
            added += 1
            continue
        entry.last_seen = now
        entry.seen_count += 1
        if not entry.present:
            entry.present = True
            entry.last_present = now
        if endpoint.country and entry.country != endpoint.country:
            # Кабинет — источник правды по стране; расхождение помечаем в note.
            entry.note = f"country {entry.country} -> {endpoint.country}"
            entry.country = endpoint.country
            entry.country_source = endpoint.country_source
        elif endpoint.country and not entry.country:
            entry.country = endpoint.country
            entry.country_source = endpoint.country_source

    current = {endpoint.ip for endpoint in snapshot.endpoints}
    vanished = 0
    for ip in previously_present - current:
        entry = inventory[ip]
        entry.present = False
        vanished += 1
    return added, vanished


def history_summary(inventory: Iterable[InventoryEntry]) -> dict:
    """Сводка по инвентарю для отчёта и MCP-инструмента."""
    entries = list(inventory)
    by_country: dict[str, int] = {}
    present = 0
    for entry in entries:
        if entry.present:
            present += 1
        key = entry.country or "??"
        by_country[key] = by_country.get(key, 0) + 1
    return {
        "total": len(entries),
        "present": present,
        "retired": len(entries) - present,
        "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])),
    }
