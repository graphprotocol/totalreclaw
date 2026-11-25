"""Measure #646 / #650 on the fake world, with the pre-fix or the fixed client.

    python -m tests.fakes.repro_646_650

Needs only ``build_and_send_userop`` and the ``_now`` / ``_sleep`` seams, so
the same script runs against ``14257b7`` (with the seams added by sed) and
against the fix. Prints one JSON object per scenario: the AA25 and AA20
errors the paymaster / bundler returned, writes that failed, virtual seconds.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import totalreclaw.userop as userop
from tests.fakes.fake_erc4337 import SENDER_DEFAULT as S
from tests.fakes.fake_erc4337 import VirtualClock, World

KEY = bytes([7]) * 32


async def _one(payload: bytes) -> str:
    return await userop.build_and_send_userop(
        sender=S, eoa_address="0x" + "22" * 20, eoa_private_key=KEY,
        protobuf_payload=payload, relay_url="http://relay.test",
        auth_key_hex="ab" * 32, wallet_address=S, chain_id=100,
    )


def _report(name: str, w: World, failed: int, t0: float) -> dict:
    errs = w.sponsor_errs + w.send_errs
    return {
        "scenario": name,
        "aa25": sum("AA25" in m for m in errs),
        "aa20": sum("AA20" in m for m in errs),
        "write_errors": failed,
        "sponsor_calls": sum(1 for m, _ in w.calls if m == "pm_sponsorUserOperation"),
        "sends": w.send_count,
        "virtual_seconds": round(w.now() - t0, 1),
    }


async def u1(mp) -> dict:
    """#646: 15 sequential single writes with a 3 s LLM turn between them."""
    w = World()
    VirtualClock(mp, w)
    a = w.acct(S)
    a.code_history.append((0, True))
    a.nonce_history.append((0, 5))
    failed = 0
    t0 = w.now()
    for i in range(15):
        try:
            await _one(b"r1-%02d" % i)
        except Exception:
            failed += 1
        w.advance(3)
    w.advance(60)
    return {**_report("U1 15 sequential writes (#646)", w, failed, t0),
            "executed": w.op_count_executed(S)}


async def u2(mp) -> dict:
    """#650: fresh account, first write deploys, second write 0.7 s later."""
    w = World()
    VirtualClock(mp, w)
    failed = 0
    t0 = w.now()
    await _one(b"r2-a")
    w.advance(0.7)
    try:
        await _one(b"r2-b")
    except Exception:
        failed += 1
    w.advance(60)
    return {**_report("U2 fresh account, immediate second write (#650)", w, failed, t0),
            "executed": w.op_count_executed(S)}


def main() -> None:
    for scenario in (u1, u2):
        with pytest.MonkeyPatch.context() as mp:
            print(json.dumps(asyncio.run(scenario(mp))))


if __name__ == "__main__":
    main()
