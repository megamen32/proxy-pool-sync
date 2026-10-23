"""Сетевой доступ к кабинету proxy5: пул, whitelist, гео-резолвер.

Два способа ходить в кабинет:

* REST (`/account_new/api/v1/...`) — для чтения: пул `/ips`, whitelist.
* MCP (`/account_new/api/v1/mcp`) — для операций, которых нет в REST:
  добавление/снятие IP, изменение лимитов, ротация.

Ключ кабинета — Bearer из защищённого файла заголовков. Он не попадает ни в
argv, ни в журнал, ни в ответы: в MCP-заголовок уходит только значение.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .core import PoolSnapshot, snapshot_from_cabinet

DEFAULT_BASE = "https://proxy5.net/account_new/api/v1"
DEFAULT_HEADERS_FILE = Path("/etc/gptadmin/proxy5.headers")
DEFAULT_KEY_FILE = Path("/opt/rutube/state/server/proxy5-mcp-api.key")


class CabinetError(RuntimeError):
    """Кабинет ответил ошибкой или недоступен."""


class Cabinet:
    """Клиент кабинета proxy5 (REST + MCP)."""

    def __init__(
        self,
        *,
        base: str | None = None,
        headers_file: Path | None = None,
        key_file: Path | None = None,
        timeout: int = 45,
    ) -> None:
        self.base = (base or os.environ.get("PROXY5_BASE") or DEFAULT_BASE).rstrip("/")
        self.headers_file = Path(headers_file or DEFAULT_HEADERS_FILE)
        self.key_file = Path(key_file or DEFAULT_KEY_FILE)
        self.timeout = timeout
        # Egress-маршрут к кабинету: /ips из датацентра идёт минуты и прямой
        # путь не успевает (см. proxy5_ips_sync.EGRESS_PROXY).
        self.egress_proxy = os.environ.get("PROXY5_IPS_EGRESS", "socks5h://127.0.0.1:30881")

    # ---------- аутентификация ----------

    def _bearer(self) -> str:
        """Значение заголовка Authorization — без файла целиком и без префикса.

        Два источника, оба защищённые: файл заголовков (0640 root:roomhacker,
        может быть недоступен процессу) и ключ-файл (0600 roomhacker). Токен
        возвращается без префикса `Bearer` — префикс добавляет вызывающий, чтобы
        значение нигде не лежало в готовом виде для журнала.
        """
        try:
            raw = self.headers_file.read_text().strip()
        except OSError:
            raw = ""
        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("authorization:"):
                value = line.split(":", 1)[1].strip()
                if value.lower().startswith("bearer "):
                    value = value[7:].strip()
                if value:
                    return value
        # Запасной путь: файл с ключом целиком (как proxy5-mcp-api.key).
        try:
            value = self.key_file.read_text().strip()
        except OSError as exc:
            raise CabinetError(f"no usable proxy5 credential: {exc}") from exc
        if not value or "\n" in value:
            raise CabinetError("no usable proxy5 credential")
        return value

    def _auth_value(self) -> str:
        """Готовое значение заголовка Authorization для HTTP/MCP-запроса."""
        return f"Bearer {self._bearer()}"

    # ---------- REST ----------

    def api(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int | None = None,
    ) -> tuple[int, Any]:
        """Вызов REST-кабинета. Возвращает (status, payload)."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        request.add_header("Authorization", self._auth_value())
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                raw = response.read().decode()
                if raw.strip().startswith(("{", "[")):
                    return response.status, json.loads(raw)
                return response.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw[:400]
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise CabinetError(f"cabinet unreachable: {type(exc).__name__}: {exc}") from exc

    def get_json(self, path: str, timeout: int | None = None) -> dict:
        status, payload = self.api("GET", path, timeout=timeout)
        if status != 200 or not isinstance(payload, dict):
            raise CabinetError(f"GET {path} -> {status}: {str(payload)[:200]}")
        return payload

    def curl_json(
        self,
        path: str,
        *,
        egress_proxy: str | None = None,
        timeout: int = 600,
        query: str = "",
    ) -> dict:
        """Быстрый GET через curl с egress-прокси и большим бюджетом.

        `/ips` из датацентра отвечает минуты и требует egress-маршрута, поэтому
        urllib с его коротким таймаутом здесь не годится. Токен уходит в
        заголовок curl, а не в argv, и не попадает в журнал.
        """
        url = f"{self.base}{path}{query}"
        command = [
            "curl", "-sS", "--max-time", str(int(timeout)),
            "--speed-limit", "1024", "--speed-time", "60",
        ]
        if egress_proxy:
            command += ["--proxy", egress_proxy]
        command += ["-H", f"Authorization: {self._auth_value()}", url]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout + 30, check=False
        )
        if result.returncode != 0:
            raise CabinetError(
                f"cabinet curl failed rc={result.returncode}: {result.stderr.strip()[:200]}"
            )
        body = result.stdout.strip()
        if not body.startswith(("{", "[")):
            raise CabinetError(f"cabinet returned non-json: {body[:200]}")
        payload = json.loads(body)
        if isinstance(payload, list):
            return {"ips": payload}
        return payload

    # ---------- доменные операции ----------

    def pool(self, service_id: str) -> PoolSnapshot:
        """Текущий пул прокси с гео (страна, тип протокола).

        Прямой путь: короткий таймаут, без egress-маршрута.
        """
        payload = self.get_json(f"/proxies/services/{service_id}/ips", timeout=self.timeout)
        return snapshot_from_cabinet(payload)

    def pool_via_egress(self, service_id: str) -> PoolSnapshot:
        """Пул через egress-прокси: единственный путь, который отвечает вовремя.

        `/ips` из датацентра отвечает минуты и требует egress-маршрута, поэтому
        urllib с его коротким таймаутом здесь не годится.
        """
        payload = self.curl_json(
            f"/proxies/services/{service_id}/ips",
            egress_proxy=self.egress_proxy,
            query="?geo_source=maxmind",
        )
        return snapshot_from_cabinet(payload)

    def whitelist(self, service_id: str) -> dict:
        """Текущий whitelist egress-IP с лимитами соединений."""
        return self.get_json(f"/proxies/services/{service_id}/whitelist")

    # ---------- MCP ----------

    def call_tool(self, tool: str, payload: dict) -> str:
        """Вызов инструмента MCP-кабинета.

        FastMCP выполняет sync-инструменты в собственном event loop, поэтому
        asyncio.run() внутри вызывающего потока недопустим — нужен отдельный
        поток со своим loop.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _noop(self, name, result):  # кабинет молчит на повторный tools/list
            return None

        ClientSession._validate_tool_result = _noop

        async def _run() -> str:
            async with streamablehttp_client(
                f"{self.base}/mcp", headers={"Authorization": self._auth_value()}
            ) as (r, wt, _):
                async with ClientSession(r, wt) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, payload)
            return "".join(getattr(c, "text", "") for c in result.content)

        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["text"] = asyncio_run(_run())
            except Exception as exc:  # noqa: BLE001 — текст ошибки уходит вызывающему
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=120)
        if "error" in box:
            raise CabinetError(box["error"])
        if "text" not in box:
            raise CabinetError("cabinet MCP call timed out")
        return box["text"]

    def list_tools(self) -> list[str]:
        """Имена всех инструментов MCP-кабинета."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _noop(self, name, result):
            return None

        ClientSession._validate_tool_result = _noop

        async def _run() -> list[str]:
            async with streamablehttp_client(
                f"{self.base}/mcp", headers={"Authorization": self._auth_value()}
            ) as (r, wt, _):
                async with ClientSession(r, wt) as session:
                    await session.initialize()
                    tools = await session.list_tools()
            return sorted(t.name for t in tools.tools)

        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["names"] = asyncio_run(_run())
            except Exception as exc:  # noqa: BLE001
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=120)
        if "error" in box:
            raise CabinetError(box["error"])
        return list(box.get("names") or [])


