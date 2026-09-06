from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


@dataclass(frozen=True, slots=True)
class PoolUpdatePolicy:
    """Safety rails for replacing a healthy pool with a suspicious upstream export."""

    min_count: int = 1
    max_drop_ratio: float | None = None

    def validate(self, previous: list[str], current: list[str], *, force: bool = False) -> None:
        if force:
            return
        if len(current) < max(0, int(self.min_count)):
            raise ValueError(f"proxy export has only {len(current)} proxies; minimum is {self.min_count}")
        if previous and self.max_drop_ratio is not None:
            drop_ratio = max(0.0, (len(previous) - len(current)) / len(previous))
            if drop_ratio > float(self.max_drop_ratio):
                raise ValueError(
                    f"proxy export shrank by {drop_ratio:.1%}; maximum allowed drop is {self.max_drop_ratio:.1%}"
                )


def normalize_proxy_line(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or not port:
        return None
    return text


def sanitize_proxy_lines(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def parse_proxy_export(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = normalize_proxy_line(raw)
        if line and line not in seen:
            seen.add(line)
            result.append(line)
    return result


class FileProxyPool:
    """Atomic text-file storage for a canonical proxy pool."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[str]:
        try:
            return parse_proxy_export(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []

    def replace_text(
        self,
        text: str,
        *,
        allow_empty: bool = False,
        update_policy: PoolUpdatePolicy | None = None,
        force: bool = False,
    ) -> list[str]:
        proxies = parse_proxy_export(text)
        if not proxies and not allow_empty:
            raise ValueError("proxy export is empty")
        if update_policy is not None:
            update_policy.validate(self.load(), proxies, force=force)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")
        os.replace(temporary, self.path)
        return proxies

    def replace_lines(
        self,
        lines: list[str],
        *,
        allow_empty: bool = False,
        update_policy: PoolUpdatePolicy | None = None,
        force: bool = False,
    ) -> list[str]:
        return self.replace_text(
            "\n".join(lines) + ("\n" if lines else ""),
            allow_empty=allow_empty,
            update_policy=update_policy,
            force=force,
        )

    def replace_raw_lines(self, lines: list[str], *, allow_empty: bool = False) -> list[str]:
        cleaned = sanitize_proxy_lines("\n".join(lines))
        if not cleaned and not allow_empty:
            raise ValueError("proxy export is empty")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
        os.replace(temporary, self.path)
        return cleaned
