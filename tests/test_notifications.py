import asyncio

from proxy_pool_sync import notify_targets


def test_empty_notification_fanout():
    result = asyncio.run(notify_targets([], {"event": "changed"}))
    assert result.total == result.sent == result.failed == 0
