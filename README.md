# proxy-pool-sync

Small async Python toolkit for applications that repeatedly need the same proxy-pool plumbing:

- pull a canonical newline-delimited proxy export over HTTP;
- atomically replace a local pool;
- normalize/deduplicate HTTP and SOCKS proxy URLs;
- choose the least-used healthy proxy;
- health-check a proxy by opening a TCP tunnel to a real destination;
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
- Per-account assignment is an injected adapter, so the package works with SQL, Redis, JSON, etc.
- Health checks verify actual reachability through the proxy instead of trusting a stale "alive" flag.

## License

MIT.
