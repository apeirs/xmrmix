"""A local monerod-compatible RPC gateway whose upstream is a public node reached over Nym.

Point a wallet at it (``monero-wallet-cli --daemon-address 127.0.0.1:18081``) and the
node you talk to only ever sees a Nym exit gateway. Requests are relayed
unchanged (JSON, binary ``.bin`` endpoints and all). Latency is that of a
mixnet round trip, so this suits balance refreshes and broadcasts, not a first
full sync of a large wallet.
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .mixhttp import MixnetClient, MixnetError

PASS_REQUEST = ("content-type", "accept")
PASS_RESPONSE = ("content-type",)


class Upstream:
    """Sticky choice of node with fail-over to the next one."""

    def __init__(self, nodes: list[str]):
        if not nodes:
            raise ValueError("no upstream nodes")
        self.nodes = list(nodes)
        self.i = 0
        self.lock = threading.Lock()

    def current(self) -> str:
        with self.lock:
            return self.nodes[self.i % len(self.nodes)]

    def failed(self, node: str) -> None:
        with self.lock:
            if self.nodes[self.i % len(self.nodes)] == node:
                self.i += 1


def make_server(client: MixnetClient, upstream: Upstream, host: str = "127.0.0.1", port: int = 18081,
                log=lambda m: None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the gateway only binds to loopback: it has no authentication")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # noqa: D401
            pass

        def _relay(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() in PASS_REQUEST}
            last = None
            for _ in range(len(upstream.nodes)):
                node = upstream.current()
                try:
                    r = client.request(self.command, node + self.path, body=body, headers=hdrs)
                except MixnetError as e:
                    last = e
                    log(f"{node} failed ({e}); trying the next node")
                    upstream.failed(node)
                    continue
                if r.status >= 500:
                    last = MixnetError(f"HTTP {r.status}")
                    upstream.failed(node)
                    continue
                log(f"{self.command} {self.path} -> {node} HTTP {r.status} {len(r.body)} B {r.elapsed:.1f}s")
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in PASS_RESPONSE:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(r.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(r.body)
                self.close_connection = True
                return
            msg = f"all upstream nodes failed: {last}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            self.close_connection = True

        do_GET = do_POST = _relay

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def serve(client: MixnetClient, nodes: list[str], host: str, port: int) -> None:
    srv = make_server(client, Upstream(nodes), host, port, log=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"xmrmix gateway on http://{host}:{port}  ->  {len(nodes)} candidate node(s), via Nym", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
