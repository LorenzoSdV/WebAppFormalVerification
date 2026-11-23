#!/usr/bin/env python3
"""Validate an app model against recorded traces with TLC (generic).

    check.py --spec spec/TraceApp.tla --template spec/TraceApp.cfg.tmpl TRACE [TRACE|DIR ...]

The trace spec must EXTEND webkit/tla/WebTrace.tla. The .cfg template contains @TRACE@ (trace
path) and @CUT@ (number of events to match). A trace is ACCEPTED when TLC finds a behavior of
the model that matches every event (it reports TraceNotDone violated).

For a REJECTED trace the checker finds the largest number of events the model can match,
reads the matched set from TLC's counterexample, and reports the events that were ready to
match next but could not be: that is where model and implementation disagree. --explain also
prints what was recorded for those events (rows, error code, row count, status) and the model's
state after the matched prefix, so the two can be compared directly.

    check.py ... --violates NoOverbooking --invariants-module BookingInvariants TRACE

checks a reproduction instead: is there a behavior that matches the whole trace AND ends in a
state violating the invariant? REPRODUCED means the recorded run is that bug, as the model
defines it (step 7's "verify you triggered the right bug").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "Specula" / "lib"
TLA_LIB = REPO / "webkit" / "tla"
CLASSPATH = f"{LIB / 'tla2tools.jar'}{os.pathsep}{LIB / 'CommunityModules-deps.jar'}"
DONE_RE = re.compile(r"/\\ done = \{([^}]*)\}")


def tlc(spec: Path, template: str, trace: Path, cut: str, work: Path,
        invariant: str = "TraceNotDone") -> tuple[str, str]:
    cfg = work / "trace.cfg"
    text = template.replace("@TRACE@", str(trace.resolve())).replace("@CUT@", cut)
    if invariant != "TraceNotDone":
        text = text.replace("INVARIANT TraceNotDone", f"INVARIANT {invariant}")
    cfg.write_text(text)
    lib = os.pathsep.join([str(spec.resolve().parent), str(TLA_LIB)])
    cmd = ["java", "-XX:+UseParallelGC", "-Xss16m", f"-DTLA-Library={lib}", "-cp", CLASSPATH,
           "tlc2.TLC", "-config", str(cfg), "-workers", "auto", "-noGenerateSpecTE",
           "-metadir", str(work / "meta"), str(spec.resolve())]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=spec.resolve().parent, timeout=3600)
    out = p.stdout + p.stderr
    if f"Invariant {invariant} is violated" in out:
        return "matched", out
    if "Model checking completed. No error has been found." in out:
        return "unmatched", out
    return "error", out


RECORDED_FIELDS = ("kind", "phase", "args", "rows", "count", "err", "writes", "status", "resp", "limit1")
STATE_RE = re.compile(r"^State (\d+): .*$", re.M)


def last_state(out: str) -> str:
    """The last state of TLC's counterexample: the model state after the matched prefix."""
    starts = list(STATE_RE.finditer(out))
    if not starts:
        return ""
    block = out[starts[-1].start():]
    end = re.search(r"\n\s*\n", block)
    return (block[:end.start()] if end else block).strip()


def events(trace: Path) -> list[dict]:
    return [json.loads(line) for line in trace.read_text().splitlines()][1:]


def ready_after(evs: list[dict], done: set[int]) -> list[int]:
    """Events (1-based) whose predecessors are all matched, mirroring WebTrace.Ready."""
    out = []
    for j, ej in enumerate(evs, 1):
        if j in done:
            continue
        preds = [i for i, ei in enumerate(evs, 1)
                 if i != j and ((ei["actor"] == ej["actor"] and i < j) or ei["hi"] < ej["lo"])]
        if all(i in done for i in preds):
            out.append(j)
    return out


def check_violates(spec: Path, template: str, trace: Path, inv: str, inv_module: str) -> dict:
    """Does some behavior match the whole trace and end violating inv?"""
    with tempfile.TemporaryDirectory(prefix="wkrepro-") as tmp:
        work = Path(tmp)
        wrapper = work / "TraceViolates.tla"
        wrapper.write_text(f"---- MODULE TraceViolates ----\nEXTENDS {spec.stem}, {inv_module}\n"
                           f"MatchedAndViolated == ~(Cardinality(done) = TraceLen /\\ ~({inv}))\n====\n")
        verdict, out = tlc_wrapped(wrapper, spec, template, trace, work)
    result = {"trace": str(trace), "invariant": inv, "events": len(events(trace))}
    result["verdict"] = {"matched": "reproduced", "unmatched": "not_reproduced"}.get(verdict, "error")
    if verdict == "error":
        result["tlc_output"] = out[-4000:]
    return result


