#!/usr/bin/env python3
"""Live eval of the pinned-conflict LLM check (S41). Not collected by pytest, not run in CI.

Calls ``llm_confirms_pin_conflict`` for every case in
``tests/fixtures/pin_check_cases.json`` against an OpenAI-compatible endpoint
and prints a table plus the pre-release gate:

  * 0 "yes" on the 24 legit facts the rc2 core dropped;
  * >= 10/12 in-band and >= 4/6 high-band reversals answered "yes".

Environment: PIN_EVAL_BASE_URL, PIN_EVAL_MODEL, PIN_EVAL_API_KEY. Only the key is never
printed; the model name is printed in the table header.

  PIN_EVAL_BASE_URL=https://.../v1 PIN_EVAL_MODEL=<model> PIN_EVAL_API_KEY=<key> \\
    python python/scripts/eval_pin_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from totalreclaw.agent import pin_check
from totalreclaw.agent.llm_client import LLMConfig

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pin_check_cases.json"


async def main() -> int:
    base, model, key = (os.environ.get(k, "") for k in
                        ("PIN_EVAL_BASE_URL", "PIN_EVAL_MODEL", "PIN_EVAL_API_KEY"))
    if not (base and model and key):
        print("set PIN_EVAL_BASE_URL, PIN_EVAL_MODEL and PIN_EVAL_API_KEY", file=sys.stderr)
        return 2
    cfg = LLMConfig(api_key=key, base_url=base, model=model, api_format="openai")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # One model, no extraction-model swap: pin the plan to the model given.
    os.environ["TOTALRECLAW_EXTRACTION_MODEL"] = model

    rows = []
    for case in data["cases"]:
        ans = await pin_check.llm_confirms_pin_conflict(cfg, data["pinned_text"], case["text"])
        word = {True: "yes", False: "no", None: "none"}[ans]
        rows.append((case, word))

    print(f"model={model}  cases={len(rows)}")
    print(f"{'case':<6} {'expect':<7} {'band':<5} {'cosine':>6}  answer")
    for case, word in rows:
        flag = ""
        if case["expect"] in ("yes", "no") and word != case["expect"]:
            flag = "  <-- differs"
        print(f"{case['id']:<6} {case['expect']:<7} {case['band']:<5} "
              f"{case['cosine_to_rc2_pin']:>6.3f}  {word}{flag}")

    legit = [(c, w) for c, w in rows if c["gate_legit"]]
    legit_yes = sum(1 for _, w in legit if w == "yes")
    inband = [(c, w) for c, w in rows if c["gate_reversal"] and c["band"] == "in"]
    high = [(c, w) for c, w in rows if c["gate_reversal"] and c["band"] == "high"]
    in_yes = sum(1 for _, w in inband if w == "yes")
    high_yes = sum(1 for _, w in high if w == "yes")
    ok = legit_yes == 0 and in_yes >= 10 and high_yes >= 4
    print()
    print(f"legit facts answered yes : {legit_yes}/{len(legit)} (need 0)")
    print(f"in-band reversals yes    : {in_yes}/{len(inband)} (need >= 10)")
    print(f"high-band reversals yes  : {high_yes}/{len(high)} (need >= 4)")
    print("GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
