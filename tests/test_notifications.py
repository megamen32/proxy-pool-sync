import asyncio

from proxy_pool_sync import notify_targets


def test_empty_notification_fanout():
    result = asyncio.run(notify_targets([], {"event": "changed"}))
    assert result.total == result.sent == result.failed == 0


def test_normalize_notification_targets_supports_dicts_and_auth():
    from proxy_pool_sync import normalize_notification_targets
    warnings=[]
    targets=normalize_notification_targets([
        {"url":"http://local/cb","method":"post","auth_type":"none"},
        {"url":"https://remote/cb","auth_type":"bearer","auth":{"token":"secret"}},
        {"url":"","auth_type":"none"},
    ], on_warning=warnings.append)
    assert len(targets)==2
    assert targets[0].method == "POST"
    assert targets[1].bearer_token == "secret"
    assert warnings