def tlc_wrapped(wrapper: Path, spec: Path, template: str, trace: Path, work: Path) -> tuple[str, str]:
    cfg = work / "trace.cfg"
    cfg.write_text(template.replace("@TRACE@", str(trace.resolve())).replace("@CUT@", "<- TraceLen")
                   .replace("INVARIANT TraceNotDone", "INVARIANT MatchedAndViolated"))
    lib = os.pathsep.join([str(spec.resolve().parent), str(TLA_LIB)])
    cmd = ["java", "-XX:+UseParallelGC", "-Xss16m", f"-DTLA-Library={lib}", "-cp", CLASSPATH,
           "tlc2.TLC", "-config", str(cfg), "-workers", "auto", "-noGenerateSpecTE",
           "-metadir", str(work / "meta"), str(wrapper)]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=work, timeout=3600)
    out = p.stdout + p.stderr
    if "Invariant MatchedAndViolated is violated" in out:
        return "matched", out
    if "Model checking completed. No error has been found." in out:
        return "unmatched", out
    return "error", out


def check(spec: Path, template: str, trace: Path) -> dict:
    evs = events(trace)
    with tempfile.TemporaryDirectory(prefix="wktrace-") as tmp:
        work = Path(tmp)
        verdict, out = tlc(spec, template, trace, "<- TraceLen", work)
        result: dict = {"trace": str(trace), "verdict": verdict, "events": len(evs)}
        if verdict == "error":
            result["tlc_output"] = out[-4000:]
        elif verdict == "unmatched":
            lo, hi = 0, len(evs) - 1          # lo events are known to match
            while lo < hi:
                mid = (lo + hi + 1) // 2
                v, _ = tlc(spec, template, trace, f"= {mid}", work)
                if v == "matched":
                    lo = mid
                else:
                    hi = mid - 1
            result["max_matched"] = lo
            done: set[int] = set()
            if lo > 0:
                _, out = tlc(spec, template, trace, f"= {lo}", work)
                found = DONE_RE.findall(out)
                if found:
                    done = {int(x) for x in found[-1].replace(" ", "").split(",") if x}
                result["state_at_stuck"] = last_state(out)
            result["stuck_at"] = [{"event": j, "actor": evs[j - 1]["actor"], "action": evs[j - 1]["action"],
                                   "recorded": {k: v for k, v in evs[j - 1].items()
                                                if k in RECORDED_FIELDS and v not in ("", [], {"none": 0}, False)}}
                                  for j in ready_after(evs, done)]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--explain", action="store_true",
                    help="for rejected traces, print the stuck events' recorded fields and the model state")
    ap.add_argument("--violates", help="invariant a reproduction trace must end up violating")
    ap.add_argument("--invariants-module", help="module defining the --violates invariant")
    ap.add_argument("traces", nargs="+", type=Path)
    args = ap.parse_args()
    template = args.template.read_text()
    traces = sorted(t for p in args.traces for t in ([p] if p.is_file() else p.glob("*.ndjson")))
    if args.violates:
        if not args.invariants_module:
            sys.exit("--violates needs --invariants-module")
        with ThreadPoolExecutor(args.jobs) as pool:
            results = list(pool.map(lambda t: check_violates(args.spec, template, t, args.violates,
                                                              args.invariants_module), traces))
        ok = sum(r["verdict"] == "reproduced" for r in results)
        print(f"{len(results)} traces: {ok} reproduce a violation of {args.violates}")
        for r in results:
            print(f"  {r['verdict'].upper():15} {Path(r['trace']).name} ({r['events']} events)"
                  + (f"\n{r['tlc_output']}" if r["verdict"] == "error" else ""))
        if args.report:
            args.report.write_text(json.dumps(results, indent=2))
        return 0 if ok == len(results) else 1
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda t: check(args.spec, template, t), traces))

    ok = sum(r["verdict"] == "matched" for r in results)
    print(f"{len(results)} traces: {ok} accepted, {len(results) - ok} not accepted")
    for r in results:
        name = Path(r["trace"]).name
        if r["verdict"] == "matched":
            print(f"  ACCEPTED  {name} ({r['events']} events)")
        elif r["verdict"] == "unmatched":
            stuck = ", ".join(f"#{s['event']} {s['actor']} {s['action']}" for s in r["stuck_at"])
            print(f"  REJECTED  {name}: model matches at most {r['max_matched']}/{r['events']} events; "
                  f"could not match next: {stuck}")
            if args.explain:
                for s in r["stuck_at"]:
                    print(f"      recorded #{s['event']} {s['actor']} {s['action']}: {json.dumps(s['recorded'])}")
                if r.get("state_at_stuck"):
                    print("      model state after the matched prefix:")
                    print("\n".join("        " + line for line in r["state_at_stuck"].splitlines()))
        else:
            print(f"  ERROR     {name}:\n{r['tlc_output']}")
    if args.report:
        args.report.write_text(json.dumps(results, indent=2))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
