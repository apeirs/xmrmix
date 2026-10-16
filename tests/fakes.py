"""In-process fakes: a SOCKS5 proxy and a monerod-like HTTP RPC."""

import json
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSocks5(socketserver.ThreadingTCPServer):
    """SOCKS5 server that records CONNECT targets and forwards to ``route(host, port)``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, route, reply_code=0):
        self.route = route
        self.reply_code = reply_code
        self.targets = []
        super().__init__(("127.0.0.1", 0), _SocksHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def addr(self):
        return f"127.0.0.1:{self.server_address[1]}"


class _SocksHandler(socketserver.BaseRequestHandler):
    def _recv(self, n):
        b = b""
        while len(b) < n:
            c = self.request.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def handle(self):
        ver, nmeth = self._recv(2)
        self._recv(nmeth)
        self.request.sendall(b"\x05\x00")
        ver, cmd, _, atyp = self._recv(4)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, self._recv(4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._recv(16))
        else:
            host = self._recv(self._recv(1)[0]).decode()
        (port,) = struct.unpack(">H", self._recv(2))
        self.server.targets.append((atyp, host, port))
        if self.server.reply_code:
            self.request.sendall(bytes([5, self.server.reply_code, 0, 1]) + b"\0" * 6)
            return
        # Like nym-socks5-client: report success at once, the far end may still fail.
        self.request.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
        try:
            upstream = socket.create_connection(self.server.route(host, port))
        except OSError:
            return  # stream just closes, as when an exit can't reach the host
        _pipe(self.request, upstream)


def _pipe(a, b):
    def fwd(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=fwd, args=(b, a), daemon=True)
    t.start()
    fwd(a, b)
    t.join(5)



class FakeMonerod(ThreadingHTTPServer):
    """Plain-HTTP monerod 'other' RPC subset. ``relay`` decides what send_raw_transaction answers."""

    daemon_threads = True

    def __init__(self, relay=True, height=3000000, fail_status=None):
        self.relay = relay
        self.height = height
        self.fail_status = fail_status
        self.requests = []
        super().__init__(("127.0.0.1", 0), _MonerodHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class _MonerodHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        s = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        s.requests.append((self.path, body))
        if s.fail_status:
            return self._send(s.fail_status, {"error": "down"})
        if self.path == "/get_info":
            return self._send(200, {"status": "OK", "height": s.height, "mainnet": True, "synchronized": True})
        if self.path == "/json_rpc":
            req = json.loads(body)
            if req["method"] == "get_fee_estimate":
                return self._send(200, {"id": "0", "jsonrpc": "2.0", "result": {"status": "OK", "fee": 20000, "fees": [20000, 80000], "quantization_mask": 10000}})
            return self._send(200, {"id": "0", "jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}})
        if self.path == "/send_raw_transaction":
            req = json.loads(body)
            ok = s.relay and req["tx_as_hex"]
            return self._send(200, {"status": "OK" if ok else "Failed", "not_relayed": not ok, "double_spend": not ok, "reason": "" if ok else "double spend"})
        if self.path == "/get_transaction_pool_hashes":
            return self._send(200, {"status": "OK", "tx_hashes": ["aa" * 32, "bb" * 32]})
        if self.path == "/get_height.bin":
            return self._send(200, b"\x01\x02\x00\xff", "application/octet-stream")
        self._send(404, {"error": "no such path"})
