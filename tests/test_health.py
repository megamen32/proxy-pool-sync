import asyncio
from proxy_pool_sync import tcp_tunnel_probe


def test_probe_keeps_failure_detail():
    result=asyncio.run(tcp_tunnel_probe("http://127.0.0.1:1", host="127.0.0.1", port=9, timeout=0.05))
    assert result.ok is False
    assert result.error_type
