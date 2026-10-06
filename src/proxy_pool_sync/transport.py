"""Common credential-free SSH and scoped redirect configuration primitives."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit
from urllib.parse import unquote
import base64
import socket
import ssl
import time

LOCAL_FORWARD_PATTERN = re.compile(r"(\s*LocalForward\s+127\.0\.0\.1:)(\d+)(\s+)([^: ]+):(\d+)(\s*)")


def public_proxy_ref(value: str) -> str:
    """Describe an endpoint without copying username/password into metadata."""
    try:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        if parsed.scheme not in {"http", "https", "socks4", "socks5"}:
            raise ValueError()
        host = str(ipaddress.ip_address(parsed.hostname or ""))
        port = parsed.port
        if not port or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError()
        return f"{parsed.scheme}://{'[' + host + ']' if ':' in host else host}:{port}"
    except ValueError:
        raise ValueError("Invalid proxy endpoint") from None


@dataclass(frozen=True)
class LocalForward:
    local_port: int
    target_host: str
    target_port: int

    def line(self) -> str:
        host = str(ipaddress.IPv4Address(self.target_host))
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in [self.local_port, self.target_port]):
            raise ValueError("Invalid forward port")
        return f"    LocalForward 127.0.0.1:{self.local_port} {host}:{self.target_port}"


def render_ssh_forwards(alias: str, hop_host: str, forwards: list[LocalForward]) -> str:
    """Build an explicit loopback-only hop preserving every distinct endpoint."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", alias):
        raise ValueError("Invalid SSH alias")
    host = str(ipaddress.IPv4Address(hop_host))
    if len({f.local_port for f in forwards}) != len(forwards):
        raise ValueError("Duplicate listener")
    lines = [f"Host {alias}", f"    HostName {host}", "    User root", "    BatchMode yes",
             "    ConnectTimeout 10", "    StrictHostKeyChecking yes", "    GatewayPorts no"]
    return "\n".join(lines + [forward.line() for forward in forwards]) + "\n"


def render_scoped_redirect(
    groups: list[dict], ports: list[int], relay_port: int, *, table: str, exists: bool = False,
) -> str:
    """Atomically replace only the caller's table; never redirect a shared UID."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", table):
        raise ValueError("Invalid owned table")
    if not ports or len(ports) > 8 or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise ValueError("Expected bounded provider ports")
    if type(relay_port) is not int or not 1024 <= relay_port <= 65535 or relay_port in ports:
        raise ValueError("Invalid relay port")
    lines = [f"delete table ip {table}"] if exists else []
    lines += [f"add table ip {table}",
              f"add chain ip {table} output {{ type nat hook output priority -105; policy accept; }}",
              f"add rule ip {table} output ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8, "
              "169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } return"]
    for group in groups:
        cg = group["cgroup"]
        if not re.fullmatch(r"system\.slice/docker-[a-f0-9]{64}\.scope", cg):
            raise ValueError("Refusing unrelated or broad cgroup")
        lines.append(f'add rule ip {table} output socket cgroupv2 level 2 "{cg}" '
                     f'tcp dport {{ {", ".join(map(str, sorted(set(ports))))} }} '
                     f'counter redirect to :{relay_port} comment "TGC provider via VUSA"')
    return "\n".join(lines) + "\n"


def probe_exit_ip(proxy_url: str, *, socks_hop: tuple[str, int] | None = None, timeout: float = 8) -> str | None:
    """Verify the actual IPv4 exit over TLS; credentials and errors never enter metadata.

    An optional first SOCKS5 hop carries the provider connection through VUSA.
    Caller must finish SQL transactions before using this bounded network probe.
    """
    sock = None
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    def exact(count: int) -> bytes:
        result = b""
        while len(result) < count:
            sock.settimeout(remaining())
            chunk = sock.recv(count - len(result))
            if not chunk:
                raise EOFError()
            result += chunk
        return result

    def socks_connect(host: str, port: int, username: str | None = None, password: str | None = None) -> None:
        sock.sendall(b"\x05\x01" + (b"\x02" if username else b"\x00"))
        method = exact(2)
        if username:
            user, secret = username.encode(), (password or "").encode()
            if method != b"\x05\x02" or not len(user) <= 255 or not len(secret) <= 255:
                raise ValueError()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
            if exact(2) != b"\x01\x00":
                raise ValueError()
        elif method != b"\x05\x00":
            raise ValueError()
        domain = host.encode("ascii")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + port.to_bytes(2, "big"))
        head = exact(4)
        if head[1] != 0:
            raise OSError()
        if head[3] == 1:
            exact(6)
        elif head[3] == 4:
            exact(18)
        elif head[3] == 3:
            exact(exact(1)[0] + 2)
        else:
            raise ValueError()

    try:
        public_proxy_ref(proxy_url)
        parsed = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
        host, port = parsed.hostname, parsed.port
        sock = socket.create_connection(socks_hop or (host, port), timeout=timeout)
        sock.settimeout(timeout)
        if socks_hop:
            socks_connect(host, port)
        user = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        if parsed.scheme == "http":
            authorization = ""
            if user:
                auth = base64.b64encode((user + ":" + (password or "")).encode()).decode()
                authorization = "Proxy-Authorization: Basic " + auth + "\r\n"
            sock.sendall(("CONNECT api.ipify.org:443 HTTP/1.1\r\nHost: api.ipify.org:443\r\n" +
                          authorization + "\r\n").encode())
            header = b""
            while not header.endswith(b"\r\n\r\n") and len(header) < 4096:
                header += exact(1)
            if header.split(b" ", 2)[1] != b"200":
                return None
        elif parsed.scheme == "socks5":
            socks_connect("api.ipify.org", 443, user, password)
        else:
            return None
        sock.settimeout(remaining())
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname="api.ipify.org")
        sock.sendall(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        response = b""
        while len(response) < 8192:
            sock.settimeout(remaining())
            chunk = sock.recv(2048)
            if not chunk:
                break
            response += chunk
        head, body = response.split(b"\r\n\r\n", 1)
        if head.split(b" ", 2)[1] != b"200":
            return None
        return str(ipaddress.IPv4Address(body.decode().strip()))
    except (OSError, ValueError, IndexError, EOFError, UnicodeError):
        return None
    finally:
        if sock:
            sock.close()
