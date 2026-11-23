#!/usr/bin/env python3
"""Live cost of a run's Codex sessions, from Codex's own token counters.

    codex_cost.py CODEX_HOME --prices IN,CACHED,OUT [--json]

Reads every session file under CODEX_HOME/sessions (one per agent session, including parallel
confirmation workers), takes the latest cumulative token_count of each, and prices it:
  (input - cached) * IN + cached * CACHED + output * OUT     (USD per million tokens)
Codex's input_tokens include the cached ones, and output_tokens include reasoning tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def session_totals(path: Path) -> dict | None:
    last = None
    with path.open(errors="replace") as f:
        for line in f:
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = (d.get("payload") or d).get("info") or {}
            if info.get("total_token_usage"):
                last = info["total_token_usage"]
    return last


def cost(codex_home: Path, prices: tuple[float, float, float]) -> dict:
    p_in, p_cached, p_out = prices
    totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "sessions": 0}
    for f in sorted((codex_home / "sessions").rglob("*.jsonl")):
        t = session_totals(f)
        if not t:
            continue
        totals["sessions"] += 1
        totals["input"] += t.get("input_tokens", 0)
        totals["cached"] += t.get("cached_input_tokens", 0)
        totals["output"] += t.get("output_tokens", 0)
        totals["reasoning"] += t.get("reasoning_output_tokens", 0)
    uncached = max(0, totals["input"] - totals["cached"])
    totals["usd"] = round((uncached * p_in + totals["cached"] * p_cached + totals["output"] * p_out) / 1e6, 4)
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codex_home", type=Path)
    ap.add_argument("--prices", required=True, help="USD per million tokens: input,cached_input,output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    prices = tuple(float(x) for x in args.prices.split(","))
    if len(prices) != 3:
        sys.exit("--prices needs three numbers: input,cached_input,output")
    t = cost(args.codex_home, prices)  # type: ignore[arg-type]
    if args.json:
        print(json.dumps(t))
    else:
        print(f"${t['usd']:.2f}  ({t['sessions']} sessions; input {t['input']/1e6:.2f}M of which cached "
              f"{t['cached']/1e6:.2f}M; output {t['output']/1e3:.0f}K incl. reasoning {t['reasoning']/1e3:.0f}K)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
