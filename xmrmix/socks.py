"""Minimal SOCKS5 CONNECT client (RFC 1928, no auth), stdlib only.

Used to open TCP streams through the local ``nym-socks5-client``, which turns
each stream into Sphinx packets that cross the Nym mixnet to an exit
gateway's network requester.
"""

from __future__ import annotations

import ipaddress
import socket
import struct


class SocksError(OSError):
    """The SOCKS5 proxy refused or broke the handshake."""


class ProxyUnreachable(SocksError):
    """No SOCKS5 proxy is listening. We never fall back to a direct connection."""


_REPLY_ERRORS = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset (exit policy?)",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def parse_proxy(spec: str) -> tuple[str, int]:
    """Parse ``host:port`` (``[v6]:port`` allowed). Defaults to port 1080."""
    spec = spec.strip()
    if spec.startswith("socks5://") or spec.startswith("socks5h://"):
        spec = spec.split("://", 1)[1]
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest.lstrip(":") or 1080)
        return host, port
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, 1080


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("SOCKS5 proxy closed the connection during handshake")
        buf += chunk
    return buf


def encode_address(host: str) -> bytes:
    """Encode a destination as SOCKS5 ATYP + address bytes."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("idna")
        if not 0 < len(name) < 256:
            raise ValueError(f"bad hostname: {host!r}")
        return b"\x03" + bytes([len(name)]) + name
    if ip.version == 4:
        return b"\x01" + ip.packed
    return b"\x04" + ip.packed


def connect(proxy: tuple[str, int], host: str, port: int, timeout: float = 60.0) -> socket.socket:
    """Open a TCP stream to ``host:port`` through the SOCKS5 proxy.

    ``host`` may be a hostname (resolved by the far end, i.e. the exit) or an
    IP literal. Raises :class:`ProxyUnreachable` if the proxy is not listening.
    """
    try:
        sock = socket.create_connection(proxy, timeout=timeout)
    except OSError as e:  # refused / no route
        raise ProxyUnreachable(
            f"no SOCKS5 proxy at {proxy[0]}:{proxy[1]} ({e}). Is nym-socks5-client "
            "running? Refusing to fall back to a direct (clearnet) connection."
        ) from e
    try:
        sock.sendall(b"\x05\x01\x00")  # version 5, 1 method, no-auth
        ver, method = _recv_exact(sock, 2)
        if ver != 5 or method != 0:
            raise SocksError(f"proxy does not accept no-auth SOCKS5 (ver={ver}, method={method})")
        sock.sendall(b"\x05\x01\x00" + encode_address(host) + struct.pack(">H", port))
        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if ver != 5:
            raise SocksError(f"bad SOCKS version in reply: {ver}")
        if rep != 0:
            raise SocksError(f"SOCKS5 CONNECT to {host}:{port} failed: {_REPLY_ERRORS.get(rep, rep)}")
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 4:
            _recv_exact(sock, 16)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        else:
            raise SocksError(f"bad ATYP in reply: {atyp}")
        _recv_exact(sock, 2)  # bound port
        return sock
    except BaseException:
        sock.close()
        raise
