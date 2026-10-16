# xmrmix

**Talk to public Monero nodes, and broadcast transactions, through the Nym mixnet.**

By apeir (https://apeir.dk/online/).

`xmrmix` is a small command-line tool and Python library (standard library only). It sends
monerod RPC requests (`get_info`, `get_fee_estimate`, `send_raw_transaction`, ...) to **public
Monero nodes through the [Nym](https://nym.com) mixnet**, so the node operator and anyone watching
your network never see the request come from your IP address. It also runs a local
monerod-compatible RPC gateway, so a wallet can use a public node over Nym without knowing anything
about Nym.

## Why

A Monero wallet that uses a remote node tells that node, from your IP address, which blocks you
fetch and which transaction you broadcast first. A public node (or a service watching it) that
logs *"this IP published transaction X first"* can link your network identity to your wallet. Tor
helps, but its exit traffic is low-latency and open to timing correlation. Nym packets are
fixed-size, delayed and reordered at every hop, with cover traffic.

I found no existing tool that puts *public-node RPC and broadcasts* for Monero on the Nym mixnet
(a GitHub search for Monero + Nym comes back empty). `xmrmix` is that tool.

## The detail that makes it work

Nym exit gateways follow the [Nym exit policy](https://nymtech.net/.wellknown/network-requester/exit-policy.txt),
which does **not** allow the usual monerod ports (18080, 18081, 18089). A naive "point a SOCKS5
proxy at a node" fails with *connection not allowed by ruleset*. Many public nodes also answer on
443 (TLS reverse proxy) or 80, which the policy does allow. `xmrmix` reads the public node list
(itself over the mixnet) and only picks nodes on exit-policy-compatible ports. Nodes on plain
`http://` (port 80) are in the list too; the exit gateway then sees that request in the clear
(a broadcast is public anyway, but not a wallet's block fetches), so pass `--node https://...`
to force TLS.

## How the mixnet is used

```
 xmrmix ──SOCKS5──> nym-socks5-client ──Sphinx packets──> entry gateway ──> 3 mix layers ──> exit gateway
 (your machine)      (your machine)       + cover traffic                                   network requester
                                                                                                │ TCP (TLS inside)
                                                                                                ▼
                                                                                       public Monero node
```

1. `xmrmix setup` asks the Nym API for exit gateways that are active, run a network requester and
   apply the exit policy, picks one at random, and initialises Nym's official `nym-socks5-client`
   with reply SURBs (the exit does not learn your mixnet address).
2. Every request opens a fresh SOCKS5 stream, chopped into Sphinx packets by the Nym client.
3. TLS is end to end between xmrmix and the node. DNS is resolved with DNS-over-HTTPS *over the
   mixnet*, so your local resolver never sees a node name.
4. **No clearnet fallback, ever.** If the SOCKS5 port is closed, xmrmix exits with an error
   (code 3) instead of connecting directly.
5. `broadcast` sends the transaction to several nodes in parallel, each over its own mixnet stream.

## Install

Requires Python 3.11+ and Nym's `nym-socks5-client` release binary
([releases](https://github.com/nymtech/nym/releases), check the sha256 against `hashes.json`).

```bash
pip install git+https://github.com/apeirs/xmrmix
export XMRMIX_NYM_SOCKS5=/path/to/nym-socks5-client
xmrmix setup --port 1081        # picks an exit, starts the client
xmrmix ip -v                    # shows the IP a web server sees: a Nym exit, not yours
```

## Use

```bash
xmrmix nodes                    # nodes reachable under the Nym exit policy
xmrmix info                     # height, difficulty, pool size from a random working node
xmrmix fee                      # fee estimate
xmrmix pool                     # transaction-pool size
xmrmix decode tx.hex            # offline sanity check of a serialized tx
xmrmix broadcast tx.hex         # to 3 nodes in parallel; exit code 0 if any relayed it
xmrmix broadcast tx.hex --dry-run
xmrmix gateway --port 18081     # local monerod-compatible RPC over Nym
monero-wallet-cli --daemon-address 127.0.0.1:18081 --untrusted-daemon
```

`broadcast` exit codes: 0 at least one node relayed it, 2 none did, 1 bad input, 3 no mixnet.
A node answering `not relayed` for a transaction that is already in the chain or pool is normal.

The gateway relays JSON and binary (`.bin`) endpoints unchanged and fails over between the nodes
it verified at start-up. It binds to loopback only (it has no authentication). A mixnet round trip
costs seconds, so it suits balance refreshes and broadcasts; a first full sync of a big wallet
will be slow.

## Verified live

`examples/live-run-2026-10-16.txt`: through a Nym exit in Italy, `ip` showed the exit's address,
`info`/`fee`/`pool` worked against public nodes on 443, `broadcast` of an already-confirmed
transaction reached three nodes and all declined to relay it (key images spent), and the gateway
served `get_height`, `json_rpc` and a binary `.bin` endpoint. The gateway has not yet been
tested with a real wallet binary. The 21 offline tests (`python -m unittest`) use in-process
fakes for the SOCKS5 proxy and the node.

## What it does not do

It does not sign transactions or touch keys, does not hide that you use Nym from your ISP, and
trusts the remote node for the data it returns (use `--untrusted-daemon` in the wallet). A
transaction's timing and size are visible to the node you send it to; xmrmix only hides *who*
sent it.

MIT licensed.
