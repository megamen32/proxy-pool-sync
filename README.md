# proxy-pool-sync

Small async Python toolkit for applications that repeatedly need the same proxy-pool plumbing:

- pull a canonical newline-delimited proxy export over HTTP;
- atomically replace a local pool;
- normalize/deduplicate HTTP and SOCKS proxy URLs;
- choose or atomically lease a least-used healthy proxy;
- health-check a proxy by opening a TCP tunnel to a real destination;
- keep framework-agnostic health state with structured failure verdicts and cooldowns;
- reject suspicious upstream pool collapses before they overwrite a healthy pool;
- inject application-specific per-account assignment logic;
- accept secure refresh callbacks;
- notify downstream consumers when a producer's export changes.

The library intentionally does **not** know about Telegram, SQLAlchemy, FastAPI or your database. Those stay in application adapters.

## Install

```bash
pip install git+https://github.com/megamen32/proxy-pool-sync.git
```

## Consumer example

```python
from proxy_pool_sync import FileProxyPool, HttpProxySource, ProxyPoolManager

pool = FileProxyPool("proxies.txt")
source = HttpProxySource(
    "https://proxy-source.example/export",
    bearer_token="secret",
)

async def usage():
    # app-specific: return {proxy_url: number_of_accounts_using_it}
    return {}

async def assign(proxies):
    # app-specific: persist per-account proxy assignments
    return 0

manager = ProxyPoolManager(
    pool=pool,
    source=source,
    usage_provider=usage,
    assigner=assign,
    health_check=ProxyPoolManager.tcp_health_check(
        host="149.154.167.50",  # example destination
        port=443,
    ),
)

await manager.sync()
proxy = await manager.choose()
```

## Runtime leases and health policy

For per-account ownership, use a lease store instead of racing concurrent `choose()` calls:

```python
from proxy_pool_sync import (
    InMemoryHealthStore,
    InMemoryLeaseStore,
    PoolUpdatePolicy,
    ProxyPoolManager,
)

manager = ProxyPoolManager(
    pool=pool,
    source=source,
    lease_store=InMemoryLeaseStore(),  # use a DB/Redis adapter for multi-process deployments
    health_store=InMemoryHealthStore(),
    update_policy=PoolUpdatePolicy(min_count=100, max_drop_ratio=0.70),
)

lease = await manager.acquire(owner="telegram-account:42", ttl_seconds=300)
if lease is not None:
    try:
        use_proxy(lease.proxy_url)
    finally:
        await manager.release(lease)
```

Applications can report failures without coupling the library to Telegram or another client:

```python
from proxy_pool_sync import FailureKind, FailureVerdict

await manager.report_failure(
    lease.proxy_url,
    FailureVerdict(FailureKind.PROXY_AUTH, "proxy authentication failed", disable_immediately=True),
)
```

## Callback endpoint adapter

The library is framework-agnostic. In FastAPI, for example:

```python
from fastapi import Request, HTTPException
from proxy_pool_sync import callback_is_authorized

async def refresh(request: Request):
    if not callback_is_authorized(
        request.client.host,
        request.headers.get("Authorization"),
        bearer_token="your-callback-token",
    ):
        raise HTTPException(401)
    await manager.sync()
```

## Producer fanout

```python
from proxy_pool_sync import NotificationTarget, notify_targets

await notify_targets(
    [NotificationTarget("https://consumer.example/proxy-sync/refresh", auth_type="bearer", bearer_token="secret")],
    {"event": "proxy_export_changed"},
)
```

## Design rules

- Secrets are supplied by the embedding application, never stored by the library.
- An empty upstream export is rejected by default so a broken producer cannot wipe a healthy local pool.
- `PoolUpdatePolicy` can additionally reject unexpectedly small exports or excessive pool shrinkage.
- Lease and health persistence are protocols; the included in-memory stores are reference implementations for a single process.
- Per-account assignment is an injected adapter, so the package works with SQL, Redis, JSON, etc.
- Health checks verify actual reachability through the proxy instead of trusting a stale "alive" flag.

## License

MIT.

## Shared hop transport and account binding notifications

`transport` provides loopback-only SSH forwards, a scoped nftables renderer and a bounded TLS exit-IP probe. The caller owns installation and runtime budgets; the renderer rejects broad cgroup rules. `storage.write_private_text` atomically replaces credential files with mode 0600.

`BindingJournal` keeps one private durable outbox/projection **per application**, keyed by native Telegram user ID rather than a local database row or cabinet ID. `BindingPeer` exchanges credential-free events over an authenticated API. Delivery is idempotent, ordered by revision/source and coalesces obsolete queued assignments. Tokens and proxy credentials stay local. Remote peers require HTTPS; the reference HTTP server binds to loopback.

The receiver stores a preference. At its next idle connection, under its own account lock, it can call `binding_consumer.checked_preference`: only a candidate already in its own pool with a matching verified exit is returned. The caller must finish read transactions before network checks and enforce its existing native reservation/capacity checks before persisting. Busy connections are never interrupted. Missing peer API, unknown identity, unavailable exit or incompatible pool leaves the current assignment intact. An API acceptance is not an application receipt.

The exit probe verifies Internet egress; applications must also run their normal destination-specific connectivity check. This package does not perform Telegram authentication, send messages or transfer session keys.
