from __future__ import annotations

import base64
import secrets


def _basic_credentials(authorization: str | None) -> tuple[str, str] | None:
    header = str(authorization or "").strip()
    if not header.lower().startswith("basic "):
        return None
    encoded = header[6:].strip()
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    return tuple(decoded.split(":", 1))  # type: ignore[return-value]


def callback_is_authorized(
    client_host: str | None,
    authorization: str | None,
    *,
    bearer_token: str = "",
    basic_username: str = "",
    basic_password: str = "",
    trust_localhost: bool = True,
) -> bool:
    """Validate localhost, bearer or basic callback authentication."""
    if trust_localhost and client_host in {"127.0.0.1", "::1", "localhost"}:
        return True

    header = str(authorization or "").strip()
    if bearer_token and header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token and secrets.compare_digest(token, bearer_token):
            return True

    if basic_username and basic_password:
        credentials = _basic_credentials(header)
        if credentials is not None:
            username, password = credentials
            return secrets.compare_digest(username, basic_username) and secrets.compare_digest(
                password, basic_password
            )
    return False
