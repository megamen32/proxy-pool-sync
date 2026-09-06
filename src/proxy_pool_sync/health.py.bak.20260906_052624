from __future__ import annotations


async def tcp_tunnel_check(
    proxy_url: str,
    *,
    host: str,
    port: int,
    timeout: float = 6.0,
) -> bool:
    """Return True if the proxy can open a TCP tunnel to the destination."""
    try:
        from python_socks.async_.asyncio import Proxy

        proxy = Proxy.from_url(proxy_url)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False