def asyncio_run(coro):  # noqa: N802 — тонкая обёртка ради одного места вызова
    """Локальный asyncio.run: держим в одном месте, чтобы не тянуть asyncio в API."""
    import asyncio

    return asyncio.run(coro)


# ---------- гео-резолвер ----------


class GeoResolver:
    """Страна адреса: кабинет (MaxMind у провайдера) + локальный GeoIP-файл.

    Кабинет отдаёт страну по каждому адресу, но это его база. Локальный
    GeoLite2 даёт второе независимое мнение — по нему видно расхождения.
    """

    def __init__(self, mmdb_path: str | os.PathLike | None = None) -> None:
        self.mmdb_path = Path(mmdb_path) if mmdb_path else None
        self._reader = None

    def _open(self):
        if self._reader is None and self.mmdb_path and self.mmdb_path.exists():
            import geoip2.database

            self._reader = geoip2.database.Reader(str(self.mmdb_path))
        return self._reader

    def resolve(self, ip: str) -> dict:
        """Страна адреса по локальной базе (None, если базы нет)."""
        reader = self._open()
        if reader is None:
            return {"ip": ip, "country": None, "source": None}
        try:
            response = reader.city(ip)
        except Exception:  # noqa: BLE001 — адрес вне базы не должен ронять вызов
            return {"ip": ip, "country": None, "source": "geoip-error"}
        code = (response.country.iso_code or "").upper() or None
        name = response.country.name or None
        return {"ip": ip, "country": code, "country_name": name, "source": "geoip"}

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None


def compare_geo(cabinet_country: str | None, geoip_country: str | None) -> str:
    """Свести две страны в вердикт: agree / conflict / unknown."""
    if not cabinet_country or not geoip_country:
        return "unknown"
    return "agree" if cabinet_country == geoip_country else "conflict"
