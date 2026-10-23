"""HTTP-выдача пула proxy5 на loopback.

Путь файла `pool.txt` (строки `http://<ip>:8085`) пишет `sync.py` после
сверки с кабинетом; здесь он раздаётся по HTTP четырём потребителям:
Video Watching, TGC, Out of Find Client, ATV.

Особенности:
  * только loopback (bind 127.0.0.1) — авторизация не нужна и не делается;
  * отдаётся файл с диска, а не пересобирается на каждый запрос: выдача
    быстрая и не зависит ни от кабинета, ни от инвентаря;
  * только stdlib — новых зависимостей в проекте не появляется.

Токены и ключи в ответах не участвуют: наружу уходят только адреса пула,
гео и счётчики.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import snapshot as snap
from .inventory import InventoryStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18102
SERVER_NAME = "proxy5-pool"


def serve_port() -> int:
    """Порт выдачи: PROXY5_SERVE_PORT либо дефолт."""
    raw = os.environ.get("PROXY5_SERVE_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def open_store(path: str | None = None) -> InventoryStore | None:
    """Инвентарь для /health. None, если БД недоступен."""
    target = path if path is not None else os.environ.get("PROXY5_INVENTORY_DB", "").strip()
    try:
        return InventoryStore(target or None)
    except Exception:  # noqa: BLE001 — выдача должна жить и без инвентаря
        return None


class PoolHandler(BaseHTTPRequestHandler):
    """Раздача snapshot-файла пула."""

    server_version = f"{SERVER_NAME}/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        path = urlsplit(self.path).path
        if path == "/pool":
            self._serve_pool()
        elif path == "/pool.json":
            self._serve_json()
        elif path == "/health":
            self._serve_health()
        elif path == "/":
            self._serve_index()
        else:
            self._reply(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 — имя задано базовым классом
        self.do_GET()

    # ---------- маршруты ----------

    def _serve_pool(self) -> None:
        path = self.server.pool_path
        try:
            data = path.read_bytes()
        except OSError:
            self._reply(503, self._missing_text(path), "text/plain; charset=utf-8")
            return
        count = sum(
            1 for line in data.decode("utf-8", "replace").splitlines() if line.strip()
        )
        self._reply(
            200,
            data,
            "text/plain; charset=utf-8",
            {"X-Proxy-Pool-Count": str(count)},
        )

    def _serve_json(self) -> None:
        path = self.server.json_path
        try:
            data = path.read_bytes()
        except OSError:
            self._reply(503, self._missing_text(path), "application/json; charset=utf-8")
            return
        self._reply(200, data, "application/json; charset=utf-8")

    def _serve_health(self) -> None:
        store = self.server.store
        total = 0
        present = 0
        last_sync = None
        degraded = True
        if store is not None:
            try:
                entries = list(store.entries())
                total = len(entries)
                present = sum(1 for entry in entries if entry.present)
                raw = store.meta("last_sync_at")
                last_sync = float(raw) if raw else None
                degraded = total == 0
            except Exception:  # noqa: BLE001 — health не должен падать
                degraded = True
        pool_path = self.server.pool_path
        payload = {
            "status": "degraded" if degraded else "ok",
            "degraded": degraded,
            "inventory_total": total,
            "inventory_present": present,
            "last_sync_at": last_sync,
            "last_sync_at_utc": snap.utc_stamp(last_sync) if last_sync else None,
            "pool_file": str(pool_path),
            "pool_file_exists": pool_path.exists(),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._reply(200, body, "application/json; charset=utf-8")

    def _serve_index(self) -> None:
        body = (
            "proxy5 pool registry — HTTP-выдача пула proxy5\n"
            "\n"
            "Пул ведёт единый реестр proxy-pool-sync: инвентарь всех\n"
            "выданных адресов, гео по каждому адресу, история ротаций.\n"
            "\n"
            "Маршруты:\n"
            "  GET /pool       список прокси, одна строка на адрес: http://<ip>:8085\n"
            "  GET /pool.json  тот же пул с гео и метаданными\n"
            "  GET /health     счётчики инвентаря и время последней сверки\n"
            "\n"
            f"Файл пула: {self.server.pool_path}\n"
            "Источник: инвентарь proxy5-registry, обновляется таймером\n"
            "proxy5-registry-sync (python -m proxy_pool_sync.proxy5.sync).\n"
            "Доступ: только с loopback (127.0.0.1).\n"
        ).encode("utf-8")
        self._reply(200, body, "text/plain; charset=utf-8")

    # ---------- низкий уровень ----------

    def _missing_text(self, path: Path) -> bytes:
        return (
            "proxy5 pool snapshot is not available\n"
            f"file: {path}\n"
            "write it: python -m proxy_pool_sync.proxy5.sync\n"
        ).encode("utf-8")

    def _reply(
        self,
        code: int,
        body: bytes,
        content_type: str,
        headers: dict | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


class PoolServer(ThreadingHTTPServer):
    """HTTP-сервер выдачи пула."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        *,
        pool_path: Path,
        json_path: Path,
        store: InventoryStore | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.pool_path = Path(pool_path)
        self.json_path = Path(json_path)
        self.store = store


def build_server(
    host: str = DEFAULT_HOST,
    port: int | None = None,
    *,
    pool_path: "str | os.PathLike[str] | None" = None,
    json_path: "str | os.PathLike[str] | None" = None,
    store: InventoryStore | None = None,
) -> PoolServer:
    """Собрать сервер выдачи (порт 0 — свободный порт, для тестов)."""
    pool = Path(pool_path) if pool_path is not None else snap.snapshot_path()
    return PoolServer(
        (host, serve_port() if port is None else port),
        PoolHandler,
        pool_path=pool,
        json_path=Path(json_path) if json_path is not None else snap.json_path(pool),
        store=store,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = DEFAULT_HOST
    port = serve_port()
    server = build_server(host, port, store=open_store())
    sys.stderr.write(
        f"proxy5 pool serve: http://{host}:{port}/pool "
        f"file={server.pool_path} json={server.json_path}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
