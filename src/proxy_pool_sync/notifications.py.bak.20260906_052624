from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

import httpx


@dataclass(frozen=True, slots=True)
class NotificationTarget:
    url: str
    method: str = "POST"
    auth_type: str = "none"
    bearer_token: str = ""
    username: str = ""
    password: str = ""


@dataclass(frozen=True, slots=True)
class NotificationResult:
    total: int
    sent: int
    failed: int


async def _send(target: NotificationTarget, payload: dict[str, Any], *, timeout: float) -> None:
    headers = {"Content-Type": "application/json"}
    auth: httpx.Auth | None = None
    if target.auth_type == "bearer":
        headers["Authorization"] = f"Bearer {target.bearer_token}"
    elif target.auth_type == "basic":
        auth = httpx.BasicAuth(target.username, target.password)
    elif target.auth_type != "none":
        raise ValueError(f"unsupported auth_type: {target.auth_type}")

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.request(target.method.upper(), target.url, json=payload, headers=headers, auth=auth)
        response.raise_for_status()


async def notify_targets(
    targets: Iterable[NotificationTarget],
    payload: dict[str, Any],
    *,
    timeout: float = 20.0,
    concurrent: bool = True,
) -> NotificationResult:
    items = list(targets)
    if not items:
        return NotificationResult(total=0, sent=0, failed=0)

    async def send_one(target: NotificationTarget) -> bool:
        try:
            await _send(target, payload, timeout=timeout)
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(send_one(item) for item in items)) if concurrent else [await send_one(item) for item in items]
    sent = sum(results)
    return NotificationResult(total=len(items), sent=sent, failed=len(items) - sent)
