import asyncio

import pytest

from proxy_pool_sync import (
    AccountProxyUnavailable,
    AccountProxyValidation,
    NoAccountProxy,
    free_exit_candidates,
    select_account_proxy,
)


def test_keeps_valid_current_route_without_touching_other_candidates():
    checked = []
    bound = []

    async def validate(proxy):
        checked.append(proxy)
        return AccountProxyValidation(ok=True)

    async def bind(proxy):
        bound.append(proxy)
        return True

    selected = asyncio.run(select_account_proxy(
        current="old", candidates=["new"], validate=validate, bind=bind,
    ))
    assert selected == "old"
    assert checked == bound == ["old"]


def test_rotates_after_explicit_proxy_failure_and_skips_raced_exit():
    checked = []
    failed = []
    bound = []

    async def validate(proxy):
        checked.append(proxy)
        return AccountProxyValidation(ok=proxy != "old")

    async def bind(proxy):
        bound.append(proxy)
        return proxy == "free"

    async def report_failure(proxy):
        failed.append(proxy)

    selected = asyncio.run(select_account_proxy(
        current="old", candidates=["old", "raced", "free"],
        validate=validate, bind=bind, report_failure=report_failure,
    ))
    assert selected == "free"
    assert checked == ["old", "raced", "free"]
    assert failed == ["old"]
    assert bound == ["raced", "free"]


def test_account_failure_stops_before_another_proxy_or_binding():
    async def validate(proxy):
        return AccountProxyValidation(ok=False, account_unavailable=True)

    async def bind(proxy):
        pytest.fail("must not bind after authorization failure")

    with pytest.raises(AccountProxyUnavailable):
        asyncio.run(select_account_proxy(
            current="old", candidates=["new"], validate=validate, bind=bind,
        ))


def test_no_validated_free_exit_fails_closed():
    async def validate(proxy):
        return AccountProxyValidation(ok=True)

    async def bind(proxy):
        return False

    with pytest.raises(NoAccountProxy):
        asyncio.run(select_account_proxy(
            current=None, candidates=["busy"], validate=validate, bind=bind,
        ))


def test_free_exit_candidates_exclude_busy_and_sample_all_of_large_pool():
    proxies = [f"local:{index}" for index in range(2000)]
    usage = {"local:0": 1}
    candidates = free_exit_candidates(
        proxies, usage, exit_key=lambda value: value if value != "local:1" else None,
        busy_exits={"local:2"}, limit=64,
    )
    assert len(candidates) == 64
    assert "local:0" not in candidates
    assert "local:1" not in candidates
    assert "local:2" not in candidates
    assert any(int(value.split(":")[1]) > 1900 for value in candidates)


def test_free_exit_candidates_preserve_minimum_density_and_unique_urls():
    candidates = free_exit_candidates(
        ["a", "a", "b", "c"], {"a": 1, "b": 2, "c": 0},
        exit_key=lambda value: value, max_accounts=2,
    )
    assert candidates == ["c", "a"]
    assert free_exit_candidates(
        ["a", "b", "c"], {"a": 1, "b": 0, "c": 0},
        exit_key=lambda value: value, max_accounts=2, limit=2,
    ) == ["b", "c"]
