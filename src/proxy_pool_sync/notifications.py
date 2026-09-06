from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable
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


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_notification_targets(
    raw_targets: Iterable[Any],
    *,
    on_warning: Callable[[str], None] | None = None,
    default_auth_type: str = "bearer",
) -> list[NotificationTarget]:
    """Convert dict/object config entries into validated notification targets."""
    def warn(message: str) -> None:
        if on_warning is not None:
            on_warning(message)

    targets: list[NotificationTarget] = []
    for raw in raw_targets:
        url = str(_value(raw, "url", "") or "").strip()
        if not url:
            warn("notification target skipped: missing url")
            continue
        method = str(_value(raw, "method", "POST") or "POST").strip().upper()
        auth_type = str(_value(raw, "auth_type", default_auth_type) or default_auth_type).strip().lower()
        auth = _value(raw, "auth", None)
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            warn(f"notification target skipped: unsupported method={method} url={url}")
            continue
        if auth_type not in {"bearer", "basic", "none"}:
            warn(f"notification target skipped: unsupported auth_type={auth_type} url={url}")
            continue
        bearer_token = ""
        username = ""
        password = ""
        if auth_type == "bearer":
            bearer_token = str(_value(auth, "token", "") if auth is not None else "").strip()
            if not bearer_token:
                warn(f"notification target skipped: bearer token missing url={url}")
                continue
        elif auth_type == "basic":
            username = str(_value(auth, "username", "") if auth is not None else "").strip()
            password = str(_value(auth, "password", "") if auth is not None else "").strip()
            if not username or not password:
                warn(f"notification target skipped: basic auth missing url={url}")
                continue
        targets.append(NotificationTarget(
            url=url, method=method, auth_type=auth_type,
            bearer_token=bearer_token, username=username, password=password,
        ))
    return targets


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
