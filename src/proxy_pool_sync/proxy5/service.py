"""proxy5-registry — MCP-сервис единого реестра прокси proxy5.

Что делает:
  * держит инвентарь ВСЕХ адресов, которые нам когда-либо выдавали, с гео;
  * сверяет пул кабинета с инвентарём (ротация не теряет историю);
  * управляет whitelist egress-IP через MCP-инструменты кабинета;
  * показывает, что реально отдаётся потребителям (Video Watching, TGC,
    Out of Find Client) и где они расходятся.

Провайдерский MCP отдаёт 54 инструмента, но whitelist — только три, и ни
один не держит инвариант «все IP, каждый >=1, Σ == conn_cap». Здесь поверх
него добавлены: пересчёт суммы, проверка результата после записи, реестр
хопов, инвентарь и гео.

Ключ кабинета — Bearer из защищённого файла; он не попадает в argv, журнал и
ответы инструментов.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .cabinet import Cabinet, CabinetError, GeoResolver, compare_geo
from .core import history_summary
from .inventory import InventoryStore

SERVER_NAME = "proxy5-registry"
SERVER_VERSION = "1.0.0"

SERVICE_ID = os.environ.get(
    "PROXY5_SERVICE_ID", "322981eb-ada2-4b95-9322-d025c354eb35"
)
INVENTORY_PATH = os.environ.get("PROXY5_INVENTORY_DB", "")
GEOIP_PATH = os.environ.get("PROXY5_GEOIP_DB", "/home/roomhacker/.cloakbrowser/geoip/GeoLite2-City.mmdb")
HOPS_FILE = os.environ.get("PROXY5_HOPS_FILE", "")

mcp = FastMCP(SERVER_NAME)

_cabinet: Cabinet | None = None
_store: InventoryStore | None = None


def log(message: str) -> None:
    path = os.environ.get("PROXY5_MCP_LOG", "/var/log/proxy5-registry.log")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


def cabinet() -> Cabinet:
    global _cabinet
    if _cabinet is None:
        _cabinet = Cabinet()
    return _cabinet


def store() -> InventoryStore:
    global _store
    if _store is None:
        _store = InventoryStore(INVENTORY_PATH or None)
    return _store


def _load_registry() -> dict:
    """Реестр хопов: home_ips, {имя: {ip, role}}, primary_hop."""
    if not HOPS_FILE:
        return {}
    path = Path(HOPS_FILE)
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text())
        import importlib.util

        spec = importlib.util.spec_from_file_location("proxy5_hops", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {
            "home_ips": list(module.HOME_IPS),
            "primary_hop": module.PRIMARY_HOP,
            "hops": {name: dict(spec_) for name, spec_ in module.HOPS.items()},
        }
    except Exception as exc:  # noqa: BLE001 — реестр опционален
        log(f"hop registry unreadable: {exc}")
        return {}


# ---------------- инвентарь ----------------


@mcp.tool()
def proxy5_inventory_summary() -> str:
    """Сводка по инвентарю proxy5: сколько адресов знаем, сколько действующих, гео."""
    entries = store().entries()
    summary = history_summary(entries)
    lines = [
        f"Всего адресов за всю историю: {summary['total']}",
        f"Действующих сейчас: {summary['present']}",
        f"Выведенных из оборота: {summary['retired']}",
        "",
        "| Страна | Адресов |",
        "|---|---|",
    ]
    for country, count in summary["by_country"].items():
        lines.append(f"| {country} | {count} |")
    last_sync = store().meta("last_sync_at")
    if last_sync:
        lines.append("")
        lines.append(f"Последняя сверка с кабинетом: {last_sync}")
    return "\n".join(lines)


@mcp.tool()
def proxy5_inventory_list(
    country: str = "",
    limit: int = 100,
    include_retired: bool = False,
) -> str:
    """Список адресов инвентаря: страна, статус (действующий/исторический)."""
    entries = store().entries(present_only=not include_retired)
    wanted = (country or "").strip().upper()
    if wanted:
        entries = [e for e in entries if (e.country or "").upper() == wanted]
    limit = max(1, min(int(limit), 5000))
    rows = ["| IP | страна | статус | первое появление | ротаций |", "|---|---|---|---|---|"]
    for entry in entries[:limit]:
        status = "действует" if entry.present else "история"
        rows.append(
            f"| {entry.ip} | {entry.country or '—'} | {status} | "
            f"{entry.first_seen:.0f} | {entry.seen_count} |"
        )
    rows.append("")
    rows.append(f"Показано {min(limit, len(entries))} из {len(entries)}")
    return "\n".join(rows)


@mcp.tool()
def proxy5_geo_check(ip: str = "", limit: int = 50) -> str:
    """Сверить страну адреса: кабинет (MaxMind провайдера) против локального GeoIP-файла."""
    resolver = GeoResolver(GEOIP_PATH)
    try:
        entries = store().entries()
        if ip:
            entries = [e for e in entries if e.ip == ip.strip()]
        else:
            entries = entries[: max(1, min(int(limit), 500))]
        rows = ["| IP | кабинет | GeoIP-файл | вердикт |", "|---|---|---|---|"]
        agree = conflict = unknown = 0
        for entry in entries:
            resolved = resolver.resolve(entry.ip)
            verdict = compare_geo(entry.country, resolved.get("country"))
            if verdict == "agree":
                agree += 1
            elif verdict == "conflict":
                conflict += 1
            else:
                unknown += 1
            rows.append(
                f"| {entry.ip} | {entry.country or '—'} | "
                f"{resolved.get('country') or '—'} | {verdict} |"
            )
        rows.append("")
        rows.append(f"совпало {agree}, расхождений {conflict}, неизвестно {unknown}")
        return "\n".join(rows)
    finally:
        resolver.close()


@mcp.tool()
def proxy5_sync_inventory() -> str:
    """Сверить инвентарь с текущим пулом кабинета: новые адреса, исчезнувшие, гео."""
    try:
        snapshot = cabinet().pool_via_egress(SERVICE_ID)
    except CabinetError as exc:
        return f"Кабинет недоступен: {exc}"
    result = store().apply_snapshot(snapshot)
    countries = snapshot.countries()
    return "\n".join([
        f"Пул из кабинета: {len(snapshot)} адресов",
        f"Новых: {result['added']}, исчезнуло из пула: {result['vanished']}",
        f"Инвентарь всего: {result['total']} (действующих {result['present']})",
        "",
        "Гео в пуле: " + ", ".join(f"{code}={count}" for code, count in sorted(countries.items())),
    ])


# ---------------- whitelist ----------------


def _format_whitelist(wl: dict) -> str:
    rows = ["| IP | лимит |", "|---|---|"]
    for entry in wl.get("entries") or []:
        rows.append(f"| {entry.get('ip_or_cidr')} | {entry.get('conn_limit')} |")
    return "\n".join(rows)


@mcp.tool()
def proxy5_whitelist() -> str:
    """Текущий whitelist пула proxy5: IP, лимиты, conn_cap и занято."""
    try:
        wl = cabinet().whitelist(SERVICE_ID)
    except CabinetError as exc:
        return f"Кабинет недоступен: {exc}"
    registry = _load_registry()
    by_ip = {}
    for name, spec_ in (registry.get("hops") or {}).items():
        if (spec_ or {}).get("role") != "disabled" and (spec_ or {}).get("ip"):
            by_ip[(spec_ or {})["ip"]] = name
    lines = [_format_whitelist(wl)]
    total = sum(int(e.get("conn_limit") or 0) for e in wl.get("entries") or [])
    lines.append(
        f"conn_cap={wl.get('conn_cap')} used={wl.get('used')}/{wl.get('capacity')} "
        f"Σ лимитов={total}"
    )
    for entry in wl.get("entries") or []:
        ip = entry.get("ip_or_cidr")
        role = by_ip.get(ip)
        if role:
            lines.append(f"{ip} — известный хоп: {role}")
    missing = [
        ip for ip in (registry.get("hops") or {}).values()
        if (ip or {}).get("role") != "disabled" and (ip or {}).get("ip")
        and (ip or {})["ip"] not in {e.get("ip_or_cidr") for e in wl.get("entries") or []}
    ]
    for spec_ in missing:
        lines.append(
            f"ВНИМАНИЕ: хоп {spec_.get('ip')} НЕ в whitelist — пул ему не ответит"
        )
    return "\n".join(lines)


@mcp.tool()
def proxy5_set_limits(limits: dict) -> str:
    """Задать conn_limit по IP с пересчётом суммы (инвариант кабинета: Σ == conn_cap)."""
    try:
        wl = cabinet().whitelist(SERVICE_ID)
    except CabinetError as exc:
        return f"Кабинет недоступен: {exc}"
    conn_cap = int(wl.get("conn_cap") or 0)
    plan = {str(k): int(v) for k, v in (limits or {}).items()}
    if not plan:
        return "Пустой план лимитов."
    for ip, value in plan.items():
        if value < 1:
            return f"Отказ: лимит для {ip} должен быть >= 1 (инвариант кабинета)."
    current = {e.get("ip_or_cidr"): int(e.get("conn_limit") or 0) for e in wl.get("entries") or []}
    projected = dict(current)
    projected.update(plan)
    total = sum(projected.values())
    if conn_cap and total != conn_cap:
        return (
            f"Отказ: сумма лимитов {total} != conn_cap {conn_cap}. "
            f"Текущие: {current}. План: {plan}. Приведите сумму к conn_cap."
        )
    try:
        reply = cabinet().call_tool(
            "update_whitelist_limits",
            {"service_id": SERVICE_ID, "limits": plan},
        )
    except CabinetError as exc:
        return f"Запись не выполнена: {exc}"
    after = cabinet().whitelist(SERVICE_ID)
    after_total = sum(int(e.get("conn_limit") or 0) for e in after.get("entries") or [])
    ok = after_total == total
    return "\n".join([
        f"Записано. Σ лимитов после записи: {after_total} (ожидалось {total}).",
        "Сходится." if ok else "ВНИМАНИЕ: сумма после записи не совпала.",
        _format_whitelist(after),
        f"Ответ кабинета: {str(reply)[:300]}",
    ])


@mcp.tool()
def proxy5_add_ip(ip: str, limit: int = 0) -> str:
    """Добавить egress-IP в whitelist (с лимитом, если задан)."""
    address = (ip or "").strip()
    if not address:
        return "Пустой IP."
    try:
        reply = cabinet().call_tool(
            "add_whitelist_ip",
            {"service_id": SERVICE_ID, "ip": address},
        )
    except CabinetError as exc:
        return f"Запись не выполнена: {exc}"
    if limit:
        try:
            cabinet().call_tool(
                "update_whitelist_limits",
                {"service_id": SERVICE_ID, "limits": {address: int(limit)}},
            )
        except CabinetError as exc:
            return f"IP добавлен, но лимит не выставлен: {exc}"
    wl = cabinet().whitelist(SERVICE_ID)
    return "\n".join([
        f"Добавлен {address}.",
        _format_whitelist(wl),
        f"Ответ кабинета: {str(reply)[:300]}",
    ])


@mcp.tool()
def proxy5_remove_ip(ip: str) -> str:
    """Убрать egress-IP из whitelist."""
    address = (ip or "").strip()
    if not address:
        return "Пустой IP."
    try:
        reply = cabinet().call_tool(
            "remove_whitelist_ip",
            {"service_id": SERVICE_ID, "ip": address},
        )
    except CabinetError as exc:
        return f"Запись не выполнена: {exc}"
    wl = cabinet().whitelist(SERVICE_ID)
    return "\n".join([
        f"Убран {address}.",
        _format_whitelist(wl),
        f"Ответ кабинета: {str(reply)[:300]}",
    ])


@mcp.tool()
def proxy5_hops() -> str:
    """Реестр хопов: кто обязан быть в whitelist, кто выключен, кто основной."""
    registry = _load_registry()
    if not registry:
        return "Реестр хопов недоступен (PROXY5_HOPS_FILE не задан)."
    try:
        wl = cabinet().whitelist(SERVICE_ID)
        in_pool = {e.get("ip_or_cidr") for e in wl.get("entries") or []}
    except CabinetError:
        in_pool = set()
    lines = [
        f"Основной хоп: {registry.get('primary_hop')}",
        "",
        "| Хоп | IP | роль | в whitelist |",
        "|---|---|---|---|",
    ]
    for name, spec_ in sorted((registry.get("hops") or {}).items()):
        address = (spec_ or {}).get("ip")
        role = (spec_ or {}).get("role", "standby")
        mark = "да" if address in in_pool else ("нет" if role != "disabled" else "выключен")
        lines.append(f"| {name} | {address} | {role} | {mark} |")
    home = registry.get("home_ips") or []
    if home:
        lines.append("")
        lines.append("Домашние WAN: " + ", ".join(f"{ip} ({'в whitelist' if ip in in_pool else 'НЕТ'})" for ip in home))
    return "\n".join(lines)


@mcp.tool()
def proxy5_cabinet_tools() -> str:
    """Список инструментов MCP-кабинета proxy5 (54) — что вообще доступно провайдеру."""
    try:
        names = cabinet().list_tools()
    except CabinetError as exc:
        return f"Кабинет недоступен: {exc}"
    return "\n".join([f"Инструментов в кабинете: {len(names)}", ""] + [f"- {n}" for n in names])


def main() -> None:
    """Запуск MCP-сервера в stdio-режиме."""
    mcp.run()


if __name__ == "__main__":
    main()
