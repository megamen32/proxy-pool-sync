from __future__ import annotations

import httpx


class HttpProxySource:
    """Fetch a newline-delimited proxy export over HTTP(S)."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        timeout: float = 20.0,
        follow_redirects: bool = False,
    ):
        self.url = url
        self.bearer_token = bearer_token or ""
        self.timeout = timeout
        self.follow_redirects = follow_redirects

    async def fetch_text(self) -> str:
        headers = {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else {}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=self.follow_redirects) as client:
            response = await client.get(self.url, headers=headers)
            response.raise_for_status()
            return response.text
