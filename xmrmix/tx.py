"""Offline sanity check of a serialized Monero transaction before anything is sent."""

from __future__ import annotations

from dataclasses import dataclass


class TxError(ValueError):
    pass


@dataclass
class TxInfo:
    size: int
    version: int
    unlock_time: int
    n_inputs: int
    input_type: int  # 0xff = coinbase, 0x02 = to-key


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while True:
        if i >= len(b):
            raise TxError("truncated transaction (varint runs off the end)")
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise TxError("varint too long")


def parse(text: str) -> TxInfo:
    """Parse the tx prefix header. Rejects hex that cannot be a Monero transaction.

    This is only a guard against pasting the wrong thing (a txid, a Bitcoin tx,
    half a blob): the node does the real validation.
    """
    h = "".join(text.split())
    if h[:2] in ("0x", "0X"):
        h = h[2:]
    if len(h) == 64:
        raise TxError("that is 32 bytes: a transaction id, not a transaction blob")
    try:
        b = bytes.fromhex(h)
    except ValueError:
        raise TxError("not valid hex") from None
    if len(b) < 100:
        raise TxError(f"{len(b)} bytes is too short for a Monero transaction")
    version, i = _varint(b, 0)
    if version not in (1, 2):
        raise TxError(f"unsupported transaction version {version}")
    unlock, i = _varint(b, i)
    n_in, i = _varint(b, i)
    if n_in == 0 or n_in > 1000:
        raise TxError(f"implausible input count {n_in}")
    if i >= len(b) or b[i] not in (0x02, 0xFF):
        raise TxError("first input has an unknown type")
    return TxInfo(len(b), version, unlock, n_in, b[i])
