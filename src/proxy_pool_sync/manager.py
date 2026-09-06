from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .health import FailureVerdict, ProxyHealthPolicy, ProxyHealthState, TunnelCheckResult, tcp_tunnel_check
from .pool import FileProxyPool, PoolUpdatePolicy
from .source import HttpProxySource

UsageProvider = Callable[[], Awaitable[Mapping[str, int]]]
Assigner = Callable[[list[str]], Awaitable[int]]
HealthCheck = Callable[[str], Awaitable[bool]]
HealthProbe = Callable[[str], Awaitable[bool | TunnelCheckResult]]


@dataclass(frozen=True, slots=True)
class ProxyLease:
    proxy_url: str
    owner: str
    acquired_at: float
    expires_at: float


class LeaseStore(Protocol):
    async def acquire(self, proxy_url: str, *, owner: str, now: float, ttl_seconds: float) -> ProxyLease | None: ...
    async def release(self, proxy_url: str, *, owner: str) -> bool: ...


class HealthStore(Protocol):
    async def get(self, proxy_url: str) -> ProxyHealthState: ...
    async def save(self, proxy_url: str, state: ProxyHealthState) -> None: ...


class InMemoryLeaseStore:
    """Reference lease store for one process. Multi-process apps should use DB/Redis."""

    def __init__(self):
        self._leases: dict[str, ProxyLease] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, proxy_url: str, *, owner: str, now: float, ttl_seconds: float) -> ProxyLease | None:
        async with self._lock:
            current = self._leases.get(proxy_url)
            if current is not None and current.expires_at > now and current.owner != owner:
                return None
            lease = ProxyLease(proxy_url, owner, float(now), float(now) + max(0.0, float(ttl_seconds)))
            self._leases[proxy_url] = lease
            return lease

    async def release(self, proxy_url: str, *, owner: str) -> bool:
        async with self._lock:
            current = self._leases.get(proxy_url)
            if current is None or current.owner != owner:
                return False
            del self._leases[proxy_url]
            return True


class InMemoryHealthStore:
    def __init__(self):
        self._states: dict[str, ProxyHealthState] = {}
        self._lock = asyncio.Lock()

    async def get(self, proxy_url: str) -> ProxyHealthState:
        async with self._lock:
            state = self._states.get(proxy_url)
            if state is None:
                state = ProxyHealthState()
                self._states[proxy_url] = state
            return state

    async def save(self, proxy_url: str, state: ProxyHealthState) -> None:
        async with self._lock:
            self._states[proxy_url] = state


@dataclass(frozen=True, slots=True)
class ProxySyncResult:
    total: int
    assigned: int = 0
    previous_total: int = 0
    added: int = 0
    removed: int = 0


