from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TunnelCheckResult:
    ok: bool
    error_type: str | None = None
    error_message: str | None = None


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
