from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from .health import tcp_tunnel_check
from .pool import FileProxyPool
from .source import HttpProxySource

UsageProvider = Callable[[], Awaitable[Mapping[str, int]]]
Assigner = Callable[[list[str]], Awaitable[int]]
HealthCheck = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ProxySyncResult:
    total: int
    assigned: int = 0


class ProxyPoolManager:
    """Synchronize a canonical pool and select least-used healthy proxies.

    Application-specific account assignment is injected through ``assigner``;
    the library itself does not depend on a database or ORM.
    """

    def __init__(
        self,
        *,
        pool: FileProxyPool,
        source: HttpProxySource | None = None,
        usage_provider: UsageProvider | None = None,
        assigner: Assigner | None = None,
        health_check: HealthCheck | None = None,
    ):
        self.pool = pool
        self.source = source
        self.usage_provider = usage_provider
        self.assigner = assigner
        self.health_check = health_check
        self._sync_lock = asyncio.Lock()

    async def pull(self) -> list[str]:
        if self.source is None:
            raise RuntimeError("no proxy source configured")
        return self.pool.replace_text(await self.source.fetch_text())

    async def sync(self, *, pushed_lines: list[str] | None = None) -> ProxySyncResult:
        async with self._sync_lock:
            proxies = self.pool.replace_lines(pushed_lines) if pushed_lines is not None else await self.pull()
            assigned = await self.assigner(proxies) if self.assigner is not None else 0
            return ProxySyncResult(total=len(proxies), assigned=assigned)

    async def choose(
        self,
        *,
        candidate_limit: int = 20,
        batch_size: int = 5,
    ) -> str | None:
        proxies = self.pool.load()
        if not proxies and self.source is not None:
            proxies = await self.pull()
        if not proxies:
            return None

        usage = dict(await self.usage_provider()) if self.usage_provider is not None else {}
        candidates = sorted(proxies, key=lambda item: int(usage.get(item, 0)))[:candidate_limit]
        if self.health_check is None:
            return candidates[0] if candidates else None

        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            checks = await asyncio.gather(*(self.health_check(item) for item in batch))
            for item, ok in zip(batch, checks):
                if ok:
                    return item
        return None

    async def periodic_sync(self, *, interval_seconds: float) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Deliberately swallowed: callers may wrap this coroutine and log.
                pass
            await asyncio.sleep(interval_seconds)

    @staticmethod
    def tcp_health_check(*, host: str, port: int, timeout: float = 6.0) -> HealthCheck:
        async def check(proxy_url: str) -> bool:
            return await tcp_tunnel_check(proxy_url, host=host, port=port, timeout=timeout)

        return check