class ProxyPoolManager:
    """Synchronize a canonical pool and select or lease healthy proxies."""

    def __init__(
        self,
        *,
        pool: FileProxyPool,
        source: HttpProxySource | None = None,
        usage_provider: UsageProvider | None = None,
        assigner: Assigner | None = None,
        health_check: HealthCheck | None = None,
        health_probe: HealthProbe | None = None,
        health_store: HealthStore | None = None,
        health_policy: ProxyHealthPolicy | None = None,
        lease_store: LeaseStore | None = None,
        update_policy: PoolUpdatePolicy | None = None,
    ):
        self.pool = pool
        self.source = source
        self.usage_provider = usage_provider
        self.assigner = assigner
        self.health_check = health_check
        self.health_probe = health_probe
        self.health_store = health_store
        self.health_policy = health_policy or ProxyHealthPolicy()
        self.lease_store = lease_store
        self.update_policy = update_policy
        self._sync_lock = asyncio.Lock()

    async def pull(self, *, force: bool = False) -> list[str]:
        if self.source is None:
            raise RuntimeError("no proxy source configured")
        return self.pool.replace_text(
            await self.source.fetch_text(),
            update_policy=self.update_policy,
            force=force,
        )

    async def sync(self, *, pushed_lines: list[str] | None = None, force: bool = False) -> ProxySyncResult:
        async with self._sync_lock:
            previous = self.pool.load()
            proxies = (
                self.pool.replace_lines(pushed_lines, update_policy=self.update_policy, force=force)
                if pushed_lines is not None
                else await self.pull(force=force)
            )
            assigned = await self.assigner(proxies) if self.assigner is not None else 0
            old, new = set(previous), set(proxies)
            return ProxySyncResult(
                total=len(proxies),
                assigned=assigned,
                previous_total=len(previous),
                added=len(new - old),
                removed=len(old - new),
            )

    async def _ordered_candidates(self, *, candidate_limit: int) -> list[str]:
        proxies = self.pool.load()
        if not proxies and self.source is not None:
            proxies = await self.pull()
        if not proxies:
            return []
        usage = dict(await self.usage_provider()) if self.usage_provider is not None else {}
        candidates = sorted(proxies, key=lambda item: int(usage.get(item, 0)))
        if self.health_store is not None:
            now = time.time()
            with_states: list[tuple[str, ProxyHealthState]] = []
            for proxy in candidates:
                state = await self.health_store.get(proxy)
                if self.health_policy.eligible(state, now=now, require_unused=False):
                    with_states.append((proxy, state))
            candidates = [item[0] for item in sorted(
                with_states,
                key=lambda pair: (int(usage.get(pair[0], 0)), self.health_policy.candidate_sort_key(pair[1])),
            )]
        return candidates[:candidate_limit]

    async def _probe(self, proxy_url: str, *, mark_used: bool) -> bool:
        result: bool | TunnelCheckResult
        if self.health_probe is not None:
            result = await self.health_probe(proxy_url)
        elif self.health_check is not None:
            result = await self.health_check(proxy_url)
        else:
            result = True

        ok = result.ok if isinstance(result, TunnelCheckResult) else bool(result)
        if self.health_store is not None:
            now = time.time()
            state = await self.health_store.get(proxy_url)
            if ok:
                self.health_policy.mark_success(state, now=now, mark_used=mark_used)
            else:
                verdict = result.to_verdict() if isinstance(result, TunnelCheckResult) else FailureVerdict(
                    kind=self._default_failure_kind(),
                    message="health check failed",
                )
                assert verdict is not None
                self.health_policy.mark_verdict(state, now=now, verdict=verdict)
            await self.health_store.save(proxy_url, state)
        return ok

    @staticmethod
    def _default_failure_kind():
        from .health import FailureKind
        return FailureKind.PROXY_CONNECT

    async def choose(self, *, candidate_limit: int = 20, batch_size: int = 5) -> str | None:
        candidates = await self._ordered_candidates(candidate_limit=candidate_limit)
        if self.health_probe is None and self.health_check is None:
            return candidates[0] if candidates else None
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            checks = await asyncio.gather(*(self._probe(item, mark_used=False) for item in batch))
            for item, ok in zip(batch, checks):
                if ok:
                    if self.health_store is not None:
                        state = await self.health_store.get(item)
                        state.last_used = time.time()
                        await self.health_store.save(item, state)
                    return item
        return None

    async def acquire(
        self,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
        candidate_limit: int = 50,
    ) -> ProxyLease | None:
        """Atomically lease one healthy proxy when a lease store is configured."""
        if self.lease_store is None:
            raise RuntimeError("no lease store configured")
        for proxy in await self._ordered_candidates(candidate_limit=candidate_limit):
            if not await self._probe(proxy, mark_used=False):
                continue
            now = time.time()
            lease = await self.lease_store.acquire(proxy, owner=owner, now=now, ttl_seconds=ttl_seconds)
            if lease is None:
                continue
            if self.health_store is not None:
                state = await self.health_store.get(proxy)
                state.last_used = now
                await self.health_store.save(proxy, state)
            return lease
        return None

    async def release(self, lease: ProxyLease) -> bool:
        if self.lease_store is None:
            raise RuntimeError("no lease store configured")
        return await self.lease_store.release(lease.proxy_url, owner=lease.owner)

    async def report_failure(self, proxy_url: str, verdict: FailureVerdict) -> None:
        if self.health_store is None:
            return
        state = await self.health_store.get(proxy_url)
        self.health_policy.mark_verdict(state, now=time.time(), verdict=verdict)
        await self.health_store.save(proxy_url, state)

    async def report_success(self, proxy_url: str) -> None:
        if self.health_store is None:
            return
        state = await self.health_store.get(proxy_url)
        self.health_policy.mark_success(state, now=time.time(), mark_used=False)
        await self.health_store.save(proxy_url, state)

    async def periodic_sync(
        self,
        *,
        interval_seconds: float,
        on_error: Callable[[Exception], Awaitable[None] | None] | None = None,
    ) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if on_error is not None:
                    result = on_error(exc)
                    if result is not None:
                        await result
            await asyncio.sleep(interval_seconds)

    @staticmethod
    def tcp_health_check(*, host: str, port: int, timeout: float = 6.0) -> HealthCheck:
        async def check(proxy_url: str) -> bool:
            return await tcp_tunnel_check(proxy_url, host=host, port=port, timeout=timeout)
        return check
