"""Shared sticky proxy selection for one independently authorized account."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountProxyValidation:
    """The consumer's verdict for a proxy and its own authorization."""

    ok: bool
    account_unavailable: bool = False


class AccountProxyUnavailable(RuntimeError):
    """The account itself cannot be used; changing proxies would not help."""


class NoAccountProxy(RuntimeError):
    """No validated proxy could be bound to the account."""


def free_exit_candidates(
    proxies: Iterable[str],
    usage: Mapping[str, int],
    *,
    exit_key: Callable[[str], str | None],
    busy_exits: Iterable[str] = (),
    max_accounts: int = 1,
    limit: int | None = None,
) -> list[str]:
    """Rank unoccupied exits and spread bounded probes across the whole pool.

    ``exit_key`` maps a local listener to its physical endpoint or observed
    exit IP. Unknown mappings return ``None`` and are excluded. The caller
    must still verify the actual exit after probing and reserve it atomically.
    """

    if max_accounts < 1 or (limit is not None and limit < 1):
        raise ValueError("max_accounts and limit must be positive")
    busy = set(busy_exits)
    unique = dict.fromkeys(proxies)
    candidates = [proxy for proxy in unique if int(usage.get(proxy, 0)) < max_accounts
                  and (key := exit_key(proxy)) is not None and key not in busy]
    candidates.sort(key=lambda proxy: int(usage.get(proxy, 0)))
    if limit is None or len(candidates) <= limit:
        return candidates
    selected: list[str] = []
    for load in range(max_accounts):
        tier = [proxy for proxy in candidates if int(usage.get(proxy, 0)) == load]
        remaining = limit - len(selected)
        if remaining <= 0:
            break
        if len(tier) <= remaining:
            selected.extend(tier)
            continue
        size = len(tier)
        selected.extend(tier[min(size - 1, int((index + 0.5) * size / remaining))]
                        for index in range(remaining))
        break
    return selected


async def select_account_proxy(
    *,
    current: str | None,
    candidates: Iterable[str],
    validate: Callable[[str], Awaitable[AccountProxyValidation]],
    bind: Callable[[str], Awaitable[bool]],
    report_failure: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Try the sticky route first, then free exits, binding only after validation.

    The caller owns candidate ordering, physical-exit capacity, hop transport,
    account credentials, and atomic persistence in ``bind``. A failed bind
    means a concurrent owner won the exit; selection continues without sharing.
    """

    seen: set[str] = set()
    for proxy in (current, *candidates):
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        verdict = await validate(proxy)
        if verdict.account_unavailable:
            raise AccountProxyUnavailable("account authorization is unavailable")
        if not verdict.ok:
            if report_failure is not None:
                await report_failure(proxy)
            continue
        if await bind(proxy):
            return proxy
    raise NoAccountProxy("no validated free proxy for account")
