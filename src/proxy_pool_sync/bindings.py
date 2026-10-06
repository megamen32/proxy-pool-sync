"""Weak, idempotent binding exchange by native Telegram identity, not local row ID."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3

from .transport import public_proxy_ref


@dataclass(frozen=True)
class BindingEvent:
    event_id: str
    scope: str
    telegram_user_id: int
    source: str
    revision: int
    proxy_ref: str
    exit_ip: str | None = None
    reason: str = "assignment_changed"

    def __post_init__(self) -> None:
        if type(self.telegram_user_id) is not int or not 0 < self.telegram_user_id < 2**63:
            raise ValueError("Native Telegram user ID required")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Positive binding revision required")
        if not re.fullmatch(r"[a-f0-9]{64}", self.event_id):
            raise ValueError("Invalid idempotency key")
        for name in [self.scope, self.source, self.reason]:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", name):
                raise ValueError("Invalid binding namespace")
        if public_proxy_ref(self.proxy_ref) != self.proxy_ref:
            raise ValueError("Credential-free canonical proxy reference required")
        if self.exit_ip is not None:
            if type(self.exit_ip) is not str or str(ipaddress.IPv4Address(self.exit_ip)) != self.exit_ip:
                raise ValueError("Canonical IPv4 exit required")

    @property
    def version(self) -> tuple[int, str]:
        return self.revision, self.source

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value: str) -> BindingEvent:
        if len(value.encode()) > 4096:
            raise ValueError("Oversized binding event")
        return cls(**json.loads(value))


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class BindingJournal:
    """One local durable projection/outbox per service; no cross-service shared DB."""

    def __init__(self, path: Path, *, scope: str, source: str):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Binding journal must not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.path, self.scope, self.source = path, scope, source
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity(scope TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferred(account INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed(account INTEGER PRIMARY KEY, ref TEXT NOT NULL, exit_ip TEXT);
                CREATE TABLE IF NOT EXISTS seen(event TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS outbox(event TEXT PRIMARY KEY, account INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS outbox_account ON outbox(account);
            """)
            identity = db.execute("SELECT scope,source FROM identity").fetchone()
            if identity and identity != (scope, source):
                raise ValueError("Journal belongs to a different service/scope")
            if not identity:
                db.execute("INSERT INTO identity VALUES(?,?)", (scope, source))

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=2, isolation_level="IMMEDIATE", factory=_Connection)

    def preferred(self, account: int) -> BindingEvent | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM preferred WHERE account=?", (account,)).fetchone()
            return BindingEvent.decode(row[0]) if row else None

    def receive(self, event: BindingEvent, *, allowed_source: str) -> str:
        if event.scope != self.scope or event.source != allowed_source or event.source == self.source:
            raise ValueError("Untrusted binding source/scope")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM seen WHERE event=?", (event.event_id,)).fetchone():
                return "duplicate"
            db.execute("INSERT INTO seen VALUES(?)", (event.event_id,))
            db.execute("DELETE FROM seen WHERE rowid NOT IN (SELECT rowid FROM seen ORDER BY rowid DESC LIMIT 10000)")
            row = db.execute("SELECT body FROM preferred WHERE account=?", (event.telegram_user_id,)).fetchone()
            old = BindingEvent.decode(row[0]) if row else None
            if old and old.version >= event.version:
                return "stale"
            db.execute("INSERT OR REPLACE INTO preferred VALUES(?,?)", (event.telegram_user_id, event.encode()))
            return "accepted"

    def observe(self, account: int, proxy_ref: str, exit_ip: str | None = None) -> BindingEvent | None:
        """Publish committed native changes; applying a peer preference produces no echo."""
        proxy_ref = public_proxy_ref(proxy_ref)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT ref,exit_ip FROM observed WHERE account=?", (account,)).fetchone()
            # A failed liveness check is not a new binding or a loss of the last verified exit.
            if old and old[0] == proxy_ref and exit_ip is None:
                return None
            if old == (proxy_ref, exit_ip):
                return None
            row = db.execute("SELECT body FROM preferred WHERE account=?", (account,)).fetchone()
            desired = BindingEvent.decode(row[0]) if row else None
            db.execute("INSERT OR REPLACE INTO observed VALUES(?,?,?)", (account, proxy_ref, exit_ip))
            if desired and (desired.proxy_ref == proxy_ref or (exit_ip and desired.exit_ip == exit_ip)):
                return None
            if desired and old is None and desired.source != self.source:
                return None
            revision = desired.revision + 1 if desired else 1
            key = hashlib.sha256(f"{self.scope}:{account}:{self.source}:{revision}:{proxy_ref}:{exit_ip}".encode()).hexdigest()
            event = BindingEvent(key, self.scope, account, self.source, revision, proxy_ref, exit_ip)
            db.execute("INSERT OR REPLACE INTO preferred VALUES(?,?)", (account, event.encode()))
            # Only the latest native assignment per account needs retransmission.
            db.execute("DELETE FROM outbox WHERE account=?", (account,))
            db.execute("INSERT INTO outbox(event,account,body) VALUES(?,?,?)", (event.event_id, account, event.encode()))
            return event

    def pending(self, limit: int = 50) -> list[BindingEvent]:
        with self.connect() as db:
            return [BindingEvent.decode(r[0]) for r in db.execute(
                "SELECT body FROM outbox ORDER BY rowid LIMIT ?", (min(50, max(1, limit)),),
            )]

    def acknowledge(self, event_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM outbox WHERE event=?", (event_id,))

    def preferences(self) -> list[BindingEvent]:
        with self.connect() as db:
            return [BindingEvent.decode(r[0]) for r in db.execute("SELECT body FROM preferred ORDER BY account")]


def may_apply_preference(*, same_account: bool, idle: bool, checked_working: bool, capacity_available: bool) -> bool:
    """Receiver keeps native ownership and capacity checks before any rebind."""
    return same_account and idle and checked_working and capacity_available
