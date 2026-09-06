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


def test_callback_auth_accepts_basic_credentials():
    import base64
    token = base64.b64encode(b"alice:secret").decode()
    assert callback_is_authorized(
        "10.0.0.2",
        f"Basic {token}",
        bearer_token="",
        basic_username="alice",
        basic_password="secret",
    )
    assert not callback_is_authorized(
        "10.0.0.2",
        f"Basic {token}",
        bearer_token="",
        basic_username="alice",
        basic_password="wrong",
    )


def test_acquire_uses_atomic_leases_for_parallel_owners(tmp_path):
    from proxy_pool_sync import InMemoryLeaseStore

    pool = FileProxyPool(tmp_path / "leases.txt")
    pool.replace_lines(["http://1.1.1.1:80", "http://2.2.2.2:80"])
    manager = ProxyPoolManager(pool=pool, lease_store=InMemoryLeaseStore())

    async def run():
        return await asyncio.gather(
            manager.acquire(owner="a"),
            manager.acquire(owner="b"),
        )

    first, second = asyncio.run(run())
    assert first is not None and second is not None
    assert first.proxy_url != second.proxy_url


def test_release_makes_proxy_available_again(tmp_path):
    from proxy_pool_sync import InMemoryLeaseStore

    pool = FileProxyPool(tmp_path / "leases.txt")
    pool.replace_lines(["http://1.1.1.1:80"])
    manager = ProxyPoolManager(pool=pool, lease_store=InMemoryLeaseStore())

    async def run():
        first = await manager.acquire(owner="a")
        assert first is not None
        assert await manager.acquire(owner="b") is None
        assert await manager.release(first)
        return await manager.acquire(owner="b")

    assert asyncio.run(run()) is not None


def test_manager_health_store_filters_failed_proxy(tmp_path):
    from proxy_pool_sync import InMemoryHealthStore, ProxyHealthPolicy

    pool = FileProxyPool(tmp_path / "health.txt")
    pool.replace_lines(["http://1.1.1.1:80", "http://2.2.2.2:80"])
    store = InMemoryHealthStore()

    async def health(proxy):
        return proxy.endswith("2:80")

    manager = ProxyPoolManager(
        pool=pool,
        health_check=health,
        health_store=store,
        health_policy=ProxyHealthPolicy(max_failures_before_disable=1),
    )
    assert asyncio.run(manager.choose()) == "http://2.2.2.2:80"
    state = asyncio.run(store.get("http://1.1.1.1:80"))
    assert not state.is_active


def test_sync_result_reports_delta_and_safe_policy(tmp_path):
    from proxy_pool_sync import PoolUpdatePolicy

    pool = FileProxyPool(tmp_path / "sync.txt")
    pool.replace_lines(["http://1.1.1.1:80", "http://2.2.2.2:80"])
    manager = ProxyPoolManager(pool=pool, update_policy=PoolUpdatePolicy(max_drop_ratio=0.75))
    result = asyncio.run(manager.sync(pushed_lines=["http://2.2.2.2:80", "http://3.3.3.3:80"]))
    assert result.total == 2
    assert result.previous_total == 2
    assert result.added == 1
    assert result.removed == 1


def test_report_failure_uses_structured_verdict(tmp_path):
    from proxy_pool_sync import FailureKind, FailureVerdict, InMemoryHealthStore

    pool = FileProxyPool(tmp_path / "health.txt")
    pool.replace_lines(["http://1.1.1.1:80"])
    store = InMemoryHealthStore()
    manager = ProxyPoolManager(pool=pool, health_store=store)

    async def run():
        await manager.report_failure(
            "http://1.1.1.1:80",
            FailureVerdict(FailureKind.PROXY_AUTH, "407", disable_immediately=True),
        )
        return await store.get("http://1.1.1.1:80")

    state = asyncio.run(run())
    assert not state.is_active
    assert state.disabled_reason == "proxy_auth_failed"
