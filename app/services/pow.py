"""Proof-of-work solver for PUBLIC enrollment (counterpart of server/services/pow.py).

Finds ``nonce`` such that SHA256(challenge | device_id | nonce) has >= ``bits`` leading zero bits. Pure stdlib (no
dependency). Takes ~0.3 s at 18 bits on a desktop; roughly 5-10x longer on a phone, once per install (more only while the
server is under a registration flood). Always run it OFF the UI thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Optional


def solve(challenge: str, device_id: str, bits: int, stop: Optional[threading.Event] = None) -> int:
    base = hashlib.sha256(f"{challenge}|{device_id}|".encode())
    shift = 256 - bits
    n = 0
    while True:
        if stop is not None and n % 4096 == 0 and stop.is_set():
            raise InterruptedError("proof-of-work cancelled")
        h = base.copy()
        h.update(str(n).encode())
        if int.from_bytes(h.digest(), "big") >> shift == 0:
            return n
        n += 1


def verify(challenge: str, device_id: str, nonce: int, bits: int) -> bool:
    d = hashlib.sha256(f"{challenge}|{device_id}|{nonce}".encode()).digest()
    return int.from_bytes(d, "big") >> (256 - bits) == 0
