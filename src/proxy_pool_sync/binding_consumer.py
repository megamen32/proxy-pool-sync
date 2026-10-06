"""Checked peer preference for a caller holding its native account connection lock."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from .binding_api import BindingPeer
from .transport import public_proxy_ref, probe_exit_ip


async def checked_preference(
    peer: BindingPeer, telegram_user_id: int, candidates: Sequence[str], *,
    scope: str, expected_source: str, idle: bool,
    public_ref: Callable[[str], str] = public_proxy_ref,
    probe: Callable[[str], str | None] = probe_exit_ip,
) -> str | None:
    """Return an owned verified candidate; never persist or interrupt a connection.

    Caller must hold the native per-account lock, finish read transactions before
    calling, and use native capacity/reservation checks before committing the result.
    An unavailable API or incompatible exit leaves the current assignment intact.
    """
    if not idle or type(telegram_user_id) is not int or telegram_user_id <= 0:
        return None
    try:
        desired = await asyncio.to_thread(peer.preferred, telegram_user_id)
        if not desired or desired.scope != scope or desired.source != expected_source or not desired.exit_ip:
            return None
        host = urlsplit(desired.proxy_ref).hostname
        # One endpoint per protocol in each owned pool; never import peer credentials.
        matches = [url for url in candidates if urlsplit(public_ref(url)).hostname == host][:2]
        for url in matches:
            if await asyncio.to_thread(probe, url) == desired.exit_ip:
                return url
    except Exception:
        # No URL, auth material or provider error may enter a consumer error message.
        return None
    return None
