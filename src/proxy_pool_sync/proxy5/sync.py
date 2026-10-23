"""Сверка инвентаря proxy5 с кабинетом — то, что дёргает таймер.

Делает три вещи и ничего больше:
  1) снимает текущий пул кабинета и вносит его в инвентарь (ротация не
     теряет историю: исчезнувшие адреса остаются с меткой «история»);
  2) сверяет страну по локальному GeoIP-файлу — независимое второе мнение;
  3) пишет сводку в stdout и в состояние инвентаря.

Запись в кабинет отсюда НЕ выполняется: только чтение.
"""

from __future__ import annotations

import json
import os
import sys
import time

from .cabinet import Cabinet, CabinetError, GeoResolver, compare_geo
from .inventory import InventoryStore
from .core import history_summary

SERVICE_ID = os.environ.get(
    "PROXY5_SERVICE_ID", "322981eb-ada2-4b95-9322-d025c354eb35"
)
INVENTORY_DB = os.environ.get("PROXY5_INVENTORY_DB", "")
GEOIP_DB = os.environ.get(
    "PROXY5_GEOIP_DB", "/home/roomhacker/.cloakbrowser/geoip/GeoLite2-City.mmdb"
)
GEO_LIMIT = int(os.environ.get("PROXY5_GEO_CHECK_LIMIT", "300"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_out = "--json" in argv

    store = InventoryStore(INVENTORY_DB or None)
    cabinet = Cabinet()
    try:
        snapshot = cabinet.pool_via_egress(SERVICE_ID)
    except CabinetError as exc:
        print(f"Сверка не выполнена: {exc}", file=sys.stderr)
        return 1

    result = store.apply_snapshot(snapshot)
    store.set_meta("last_countries", json.dumps(snapshot.countries()))

    geo = {"checked": 0, "agree": 0, "conflict": 0, "unknown": 0}
    resolver = GeoResolver(GEOIP_DB)
    try:
        geo = store.enrich_geo(resolver, limit=GEO_LIMIT, only_missing=False)
    except Exception as exc:  # noqa: BLE001 — гео не должно ронять сверку
        geo = {"checked": 0, "agree": 0, "conflict": 0, "unknown": 0,
               "error": f"{type(exc).__name__}: {exc}"}
    finally:
        resolver.close()

    summary = history_summary(store.entries())
    payload = {
        "pool_size": len(snapshot),
        "countries": snapshot.countries(),
        "inventory": result,
        "history": summary,
        "geo": geo,
        "synced_at": time.time(),
    }
    store.set_meta("last_sync_summary", json.dumps(payload))

    if json_out:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(
        "Пул: {pool_size} адресов ({countries}). "
        "Инвентарь: всего {total}, действующих {present}, в истории {retired}.".format(
            pool_size=payload["pool_size"],
            countries=", ".join(f"{k}={v}" for k, v in sorted(snapshot.countries().items())),
            total=summary["total"],
            present=summary["present"],
            retired=summary["retired"],
        )
    )
    print(
        "Гео-сверка: проверено {checked}, совпало {agree}, "
        "расхождений {conflict}, неизвестно {unknown}.".format(**geo)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
