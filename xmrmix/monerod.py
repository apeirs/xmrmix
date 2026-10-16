"""monerod RPC over the mixnet. Plain JSON, no dependencies."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .mixhttp import MixnetClient, MixnetError


class NodeError(Exception):
    pass


def rpc(client: MixnetClient, node: str, path: str, payload: Optional[dict] = None) -> dict:
    """POST ``payload`` as JSON to ``node``/``path`` (a monerod 'other' RPC, e.g. get_info)."""
    r = client.post(node.rstrip("/") + "/" + path.lstrip("/"), json.dumps(payload or {}).encode(),
                    headers={"Content-Type": "application/json"})
    if not r.ok:
        raise NodeError(f"{node}: HTTP {r.status}")
    try:
        return json.loads(r.body)
    except ValueError:
        raise NodeError(f"{node}: reply is not JSON") from None


def json_rpc(client: MixnetClient, node: str, method: str, params: Optional[dict] = None) -> dict:
    body = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    r = rpc(client, node, "json_rpc", body)
    if "error" in r:
        raise NodeError(f"{node}: {r['error']}")
    return r.get("result") or {}


def get_info(client: MixnetClient, node: str) -> dict:
    return rpc(client, node, "get_info")


def fee_estimate(client: MixnetClient, node: str) -> dict:
    return json_rpc(client, node, "get_fee_estimate")


def pool_hashes(client: MixnetClient, node: str) -> list[str]:
    r = rpc(client, node, "get_transaction_pool_hashes")
    if r.get("status") != "OK":
        raise NodeError(f"{node}: get_transaction_pool_hashes status {r.get('status')!r}")
    return r.get("tx_hashes") or []


@dataclass
class SendResult:
    node: str
    accepted: bool
    status: Optional[str]
    reason: str
    elapsed: Optional[float]


def _flags(r: dict) -> str:
    bad = [k for k in ("double_spend", "fee_too_low", "invalid_input", "invalid_output", "low_mixin",
                       "not_relayed", "overspend", "too_big", "too_few_outputs", "sanity_check_failed",
                       "tx_extra_too_big") if r.get(k)]
    if bad:
        return ", ".join(bad)
    if r.get("not_relayed"):
        return "node did not relay it (already in the chain or pool, or refused)"
    return r.get("reason") or ""


def send_one(client: MixnetClient, node: str, tx_hex: str, do_not_relay: bool = False) -> SendResult:
    """Submit one transaction to one node. ``accepted`` means status OK and not 'not_relayed'."""
    t0 = time.monotonic()
    try:
        r = rpc(client, node, "send_raw_transaction",
                {"tx_as_hex": tx_hex, "do_not_relay": do_not_relay})
    except (NodeError, MixnetError, OSError) as e:
        return SendResult(node, False, None, str(e), time.monotonic() - t0)
    ok = r.get("status") == "OK" and not r.get("not_relayed") and not do_not_relay
    return SendResult(node, ok, r.get("status"), _flags(r) or ("dry run, not relayed" if do_not_relay else ""),
                      time.monotonic() - t0)


def broadcast(client: MixnetClient, nodes: list[str], tx_hex: str, parallel: bool = True,
              do_not_relay: bool = False) -> list[SendResult]:
    """Send the same transaction to every node, each over its own mixnet stream."""
    if not parallel:
        return [send_one(client, n, tx_hex, do_not_relay) for n in nodes]
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as ex:
        return list(ex.map(lambda n: send_one(client, n, tx_hex, do_not_relay), nodes))
