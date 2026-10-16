"""HTTP(S) over the Nym mixnet via a local nym-socks5-client.

Every request opens a fresh SOCKS5 stream. Nothing here ever opens a direct
connection to the destination: if the proxy is down, requests fail.

DNS: by default (``dns="doh"``) hostnames are resolved with DNS-over-HTTPS
*through the mixnet* (Cloudflare 1.1.1.1, then Google 8.8.8.8, both reached
by IP literal so no DNS lookup is needed to find them) and the stream is
opened to an IPv4 address. Reason: some exit gateways have broken IPv6
routes, and a network requester that resolves a dual-stack name itself may
pick an unreachable AAAA record and stall (seen live against mempool.space,
2026-10-10). ``dns="exit"`` passes the hostname to the exit instead.
Either way the local resolver never sees the destination name.
"""

from __future__ import annotations

import http.client
import json
import random
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import socks

DEFAULT_PROXY = "127.0.0.1:1080"
# Generic UA: don't advertise the tool to the API operator.
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Connection": "close"}

DOH_RESOLVERS = [
    # (ip, tls server name, path template)
    ("1.1.1.1", "1.1.1.1", "/dns-query?name={name}&type=A"),
    ("8.8.8.8", "8.8.8.8", "/resolve?name={name}&type=A"),
]


class MixnetError(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    reason: str
    body: bytes
    elapsed: float
    connect_host: str
    attempts: int
    headers: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, connect_host, proxy, timeout):
        super().__init__(host, port, timeout=timeout)
        self._connect_host = connect_host
        self._proxy = proxy

    def connect(self):
        self.sock = socks.connect(self._proxy, self._connect_host, self.port, self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, connect_host, proxy, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._connect_host = connect_host
        self._proxy = proxy

    def connect(self):
        raw = socks.connect(self._proxy, self._connect_host, self.port, self.timeout)
        # TLS is end-to-end with the destination: the exit gateway only relays
        # ciphertext. SNI/certificate are checked against the real hostname.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class MixnetClient:
    """Tiny HTTP client whose every byte goes through the Nym mixnet."""

    def __init__(
        self,
        proxy: str = DEFAULT_PROXY,
        timeout: float = 45.0,
        retries: int = 2,
        dns: str = "doh",
        trace: Optional[Callable[[str], None]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        if dns not in ("doh", "exit"):
            raise ValueError("dns must be 'doh' or 'exit'")
        self.proxy = socks.parse_proxy(proxy)
        self.timeout = timeout
        self.retries = retries
        self.dns = dns
        self.trace = trace or (lambda msg: None)
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._dns_cache: dict[str, tuple[float, list[str]]] = {}

    # -- low level -------------------------------------------------------
    def _once(self, method, parsed, connect_host, body, headers):
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https":
            conn = _SocksHTTPSConnection(parsed.hostname, port, connect_host, self.proxy, self.timeout, self.ssl_context)
        elif parsed.scheme == "http":
            conn = _SocksHTTPConnection(parsed.hostname, port, connect_host, self.proxy, self.timeout)
        else:
            raise ValueError(f"unsupported scheme {parsed.scheme}")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.reason, data, dict(resp.getheaders())
        finally:
            conn.close()

    def _resolve(self, host: str) -> list[str]:
        """Resolve ``host`` to IPv4 addresses using DoH over the mixnet."""
        try:
            import ipaddress

            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        cached = self._dns_cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        errors = []
        for ip, sni, tmpl in DOH_RESOLVERS:
            parsed = urllib.parse.urlsplit(f"https://{sni}" + tmpl.format(name=urllib.parse.quote(host)))
            t0 = time.monotonic()
            try:
                status, _, data, _ = self._once(
                    "GET", parsed, ip, None, dict(DEFAULT_HEADERS, Accept="application/dns-json")
                )
                if status != 200:
                    raise MixnetError(f"DoH HTTP {status}")
                answers = [a["data"] for a in json.loads(data).get("Answer", []) if a.get("type") == 1]
                if not answers:
                    raise MixnetError("no A records")
                self._dns_cache[host] = (time.monotonic() + 300.0, answers)
                self.trace(f"dns  {host} -> {', '.join(answers)} (DoH via {ip}, over mixnet, {time.monotonic() - t0:.2f}s)")
                return answers
            except (OSError, MixnetError, ValueError, http.client.HTTPException) as e:
                errors.append(f"{ip}: {e}")
                self.trace(f"dns  DoH via {ip} failed after {time.monotonic() - t0:.2f}s: {e}")
        raise MixnetError(f"could not resolve {host} over the mixnet: {'; '.join(errors)}")

    # -- public ----------------------------------------------------------
    def request(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[dict] = None) -> Response:
        parsed = urllib.parse.urlsplit(url)
        hdrs = dict(DEFAULT_HEADERS)
        hdrs.update(headers or {})
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.retries + 2):
            connect_host = parsed.hostname
            if self.dns == "doh":
                try:
                    connect_host = random.choice(self._resolve(parsed.hostname))
                except MixnetError as e:
                    self.trace(f"dns  falling back to exit-side resolution: {e}")
                    connect_host = parsed.hostname
            t0 = time.monotonic()
            self.trace(f"send {method} {url} -> socks5 {self.proxy[0]}:{self.proxy[1]} -> mixnet -> {connect_host}:{parsed.port or 443} (attempt {attempt})")
            try:
                status, reason, data, rh = self._once(method, parsed, connect_host, body, hdrs)
            except socks.ProxyUnreachable:
                raise
            except (OSError, http.client.HTTPException) as e:
                last_exc = e
                self.trace(f"fail {url} after {time.monotonic() - t0:.2f}s: {type(e).__name__}: {e}")
                continue
            elapsed = time.monotonic() - t0
            self.trace(f"recv HTTP {status} {reason} from {parsed.hostname} in {elapsed:.2f}s ({len(data)} bytes)")
            return Response(url, status, reason, data, elapsed, connect_host, attempt, rh)
        raise MixnetError(f"{method} {url} failed after {self.retries + 1} attempts: {last_exc}")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, body: bytes, **kw) -> Response:
        return self.request("POST", url, body=body, **kw)
