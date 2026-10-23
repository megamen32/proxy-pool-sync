"""Канонический snapshot пула proxy5: текстовый файл и JSON-снимок.

Потребители (Video Watching, TGC, Out of Find Client, ATV) берут прокси из
одного реестра. Формат строки — тот, что они уже понимают:
`http://<ip>:8085`. Логина и пароля у пула нет: аутентификация идёт по
whitelist egress-IP, поэтому в строке только схема, IP и порт.

Файл собирается из инвентаря по действующим адресам (present=1): ротация
пула не ломает выдачу, а история в файл не попадает. Запись атомарная —
временный файл в том же каталоге плюс os.replace, читатель никогда не
увидит половину файла.

Хранилище инвентаря внедряется (а не создаётся внутри модуля): так
snapshot собирается и проверяется без БД.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .core import HTTP_PORT, InventoryEntry

if TYPE_CHECKING:  # pragma: no cover — только подсказки типов
    from .inventory import InventoryStore

DEFAULT_SNAPSHOT_PATH = Path("/var/lib/proxy5-registry/pool.txt")
JSON_FILE_NAME = "pool.json"
FILE_MODE = 0o640
MIRROR_MODE = 0o644
POOL_SCHEME = "http"


def snapshot_path() -> Path:
    """Путь файла пула: PROXY5_POOL_SNAPSHOT либо дефолт."""
    override = os.environ.get("PROXY5_POOL_SNAPSHOT", "").strip()
    return Path(override) if override else DEFAULT_SNAPSHOT_PATH


def mirror_paths() -> list[Path]:
    """Файлы, куда раскладывается та же выдача для потребителей.

    Потребители (TGC, ATV) читают пул из файла, который им уже известен.
    Реестр пишет в эти файлы сам, поэтому при падении HTTP-сервиса у них
    остаётся последний рабочий снимок на диске.
    """
    raw = os.environ.get("PROXY5_POOL_MIRRORS", "").strip()
    if not raw:
        return []
    return [Path(item.strip()) for item in raw.split(",") if item.strip()]


def json_path(path: "str | os.PathLike[str] | None" = None) -> Path:
    """JSON-снимок лежит рядом с текстовым файлом пула."""
    target = Path(path) if path is not None else snapshot_path()
    return target.parent / JSON_FILE_NAME


def _ip_sort_key(ip: str) -> tuple[int, int, str]:
    """Порядок по IP — числовой (9.0.0.1 идёт раньше 10.0.0.1)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return (1, 0, str(ip))
    return (0, int(address), str(ip))


def active_entries(store: "InventoryStore") -> list[InventoryEntry]:
    """Действующие адреса инвентаря в стабильном порядке по IP."""
    entries = [entry for entry in store.entries() if entry.present]
    entries.sort(key=lambda entry: _ip_sort_key(entry.ip))
    return entries


def pool_lines(entries: Iterable[InventoryEntry]) -> list[str]:
    """Строки пула для потребителей: `http://<ip>:8085`."""
    return [f"{POOL_SCHEME}://{entry.ip}:{HTTP_PORT}" for entry in entries]


def pool_text(entries: Iterable[InventoryEntry]) -> str:
    """Текст файла пула: одна строка на адрес, без пустых строк."""
    return "".join(f"{line}\n" for line in pool_lines(entries))


def build_snapshot(
    store: "InventoryStore",
    *,
    now: float | None = None,
    entries: list[InventoryEntry] | None = None,
) -> dict:
    """JSON-снимок пула из инвентаря (без записи на диск)."""
    stamp = time.time() if now is None else now
    all_entries = list(entries) if entries is not None else list(store.entries())
    active = [entry for entry in all_entries if entry.present]
    active.sort(key=lambda entry: _ip_sort_key(entry.ip))

    countries: dict[str, int] = {}
    for entry in active:
        key = entry.country or "??"
        countries[key] = countries.get(key, 0) + 1

    last_sync = None
    meta = getattr(store, "meta", None)
    if callable(meta):
        try:
            raw = meta("last_sync_at")
            last_sync = float(raw) if raw else None
        except Exception:  # noqa: BLE001 — метаданные не критичны для снимка
            last_sync = None

    return {
        "generated_at": stamp,
        "generated_at_utc": utc_stamp(stamp),
        "source": "proxy5-registry/inventory",
        "count": len(active),
        "total_inventory": len(all_entries),
        "retired": len(all_entries) - len(active),
        "last_sync_at": last_sync,
        "last_sync_at_utc": utc_stamp(last_sync) if last_sync else None,
        "countries": dict(sorted(countries.items())),
        "addresses": [
            {
                "ip": entry.ip,
                "port": HTTP_PORT,
                "protocol": POOL_SCHEME,
                "country": entry.country,
                "country_source": entry.country_source,
                "first_seen": entry.first_seen,
                "last_seen": entry.last_seen,
                "present": bool(entry.present),
            }
            for entry in active
        ],
    }


def write_snapshot(
    store: "InventoryStore",
    *,
    path: "str | os.PathLike[str] | None" = None,
    now: float | None = None,
) -> dict:
    """Записать текстовый файл пула и JSON-снимок. Обе записи атомарны."""
    target = Path(path) if path is not None else snapshot_path()
    json_target = json_path(target)
    target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    json_target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)

    all_entries = list(store.entries())
    active = [entry for entry in all_entries if entry.present]
    active.sort(key=lambda entry: _ip_sort_key(entry.ip))

    payload = build_snapshot(store, now=now, entries=all_entries)
    payload["pool_path"] = str(target)
    payload["json_path"] = str(json_target)

    _atomic_write(target, pool_text(active))
    _atomic_write(json_target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    mirrored = []
    text = pool_text(active)
    for mirror in mirror_paths():
        try:
            mirror.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _atomic_write(mirror, text, mode=MIRROR_MODE)
            mirrored.append(str(mirror))
        except OSError:
            # Зеркало —best effort: канонический файл уже записан.
            mirrored.append(f"{mirror}: ошибка записи")
    return {
        "ok": True,
        "pool_path": str(target),
        "json_path": str(json_target),
        "mirrors": mirrored,
        "count": payload["count"],
        "generated_at": payload["generated_at"],
    }


def _atomic_write(path: Path, text: str, *, mode: int = FILE_MODE) -> None:
    """Записать файл атомарно: временный файл рядом + os.replace."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def utc_stamp(stamp: float) -> str:
    """Время в UTC одной строкой — для читаемого снимка."""
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
