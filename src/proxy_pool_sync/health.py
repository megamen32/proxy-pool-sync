from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FailureKind(str, Enum):
    """Portable failure categories understood by the common rotation policy."""

    PROXY_CONNECT = "proxy_connect_failed"
    PROXY_AUTH = "proxy_auth_failed"
    DESTINATION_TIMEOUT = "destination_timeout"
    DESTINATION_REJECTED = "destination_rejected"
    RATE_LIMITED = "rate_limited"
    ACCOUNT = "account_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureVerdict:
    kind: FailureKind
    message: str | None = None
    counts_toward_disable: bool = True
    disable_immediately: bool = False
    cooldown_seconds: float = 0.0

    @classmethod
    def account_error(cls, message: str | None = None) -> "FailureVerdict":
        return cls(FailureKind.ACCOUNT, message, counts_toward_disable=False)


@dataclass(frozen=True, slots=True)
class TunnelCheckResult:
    ok: bool
    error_type: str | None = None
    error_message: str | None = None

    def to_verdict(self) -> FailureVerdict | None:
        if self.ok:
            return None
        name = (self.error_type or "").lower()
        message = self.error_message
        if "auth" in name or "407" in (message or ""):
            return FailureVerdict(FailureKind.PROXY_AUTH, message, disable_immediately=True)
        if "timeout" in name:
            return FailureVerdict(FailureKind.DESTINATION_TIMEOUT, message, cooldown_seconds=30.0)
        return FailureVerdict(FailureKind.PROXY_CONNECT, message, cooldown_seconds=10.0)


async def tcp_tunnel_probe(
    proxy_url: str,
    *,
    host: str,
    port: int,
    timeout: float = 6.0,
) -> TunnelCheckResult:
    """Probe a real TCP tunnel and retain failure detail for application logs."""
    try:
        from python_socks.async_.asyncio import Proxy

        proxy = Proxy.from_url(proxy_url)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=timeout)
        sock.close()
        return TunnelCheckResult(ok=True)
    except Exception as exc:
        return TunnelCheckResult(
            ok=False,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


async def tcp_tunnel_check(
    proxy_url: str,
    *,
    host: str,
    port: int,
    timeout: float = 6.0,
) -> bool:
    """Return True if the proxy can open a TCP tunnel to the destination."""
    return (await tcp_tunnel_probe(proxy_url, host=host, port=port, timeout=timeout)).ok


@dataclass(slots=True)
class ProxyHealthState:
    """Framework-agnostic health/rotation state for one proxy endpoint."""

    is_active: bool = True
    fail_count: int = 0
    last_ok: float = 0.0
    last_check: float = 0.0
    last_used: float = 0.0
    last_error: str | None = None
    disabled_reason: str | None = None
    reserved_until: float = 0.0
    uses: int = 0


@dataclass(frozen=True, slots=True)
class ProxyHealthPolicy:
    """Deterministic health/rotation transitions shared by proxy consumers."""

    max_failures_before_disable: int = 5
    preserve_disabled_prefixes: tuple[str, ...] = ("absent_from_import_",)

    def mark_success(self, state: ProxyHealthState, *, now: float, mark_used: bool = True) -> ProxyHealthState:
        state.is_active = True
        state.last_ok = float(now)
        state.last_check = float(now)
        if mark_used:
            state.last_used = float(now)
        state.fail_count = 0
        state.last_error = None
        if not (
            state.disabled_reason
            and any(state.disabled_reason.startswith(prefix) for prefix in self.preserve_disabled_prefixes)
        ):
            state.disabled_reason = None
        state.reserved_until = 0.0
        return state

    def mark_failure(
        self,
        state: ProxyHealthState,
        *,
        now: float,
        error: str | None,
        counts_toward_disable: bool,
        disabled_reason: str | None = None,
    ) -> ProxyHealthState:
        verdict = FailureVerdict(
            FailureKind.UNKNOWN,
            message=error,
            counts_toward_disable=counts_toward_disable,
        )
        return self.mark_verdict(state, now=now, verdict=verdict, disabled_reason=disabled_reason)

    def mark_verdict(
        self,
        state: ProxyHealthState,
        *,
        now: float,
        verdict: FailureVerdict,
        disabled_reason: str | None = None,
    ) -> ProxyHealthState:
        state.last_check = float(now)
        state.last_error = verdict.message or verdict.kind.value
        if disabled_reason:
            state.disabled_reason = disabled_reason
        if verdict.counts_toward_disable:
            state.fail_count = int(state.fail_count or 0) + 1
        if verdict.cooldown_seconds > 0:
            state.reserved_until = max(
                float(state.reserved_until or 0.0),
                float(now) + float(verdict.cooldown_seconds),
            )
        if verdict.disable_immediately or int(state.fail_count or 0) >= int(self.max_failures_before_disable):
            state.is_active = False
            if not state.disabled_reason:
                state.disabled_reason = verdict.kind.value if verdict.disable_immediately else "too_many_failures"
        return state

    def reserve(self, state: ProxyHealthState, *, now: float, seconds: float) -> ProxyHealthState:
        state.reserved_until = float(now) + max(0.0, float(seconds))
        return state

    def eligible(self, state: ProxyHealthState, *, now: float, require_unused: bool = True) -> bool:
        if not state.is_active:
            return False
        if float(state.reserved_until or 0.0) > float(now):
            return False
        if require_unused and int(state.uses or 0) > 0:
            return False
        if int(state.fail_count or 0) >= int(self.max_failures_before_disable):
            return False
        return True

    @staticmethod
    def candidate_sort_key(state: ProxyHealthState) -> tuple[int, float, float]:
        return (
            int(state.fail_count or 0),
            float(state.last_used or 0.0),
            float(state.last_check or 0.0),
        )
