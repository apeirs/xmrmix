"""xmrmix command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

from . import __version__, monerod, nymclient, nodes as nodemod, proxy
from . import tx as txmod
from .mixhttp import MixnetClient, MixnetError
from .socks import ProxyUnreachable

_T0 = time.monotonic()
IP_ECHO = ["https://api.ipify.org", "https://ifconfig.me/ip"]


def _trace(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:7.2f}s] {msg}", file=sys.stderr, flush=True)


def _proxy(args) -> str:
    if args.proxy:
        return args.proxy
    if os.environ.get("XMRMIX_PROXY"):
        return os.environ["XMRMIX_PROXY"]
    try:
        return nymclient.read_config(args.id)["core"]["socks5"]["bind_address"]
    except (OSError, KeyError):
        return "127.0.0.1:1080"


def _client(args) -> MixnetClient:
    return MixnetClient(proxy=_proxy(args), timeout=args.timeout, retries=args.retries, dns=args.dns,
                        trace=_trace if args.verbose else None)


def _out(args, obj, human: str) -> None:
    print(json.dumps(obj, indent=2) if args.json else human)


def _nodes(args, client) -> list[str]:
    """Explicit --node values, else the live public list (over the mixnet), else the built-in seeds."""
    if args.node:
        bad = [n for n in args.node if not nodemod.allowed_by_exit_policy(n)]
        if bad:
            raise ValueError(f"{bad[0]}: port not allowed by the Nym exit policy (use 80, 81, 443 or 8081-9080)")
        return [nodemod.normalise(n) for n in args.node]
    try:
        found = nodemod.discover(client)
        if found:
            return found
    except (MixnetError, OSError, RuntimeError, ValueError) as e:
        print(f"note: could not fetch the node list over the mixnet ({e}); using built-in seeds", file=sys.stderr)
    return list(nodemod.SEED_NODES)


def _first_working(args, client, candidates, want=1):
    """First `want` nodes that answer get_info, trying shuffled candidates."""
    good, tried = [], set()
    while len(good) < want:
        batch = nodemod.pick(candidates, max(want - len(good), 3), exclude=tried)
        if not batch:
            break
        tried.update(batch)
        for n in batch:
            try:
                info = monerod.get_info(client, n)
                if info.get("status") == "OK" and info.get("mainnet", True) and info.get("synchronized", True):
                    good.append((n, info))
                    if len(good) >= want:
                        break
            except (monerod.NodeError, MixnetError, OSError) as e:
                _trace(f"skip {n}: {e}") if args.verbose else None
    if len(good) < want:
        raise RuntimeError(f"found {len(good)} working node(s) of the {want} wanted")
    return good


# -- commands -------------------------------------------------------------
def cmd_exits(args):
    provs = nymclient.list_exit_providers(min_performance=args.min_performance)
    provs.sort(key=lambda p: -p.performance)
    if args.json:
        print(json.dumps([asdict(p) for p in provs], indent=2))
        return 0
    for p in provs[: args.limit]:
        print(f"{p.node_id:5} {p.country or '--':3} perf={p.performance:.2f} v{p.version}  {p.address}")
    print(f"({len(provs)} active exit gateways with a network requester)", file=sys.stderr)
    return 0


def cmd_setup(args):
    binary = nymclient.find_binary(args.binary)
    cfg = nymclient.config_path(args.id)
    if cfg.exists() and not args.force:
        print(f"client '{args.id}' already initialised ({cfg}); use --force to re-init", file=sys.stderr)
    else:
        if args.force and nymclient.running_pid(args.id):
            nymclient.stop(args.id)
        provider = args.provider
        if provider == "auto":
            p = nymclient.pick_provider(nymclient.list_exit_providers())
            print(f"exit gateway: node {p.node_id} ({p.country}) perf={p.performance:.2f}", file=sys.stderr)
            provider = p.address
        if args.force and cfg.exists():
            import shutil

            shutil.rmtree(cfg.parent.parent)
        nymclient.init(binary, args.id, provider, args.port, anonymous=not args.no_anonymous, gateway=args.gateway)
        print(f"initialised '{args.id}' -> {provider}", file=sys.stderr)
    return cmd_start(args)


def cmd_start(args):
    binary = nymclient.find_binary(args.binary)
    st = nymclient.start(binary, args.id)
    _out(args, st, "\n".join(f"{k:18} {v}" for k, v in st.items()))
    return 0


def cmd_stop(args):
    print("stopped" if nymclient.stop(args.id) else "not running")
    return 0


def cmd_status(args):
    st = nymclient.status(args.id)
    _out(args, st, "\n".join(f"{k:18} {v}" for k, v in st.items()))
    return 0


def cmd_ip(args):
    c = _client(args)
    errs = []
    for url in IP_ECHO:
        try:
            r = c.get(url)
            if r.ok:
                ip = r.text.strip()
                _out(args, {"service": url, "seen_ip": ip}, f"{url} sees this request coming from {ip} (a Nym exit gateway)")
                return 0
            errs.append(f"{url}: HTTP {r.status}")
        except MixnetError as e:
            errs.append(str(e))
    raise MixnetError("; ".join(errs))


def cmd_nodes(args):
    c = _client(args)
    found = _nodes(args, c)
    _out(args, found, "\n".join(found) + f"\n({len(found)} nodes on exit-policy-compatible ports)")
    return 0


def cmd_info(args):
    c = _client(args)
    n, info = _first_working(args, c, _nodes(args, c))[0]
    keep = {k: info.get(k) for k in ("height", "target_height", "top_block_hash", "difficulty", "tx_pool_size",
                                     "version", "synchronized", "mainnet", "status")}
    _out(args, {"node": n, **keep}, f"{n}\n" + "\n".join(f"{k:15} {v}" for k, v in keep.items()))
    return 0


def cmd_fee(args):
    c = _client(args)
    n, _ = _first_working(args, c, _nodes(args, c))[0]
    f = monerod.fee_estimate(c, n)
    per_byte = f.get("fee")
    _out(args, {"node": n, **f},
         f"{n}\nfee per byte  {per_byte} atomic units (quantization mask {f.get('quantization_mask')})\n"
         + "tiers          " + ", ".join(str(x) for x in (f.get("fees") or [])))
    return 0


def cmd_pool(args):
    c = _client(args)
    n, _ = _first_working(args, c, _nodes(args, c))[0]
    hs = monerod.pool_hashes(c, n)
    _out(args, {"node": n, "count": len(hs), "tx_hashes": hs[: args.limit]}, f"{n}: {len(hs)} transactions in the pool")
    return 0


def _read_tx(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if os.path.isfile(arg):
        with open(arg) as f:
            return f.read()
    return arg


def cmd_decode(args):
    info = txmod.parse(_read_tx(args.tx))
    _out(args, asdict(info), "\n".join(f"{k:12} {v}" for k, v in asdict(info).items()))
    return 0


def cmd_broadcast(args):
    raw = "".join(_read_tx(args.tx).split())
    info = txmod.parse(raw)  # fail fast, offline
    print(f"tx v{info.version}, {info.size} bytes, {info.n_inputs} input(s)", file=sys.stderr)
    if args.dry_run:
        print("dry run: not sending", file=sys.stderr)
        return 0
    c = _client(args)
    candidates = _nodes(args, c)
    chosen = nodemod.pick(candidates, args.nodes)
    if not chosen:
        raise RuntimeError("no node to send to")
    results = monerod.broadcast(c, chosen, raw, parallel=not args.sequential, do_not_relay=args.do_not_relay)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for r in results:
            mark = "ACCEPTED" if r.accepted else "REJECTED" if r.status else "NO REPLY"
            t = f"{r.elapsed:.2f}s" if r.elapsed is not None else "-"
            print(f"{mark:9} {r.node}  {t}  {r.reason}")
    return 0 if any(r.accepted for r in results) else 2


def cmd_gateway(args):
    c = _client(args)
    candidates = _nodes(args, c)
    chosen = [n for n, _ in _first_working(args, c, candidates, want=min(args.nodes, len(candidates)))]
    proxy.serve(c, chosen, args.listen, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xmrmix", description="Monero public-node traffic and transaction broadcast through the Nym mixnet.")
    p.add_argument("--version", action="version", version=f"xmrmix {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--id", default=nymclient.DEFAULT_ID, help="nym-socks5-client id (default: xmrmix)")
    common.add_argument("--proxy", help="SOCKS5 address of a running nym-socks5-client (default: from the client config, else 127.0.0.1:1080)")
    common.add_argument("--node", action="append", help="public node URL on port 80/81/443/8081-9080 (repeatable); default: the live public list")
    common.add_argument("--timeout", type=float, default=60.0, help="per-attempt timeout in seconds (default 60)")
    common.add_argument("--retries", type=int, default=1)
    common.add_argument("--dns", choices=["doh", "exit"], default="doh", help="doh: resolve via DNS-over-HTTPS over the mixnet, connect by IPv4 (default); exit: let the exit gateway resolve")
    common.add_argument("-v", "--verbose", action="store_true", help="trace the mixnet path and timings on stderr")
    common.add_argument("--json", action="store_true")
    common.add_argument("--binary", help="path to nym-socks5-client")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", parents=[common], help="init + start a nym-socks5-client on a random active exit gateway")
    s.add_argument("--provider", default="auto", help="network requester address, or 'auto'")
    s.add_argument("--gateway", help="entry gateway identity (default: chosen by nym)")
    s.add_argument("--port", type=int, default=nymclient.DEFAULT_PORT)
    s.add_argument("--no-anonymous", action="store_true", help="don't use reply SURBs (faster; the exit then learns our mixnet address)")
    s.add_argument("--force", action="store_true", help="wipe and re-init the client")
    s.set_defaults(fn=cmd_setup)
    sub.add_parser("start", parents=[common], help="start the managed nym-socks5-client").set_defaults(fn=cmd_start)
    sub.add_parser("stop", parents=[common], help="stop it").set_defaults(fn=cmd_stop)
    sub.add_parser("nym-status", parents=[common], help="show the mixnet client's addresses").set_defaults(fn=cmd_status)
    e = sub.add_parser("exits", parents=[common], help="list active exit gateways with a network requester (queries nym-api)")
    e.add_argument("--limit", type=int, default=20)
    e.add_argument("--min-performance", type=float, default=0.95)
    e.set_defaults(fn=cmd_exits)
    sub.add_parser("ip", parents=[common], help="show the IP that web servers see (should be a Nym exit)").set_defaults(fn=cmd_ip)
    sub.add_parser("nodes", parents=[common], help="list public nodes reachable under the Nym exit policy").set_defaults(fn=cmd_nodes)
    sub.add_parser("info", parents=[common], help="chain height etc. from a random working node").set_defaults(fn=cmd_info)
    sub.add_parser("fee", parents=[common], help="fee estimate from a random working node").set_defaults(fn=cmd_fee)
    pl = sub.add_parser("pool", parents=[common], help="size of a node's transaction pool")
    pl.add_argument("--limit", type=int, default=10)
    pl.set_defaults(fn=cmd_pool)
    d = sub.add_parser("decode", parents=[common], help="offline: sanity-check a serialized tx")
    d.add_argument("tx", help="hex, a file containing hex, or - for stdin")
    d.set_defaults(fn=cmd_decode)
    b = sub.add_parser("broadcast", parents=[common], help="send a signed tx (hex) to several public nodes, each over its own mixnet stream")
    b.add_argument("tx", help="hex, a file containing hex, or - for stdin")
    b.add_argument("--nodes", type=int, default=3, help="how many nodes to send to (default 3)")
    b.add_argument("--dry-run", action="store_true", help="parse the tx, send nothing")
    b.add_argument("--do-not-relay", action="store_true", help="ask the nodes to validate the tx but not relay it (monerod do_not_relay)")
    b.add_argument("--sequential", action="store_true")
    b.set_defaults(fn=cmd_broadcast)
    g = sub.add_parser("gateway", parents=[common], help="local monerod-compatible RPC endpoint backed by public nodes over Nym")
    g.add_argument("--listen", default="127.0.0.1")
    g.add_argument("--port", type=int, default=18081)
    g.add_argument("--nodes", type=int, default=3, help="upstream nodes to verify and fail over between (default 3)")
    g.set_defaults(fn=cmd_gateway)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ProxyUnreachable as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except (MixnetError, txmod.TxError, ValueError, FileNotFoundError, RuntimeError, TimeoutError, monerod.NodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
