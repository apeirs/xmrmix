"""Offline tests: SOCKS5 + HTTP + monerod logic against in-process fakes."""

import json
import socket
import threading
import unittest
import urllib.request

from xmrmix import monerod, nodes, proxy, tx
from xmrmix.mixhttp import MixnetClient, MixnetError
from xmrmix.socks import ProxyUnreachable

from .fakes import FakeMonerod, FakeSocks5

# A minimal v2 tx prefix header: version 2, unlock 0, 1 input of type 0x02, padded to the 100-byte floor.
GOOD_TX = (bytes([2, 0, 1, 2]) + bytes(120)).hex()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Tx(unittest.TestCase):
    def test_good(self):
        i = tx.parse(GOOD_TX)
        self.assertEqual((i.version, i.unlock_time, i.n_inputs, i.input_type, i.size), (2, 0, 1, 2, 124))

    def test_rejects_wrong_things(self):
        for bad, word in [("ab" * 32, "transaction id"), ("zz", "hex"), ("00" * 10, "short"),
                          (bytes([9, 0, 1, 2]).hex() + "00" * 120, "version"),
                          (bytes([2, 0, 0, 2]).hex() + "00" * 120, "input count"),
                          (bytes([2, 0, 1, 7]).hex() + "00" * 120, "unknown type")]:
            with self.assertRaises(tx.TxError) as cm:
                tx.parse(bad)
            self.assertIn(word, str(cm.exception))

    def test_whitespace_and_prefix(self):
        self.assertEqual(tx.parse("0x" + GOOD_TX[:20] + "\n" + GOOD_TX[20:]).size, 124)


class Nodes(unittest.TestCase):
    def test_exit_policy_ports(self):
        for u in ["https://a.example", "http://a.example", "http://a.example:80", "https://a.example:443",
                  "http://a.example:81", "http://a.example:8080", "http://a.example:8081", "http://a.example:9080"]:
            self.assertTrue(nodes.allowed_by_exit_policy(u), u)
        for u in ["http://a.example:18080", "http://a.example:18081", "http://a.example:18089",
                  "http://a.example:9081", "ftp://a.example:443"]:
            self.assertFalse(nodes.allowed_by_exit_policy(u), u)

    def test_filter_dedupes_and_normalises(self):
        out = nodes.filter_urls(["https://x.example", "https://x.example:443", "http://y.example:18081",
                                 "http://z.example:80", "garbage"])
        self.assertEqual(out, ["https://x.example:443", "http://z.example:80"])


class OverMixnet(unittest.TestCase):
    def setUp(self):
        self.node = FakeMonerod()
        # "exit": route any CONNECT to the fake node.
        self.proxy = FakeSocks5(route=lambda host, port: ("127.0.0.1", self.node.port))
        self.client = MixnetClient(proxy=self.proxy.addr, dns="exit", retries=0, timeout=10)
        self.url = "http://monero.example:80"

    def tearDown(self):
        self.node.shutdown()
        self.proxy.shutdown()

    def test_every_request_goes_through_the_proxy(self):
        info = monerod.get_info(self.client, self.url)
        self.assertEqual(info["height"], 3000000)
        self.assertEqual(self.proxy.targets, [(3, "monero.example", 80)])  # hostname sent to the exit, not resolved locally

    def test_fee_estimate(self):
        self.assertEqual(monerod.fee_estimate(self.client, self.url)["fee"], 20000)

    def test_json_rpc_error_is_raised(self):
        with self.assertRaises(monerod.NodeError):
            monerod.json_rpc(self.client, self.url, "nope")

    def test_pool_hashes(self):
        self.assertEqual(len(monerod.pool_hashes(self.client, self.url)), 2)

    def test_broadcast_accepted(self):
        res = monerod.broadcast(self.client, [self.url, self.url], GOOD_TX)
        self.assertTrue(all(r.accepted for r in res))
        sent = [json.loads(b) for p, b in self.node.requests if p == "/send_raw_transaction"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], {"tx_as_hex": GOOD_TX, "do_not_relay": False})

    def test_broadcast_rejected_reports_reason(self):
        self.node.relay = False
        (r,) = monerod.broadcast(self.client, [self.url], GOOD_TX)
        self.assertFalse(r.accepted)
        self.assertIn("double_spend", r.reason)

    def test_do_not_relay_is_never_reported_as_accepted(self):
        (r,) = monerod.broadcast(self.client, [self.url], GOOD_TX, do_not_relay=True)
        self.assertFalse(r.accepted)

    def test_sequential_mode(self):
        res = monerod.broadcast(self.client, [self.url, self.url], GOOD_TX, parallel=False)
        self.assertEqual([r.accepted for r in res], [True, True])

    def test_http_error_becomes_a_failed_result(self):
        self.node.fail_status = 503
        (r,) = monerod.broadcast(self.client, [self.url], GOOD_TX)
        self.assertFalse(r.accepted)
        self.assertIn("503", r.reason)

    def test_no_proxy_means_no_request(self):
        c = MixnetClient(proxy=f"127.0.0.1:{free_port()}", dns="exit", retries=0)
        with self.assertRaises(ProxyUnreachable):
            monerod.get_info(c, self.url)

    def test_exit_refusal_is_an_error_not_a_direct_connection(self):
        p = FakeSocks5(route=None, reply_code=2)
        c = MixnetClient(proxy=p.addr, dns="exit", retries=0)
        with self.assertRaises(MixnetError):
            monerod.get_info(c, "http://monero.example:18081")
        p.shutdown()


class Gateway(unittest.TestCase):
    def setUp(self):
        self.node = FakeMonerod()
        self.socks = FakeSocks5(route=lambda host, port: ("127.0.0.1", self.node.port))
        self.client = MixnetClient(proxy=self.socks.addr, dns="exit", retries=0, timeout=10)
        self.srv = proxy.make_server(self.client, proxy.Upstream(["http://one.example:80", "http://two.example:80"]),
                                     port=free_port())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.socks.shutdown()
        self.node.shutdown()

    def _post(self, path, data=b"{}"):
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10)

    def test_json_passthrough(self):
        self.assertEqual(json.load(self._post("/get_info"))["height"], 3000000)

    def test_binary_passthrough(self):
        r = self._post("/get_height.bin")
        self.assertEqual(r.read(), b"\x01\x02\x00\xff")
        self.assertEqual(r.headers["Content-Type"], "application/octet-stream")

    def test_upstream_status_is_relayed(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/nonexistent")
        self.assertEqual(cm.exception.code, 404)

    def test_only_loopback(self):
        with self.assertRaises(ValueError):
            proxy.make_server(self.client, proxy.Upstream(["http://one.example:80"]), host="0.0.0.0", port=free_port())

    def test_fails_over_then_502(self):
        self.node.fail_status = 503
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/get_info")
        self.assertEqual(cm.exception.code, 502)
        self.node.fail_status = None
        self.assertEqual(json.load(self._post("/get_info"))["status"], "OK")


if __name__ == "__main__":
    unittest.main()
