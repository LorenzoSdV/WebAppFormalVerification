#!/usr/bin/env python3
"""Coverage gate for web traces (generic): did the workloads exercise the model?

    coverage.py --mapping mapping.toml TRACES_DIR [--json report.json]

Passes when both hold:

1. Every action the mapping defines appears in at least one trace: <Route>_Begin / <Route>_End per
   route, <Sql> per select/begin/commit/rollback rule, <Sql>_start / <Sql>_finish per write/insert rule.
   An action that cannot occur through the public API can be exempted in the mapping, with a reason
   that says why (reasons like "not tested" or "not in the workloads" are rejected):

       [coverage.exempt]
       Rollback = "the app never issues ROLLBACK: errors end the transaction by closing it"

2. At least one scenario was recorded as concurrent, and every scenario the runner recorded as concurrent (TRACES_DIR/windows.json, from Client.together)
   has at least one trace with **overlapping actors**: one request issued a SQL statement while
   another was between two of its own statements. Without it only sequential behavior is validated.

Exit status 0 when the gate passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

BOUNDARY = {"request", "response"}
# An exemption must say why the public API cannot reach the action. "Not tested" is a gap in the
# workloads, which is exactly what this gate exists to catch.
NOT_A_REASON = re.compile(
    r"not (yet )?(tested|covered|exercised|reached|recorded|triggered)|untested|uncovered|unexercised"
    r"|no workload|not in (the )?(current )?workloads?|workloads? (do|does) not|skipped|todo|later",
    re.IGNORECASE,
)


def mapped_actions(mapping: dict) -> set[str]:
    actions: set[str] = set()
    for r in mapping.get("route", []):
        actions |= {f"{r['action']}_Begin", f"{r['action']}_End"}
    for s in mapping.get("sql", []):
        if s["kind"] in ("write", "insert"):
            actions |= {f"{s['action']}_start", f"{s['action']}_finish"}
        else:
            actions.add(s["action"])
    return actions


def read_trace(path: Path) -> list[dict]:
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in lines if e.get("ev") != "init"]


def overlapping_pairs(events: list[dict]) -> list[tuple[str, str]]:
    """Pairs (a, b): b issued a statement strictly between a's first and last statement."""
    sql: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("kind") not in BOUNDARY:
            sql[e["actor"]].append(e)
    spans = {a: (min(e["lo"] for e in es), max(e["hi"] for e in es)) for a, es in sql.items() if len(es) >= 2}
    pairs = []
    for a, (lo, hi) in spans.items():
        for b, es in sql.items():
            if b != a and any(lo < e["lo"] < hi for e in es):
                pairs.append((a, b))
    return pairs


def check(mapping_path: Path, traces: Path) -> dict:
    with mapping_path.open("rb") as f:
        mapping = tomllib.load(f)
    exempt = mapping.get("coverage", {}).get("exempt", {})
    wanted = mapped_actions(mapping)
    windows_file = traces / "windows.json"
    windows = json.loads(windows_file.read_text()) if windows_file.exists() else {}

    seen: dict[str, int] = defaultdict(int)
    by_scenario: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    files = sorted(traces.glob("*.ndjson"))
    for path in files:
        events = read_trace(path)
        for e in events:
            seen[e["action"]] += 1
        w = windows.get(path.stem)
        if isinstance(w, dict) and w.get("concurrent"):
            by_scenario[w.get("scenario", path.stem)].append((path.stem, bool(overlapping_pairs(events))))

    missing = sorted(a for a in wanted if not seen.get(a) and a not in exempt)
    unknown_exempt = sorted(set(exempt) - wanted)
    bad_reasons = sorted(a for a, why in exempt.items()
                         if not isinstance(why, str) or len(why.strip()) < 15 or NOT_A_REASON.search(why))
    no_overlap = sorted(s for s, ts in by_scenario.items() if not any(ok for _, ok in ts))
    return {
        "traces": len(files),
        "actions": {a: seen.get(a, 0) for a in sorted(wanted)},
        "missing_actions": missing,
        "exempt": exempt,
        "unknown_exempt": unknown_exempt,
        "bad_exemption_reasons": bad_reasons,
        "concurrent_scenarios": {s: [t for t, ok in ts if ok] for s, ts in sorted(by_scenario.items())},
        "scenarios_without_overlap": no_overlap,
        "ok": bool(files) and bool(by_scenario) and not missing and not no_overlap and not unknown_exempt
              and not bad_reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("traces", type=Path)
    ap.add_argument("--json", type=Path, help="also write the report as JSON")
    args = ap.parse_args()
    report = check(args.mapping, args.traces)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    covered = sum(1 for n in report["actions"].values() if n)
    exempt = f", {len(report['exempt'])} exempt" if report["exempt"] else ""
    print(f"coverage: {report['traces']} traces; {covered}/{len(report['actions'])} mapped actions seen{exempt}")
    for a in report["missing_actions"]:
        print(f"  MISSING  {a}: in no trace (add a workload that reaches it, or exempt it with a reason)")
    for a in report["unknown_exempt"]:
        print(f"  BAD EXEMPTION  {a}: not an action of this mapping")
    for a in report["bad_exemption_reasons"]:
        print(f"  BAD EXEMPTION  {a}: the reason must explain why the public API cannot reach it "
              f"({report['exempt'][a]!r}); a workload gap is not a reason")
    for s, ts in report["concurrent_scenarios"].items():
        if ts:
            print(f"  OK       {s}: overlapping actors in {len(ts)} trace(s)")
        else:
            print(f"  NO OVERLAP  {s}: concurrent scenario, but no trace has overlapping actors "
                  "(repeat it, or make the requests contend for the same rows)")
    if not report["concurrent_scenarios"]:
        print("  FAIL     no concurrent scenarios recorded (Client.together): interleavings would be unvalidated")
    if not report["traces"]:
        print("  FAIL     no traces")
    print("coverage gate: " + ("PASS" if report["ok"] else "FAIL"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
