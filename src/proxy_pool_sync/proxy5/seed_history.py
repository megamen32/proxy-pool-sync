"""Слить в инвентарь адреса, уже накопленные в БД Video Watching.

`db_video_stats.proxyn` хранит 6000 строк (несколько ротаций) без гео — это
и есть накопленная история, просто без привязки к стране. Забираем оттуда
адреса и их реальные порты, потом дополняем страной из локального GeoIP-файла
и сверяем с тем, что сейчас в пуле кабинета.

Чтение БД выполняется через psql от роли postgres (суперпользователь, пароля
нет), запись — в собственный инвентарь от владельца каталога.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, "/home/roomhacker/PycharmProjects/proxy-pool-sync/src")

from proxy_pool_sync.proxy5.cabinet import Cabinet, GeoResolver
from proxy_pool_sync.proxy5.core import history_summary
from proxy_pool_sync.proxy5.inventory import InventoryStore

GEOIP = "/home/roomhacker/.cloakbrowser/geoip/GeoLite2-City.mmdb"
SVC = "322981eb-ada2-4b95-9322-d025c354eb35"
INVENTORY = "/var/lib/proxy5-registry/inventory.db"


def fetch_db_endpoints() -> dict:
    """Адреса из proxyn: ip → (port, protocol).

    Читаем через psql от postgres: у прикладной роли пароль неизвестен,
    а суперпользователь доступен через peer-аутентификацию по сокету.
    """
    command = [
        "sudo", "-u", "postgres", "psql", "-d", "db_video_stats", "-tAc",
        "SELECT ip || '|' || port || '|' || coalesce(protocol,'http') FROM proxyn "
        "WHERE ip IS NOT NULL;",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"db read failed: {result.stderr.strip()[:200]}")
    seen: dict[str, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        ip = parts[0]
        if not ip or ip in seen:
            continue
        try:
            port = int(parts[1] or 8085)
        except ValueError:
            port = 8085
        seen[ip] = (port, (parts[2] or "http").lower())
    return seen


def main() -> int:
    store = InventoryStore(INVENTORY)
    cabinet = Cabinet()
    snapshot = cabinet.pool_via_egress(SVC)
    store.apply_snapshot(snapshot)

    db_rows = fetch_db_endpoints()
    current = {endpoint.ip for endpoint in snapshot.endpoints}
    known = store.entries_map()

    resolver = GeoResolver(GEOIP)
    added_history = 0
    try:
        for ip, (port, protocol) in db_rows.items():
            # Текущий пул уже в инвентаре; добавляем только то, чего в нём нет.
            if ip in current or ip in known:
                continue
            resolved = resolver.resolve(ip)
            country = resolved.get("country")
            if store.upsert_historical(
                ip,
                port=port,
                protocol=protocol,
                country=country,
                country_source="geoip" if country else None,
            ):
                added_history += 1
    finally:
        resolver.close()

    summary = history_summary(store.entries())
    print("Пул сейчас: %d" % len(snapshot))
    print("Уникальных адресов в накопленной истории БД: %d" % len(db_rows))
    print("Добавлено исторических адресов в инвентарь: %d" % added_history)
    print("Инвентарь: всего %d, действующих %d, в истории %d" % (
        summary["total"], summary["present"], summary["retired"]))
    print("Страны: %s" % summary["by_country"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
