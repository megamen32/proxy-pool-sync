import asyncio

from proxy_pool_sync import FileProxyPool, ProxyPoolManager, callback_is_authorized


def test_choose_least_used_healthy(tmp_path):
    pool = FileProxyPool(tmp_path / "p.txt")
    pool.replace_lines(["http://1.1.1.1:1", "http://2.2.2.2:2", "http://3.3.3.3:3"])

    async def usage():
        return {"http://1.1.1.1:1": 5, "http://2.2.2.2:2": 0, "http://3.3.3.3:3": 1}

    async def health(proxy):
        return proxy.endswith(":3")

    manager = ProxyPoolManager(pool=pool, usage_provider=usage, health_check=health)
    assert asyncio.run(manager.choose()) == "http://3.3.3.3:3"


def test_callback_auth():
    assert callback_is_authorized("127.0.0.1", None)
    assert callback_is_authorized("10.0.0.2", "Bearer x", bearer_token="x")
    assert not callback_is_authorized("10.0.0.2", "Bearer bad", bearer_token="x")
