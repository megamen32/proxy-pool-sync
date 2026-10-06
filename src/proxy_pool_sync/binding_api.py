"""Small authenticated API for exchanging metadata; native rebind stays outside HTTP handling."""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from urllib.parse import urlsplit, parse_qs

import httpx

from .bindings import BindingEvent, BindingJournal


@dataclass(frozen=True)
class BindingPeer:
    url: str
    bearer_token: str = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.url)
        if url.username or url.password or url.query or url.fragment or len(self.bearer_token) < 32:
            raise ValueError("Private authenticated binding endpoint required")
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("Remote binding metadata requires HTTPS")

    def deliver(self, event: BindingEvent) -> bool:
        """Acknowledge only an authenticated, matching durable receiver response."""
        response = httpx.post(
            self.url, content=event.encode(), timeout=3,
            headers={"Authorization": f"Bearer {self.bearer_token}", "Content-Type": "application/json",
                     "Idempotency-Key": event.event_id},
            follow_redirects=False,
        )
        if response.status_code != 200:
            return False
        body = response.json()
        return body.get("event_id") == event.event_id and body.get("status") in {"accepted", "duplicate", "stale"}

    def preferred(self, telegram_user_id: int) -> BindingEvent | None:
        """Read one durable preference at the native next-connection boundary."""
        response = httpx.get(self.url, params={"telegram_user_id": telegram_user_id}, timeout=2,
                             headers={"Authorization": f"Bearer {self.bearer_token}"}, follow_redirects=False)
        response.raise_for_status()
        body = response.json().get("binding")
        event = BindingEvent.decode(json.dumps(body)) if body else None
        if event and event.telegram_user_id != telegram_user_id:
            raise ValueError("Native identity mismatch")
        return event


def binding_server(
    address: tuple[str, int], journal: BindingJournal, *, bearer_token: str, allowed_source: str,
) -> HTTPServer:
    """Single HTTP handler thread, bounded body and timeout; tokens never enter logs."""
    if address[0] != "127.0.0.1" or len(bearer_token) < 32:
        raise ValueError("Private loopback API and strong ingress token required")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def authorized(self) -> bool:
            value = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(value, ("Bearer " + bearer_token).encode())

        def reply(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            parsed = urlsplit(self.path)
            if parsed.path != "/v1/bindings":
                return self.reply(404, {"error": "not_found"})
            if parsed.query:
                try:
                    query = parse_qs(parsed.query, strict_parsing=True)
                    if set(query) != {"telegram_user_id"} or len(query["telegram_user_id"]) != 1:
                        raise ValueError()
                    account = int(query["telegram_user_id"][0])
                    if not 0 < account < 2**63:
                        raise ValueError()
                    event = journal.preferred(account)
                except (ValueError, KeyError):
                    return self.reply(400, {"error": "invalid_identity"})
                return self.reply(200, {"binding": json.loads(event.encode()) if event else None})
            return self.reply(200, {"bindings": [json.loads(e.encode()) for e in journal.preferences()]})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            if self.path != "/v1/bindings":
                return self.reply(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("Invalid body length")
                event = BindingEvent.decode(self.rfile.read(size).decode())
                if self.headers.get("Idempotency-Key") != event.event_id:
                    raise ValueError("Idempotency key mismatch")
                status = journal.receive(event, allowed_source=allowed_source)
            except (ValueError, TypeError, UnicodeError):
                return self.reply(400, {"error": "invalid_binding_event"})
            return self.reply(200, {"event_id": event.event_id, "status": status, "applied": False})

    return HTTPServer(address, Handler)
