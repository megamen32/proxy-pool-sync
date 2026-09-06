from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


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
    """Strip blanks/comments and deduplicate while preserving each raw proxy line.

    Useful for legacy consumers that own scheme inference or custom parsing.
    """
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

    def replace_text(self, text: str, *, allow_empty: bool = False) -> list[str]:
        proxies = parse_proxy_export(text)
        if not proxies and not allow_empty:
            raise ValueError("proxy export is empty")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")
        os.replace(temporary, self.path)
        return proxies

    def replace_lines(self, lines: list[str], *, allow_empty: bool = False) -> list[str]:
        return self.replace_text("\n".join(lines) + ("\n" if lines else ""), allow_empty=allow_empty)

    def replace_raw_lines(self, lines: list[str], *, allow_empty: bool = False) -> list[str]:
        """Atomically replace the file while preserving caller-owned line syntax.

        Use this when a legacy consumer intentionally owns proxy scheme inference.
        Blank/comment stripping and deduplication remain available through
        :func:`sanitize_proxy_lines`.
        """
        cleaned = sanitize_proxy_lines("\n".join(lines))
        if not cleaned and not allow_empty:
            raise ValueError("proxy export is empty")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
        os.replace(temporary, self.path)
        return cleaned
