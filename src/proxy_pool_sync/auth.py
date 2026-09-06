from __future__ import annotations

import secrets


def callback_is_authorized(
    client_host: str | None,
    authorization: str | None,
    *,
    bearer_token: str = "",
    trust_localhost: bool = True,
) -> bool:
    if trust_localhost and client_host in {"127.0.0.1", "::1", "localhost"}:
        return True
    if not bearer_token:
        return False
    header = str(authorization or "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return bool(token and secrets.compare_digest(token, bearer_token))
