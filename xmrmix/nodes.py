"""Public Monero node selection that respects the Nym exit policy.

Nym's network requesters follow the Nym exit policy, which does *not* allow the
usual monerod ports (18080, 18081, 18089). It does allow 80, 81, 443, 8008, 8080 and
8081-9080, and plenty of public nodes answer on 443 behind a TLS reverse proxy.
So xmrmix only ever picks nodes on a port the exit policy accepts.
"""

from __future__ import annotations

import json
import random
import urllib.parse
from typing import Iterable, Optional

from .mixhttp import MixnetClient

LIST_URL = "https://monero.fail/nodes.json"

# Used when the list cannot be fetched. All answered on 443 on 2026-10-16.
SEED_NODES = [
    "https://node.xmr.pub:443",
    "https://xmr.ducks.party:443",
    "https://node.privacyx.co:443",
    "https://node.chaoswg.dev:443",
    "https://xmr.carlslab.de:443",
    "https://monero.definitelynotafed.com:443",
]


def allowed_by_exit_policy(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    port = u.port or (443 if u.scheme == "https" else 80)
    return port in (80, 81, 443, 8008, 8080) or 8081 <= port <= 9080


def normalise(url: str) -> str:
    u = urllib.parse.urlsplit(url.strip())
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{u.scheme}://{u.hostname}:{port}"


def filter_urls(urls: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for url in urls:
        try:
            if not allowed_by_exit_policy(url):
                continue
            n = normalise(url)
        except ValueError:
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def discover(client: MixnetClient, network: str = "monero") -> list[str]:
    """Fetch the public node list over the mixnet and keep exit-policy-compatible clearnet nodes."""
    r = client.get(LIST_URL)
    if not r.ok:
        raise RuntimeError(f"node list HTTP {r.status}")
    clear = (json.loads(r.body).get(network) or {}).get("clear") or []
    return filter_urls(clear)


def pick(urls: list[str], k: int, exclude: Optional[set[str]] = None) -> list[str]:
    pool = [u for u in urls if not exclude or u not in exclude]
    random.shuffle(pool)
    return pool[:k]
