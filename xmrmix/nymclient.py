"""Set up and run the official ``nym-socks5-client`` (Nym release binary).

xmrmix does not reimplement the mixnet: it drives Nym's own client, which
does the Sphinx packet construction, cover traffic, Poisson delays and
gateway handling, and exposes a SOCKS5 port that xmrmix talks to.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import subprocess
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

NYM_API = "https://validator.nymtech.net/api"
DEFAULT_ID = "xmrmix"
DEFAULT_PORT = 1080
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def nym_home() -> Path:
    return Path(os.environ.get("NYM_HOME", Path.home() / ".nym"))


def config_path(client_id: str) -> Path:
    return nym_home() / "socks5-clients" / client_id / "config" / "config.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "xmrmix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_binary(explicit: Optional[str] = None) -> str:
    for cand in (explicit, os.environ.get("XMRMIX_NYM_SOCKS5"), shutil.which("nym-socks5-client")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise FileNotFoundError(
        "nym-socks5-client not found. Download it from "
        "https://github.com/nymtech/nym/releases (nym-binaries-*), check its sha256 "
        "against the release's hashes.json, chmod +x it, then pass --binary PATH "
        "or set XMRMIX_NYM_SOCKS5."
    )


@dataclass
class ExitProvider:
    node_id: int
    country: Optional[str]
    performance: float
    version: str
    address: str


def list_exit_providers(nym_api: str = NYM_API, min_performance: float = 0.95) -> list[ExitProvider]:
    """Network requesters on currently *active* exit gateways that use the exit policy.

    Note: this queries the public nym-api directly (not through the mixnet),
    exactly like nym-socks5-client itself does to fetch the topology. It
    reveals that you use Nym, nothing about any transaction.
    """

    def get(path):
        req = urllib.request.Request(nym_api.rstrip("/") + path, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    skimmed = get("/v1/unstable/nym-nodes/skimmed/exit-gateways/all")["nodes"]["data"]
    described = {n["node_id"]: n["description"] for n in get("/v1/nym-nodes/described?per_page=5000")["data"]}
    out = []
    for n in skimmed:
        if n.get("role") != "ExitGateway" or float(n.get("performance", 0)) < min_performance:
            continue
        d = described.get(n["node_id"])
        nr = (d or {}).get("network_requester") or {}
        if not nr.get("address") or not nr.get("uses_exit_policy"):
            continue
        out.append(
            ExitProvider(
                n["node_id"],
                (d.get("auxiliary_details") or {}).get("location"),
                float(n["performance"]),
                (d.get("build_information") or {}).get("build_version", "?"),
                nr["address"],
            )
        )
    return out


def patch_config(client_id: str) -> bool:
    """Work around nym-socks5-client 1.1.86 (v2026.19) writing a config it can't read.

    Its config template omits ``credential_requests_database`` while the loader
    requires it, so ``run`` fails right after ``init`` with
    "missing field `credential_requests_database`". Add the default path.
    Returns True if the file was changed.
    """
    p = config_path(client_id)
    text = p.read_text()
    if re.search(r"^credential_requests_database\s*=", text, re.M):
        return False
    m = re.search(r"^credentials_database = '(.*)/credentials_database\.db'\s*$", text, re.M)
    if not m:
        return False
    line = f"credential_requests_database = '{m.group(1)}/credential_requests.db'"
    p.write_text(text[: m.end()] + "\n" + line + text[m.end() :])
    return True


def init(binary: str, client_id: str, provider: str, port: int, anonymous: bool, gateway: Optional[str] = None) -> str:
    cmd = [binary, "init", "--id", client_id, "--provider", provider, "--port", str(port),
           "--use-reply-surbs", "true" if anonymous else "false"]
    if gateway:
        cmd += ["--gateway", gateway]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    text = _ANSI.sub("", out.stdout + out.stderr)
    if out.returncode != 0:
        raise RuntimeError(f"nym-socks5-client init failed:\n{text[-2000:]}")
    patch_config(client_id)
    return text


def read_config(client_id: str) -> dict:
    with open(config_path(client_id), "rb") as f:
        return tomllib.load(f)


def _pidfile(client_id: str) -> Path:
    return state_dir() / f"{client_id}.pid"


def logfile(client_id: str) -> Path:
    return state_dir() / f"{client_id}.log"


def running_pid(client_id: str) -> Optional[int]:
    try:
        pid = int(_pidfile(client_id).read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(binary: str, client_id: str, wait: float = 120.0) -> dict:
    """Start ``nym-socks5-client run`` in the background and wait until it is ready."""
    if running_pid(client_id):
        return status(client_id)
    patch_config(client_id)
    log = logfile(client_id)
    fh = open(log, "w")
    proc = subprocess.Popen([binary, "run", "--id", client_id], stdout=fh, stderr=subprocess.STDOUT,
                            start_new_session=True)
    _pidfile(client_id).write_text(str(proc.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"nym-socks5-client exited early:\n{_ANSI.sub('', log.read_text())[-2000:]}")
        if "Client startup finished" in log.read_text():
            return status(client_id)
        time.sleep(0.5)
    raise TimeoutError(f"nym-socks5-client not ready after {wait}s; see {log}")


def stop(client_id: str) -> bool:
    pid = running_pid(client_id)
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    _pidfile(client_id).unlink(missing_ok=True)
    return True


def status(client_id: str) -> dict:
    cfg = read_config(client_id)
    socks_cfg = cfg.get("core", {}).get("socks5", {})
    info = {
        "id": client_id,
        "pid": running_pid(client_id),
        "socks5": socks_cfg.get("bind_address"),
        "provider": socks_cfg.get("provider_mix_address"),
        "anonymous_replies": socks_cfg.get("send_anonymously"),
        "client_address": None,
        "gateway": None,
        "log": str(logfile(client_id)),
    }
    try:
        text = _ANSI.sub("", logfile(client_id).read_text())
        m = re.findall(r"The address of this client is: (\S+)", text)
        if m:
            info["client_address"] = m[-1]
            info["gateway"] = m[-1].rsplit("@", 1)[-1]
    except OSError:
        pass
    return info


def pick_provider(providers: list[ExitProvider]) -> ExitProvider:
    if not providers:
        raise RuntimeError("nym-api lists no active exit gateway with a network requester")
    # Random rather than "best": spreads load and keeps the choice unpredictable.
    return random.choice(providers)
