#!/usr/bin/env python3
"""Render a TLC counterexample of a WebDB-based model as a per-request web timeline (generic).

    timeline.py COUNTEREXAMPLE.json [--invariant NAME] [--schedule-out schedule.json]

Input: TLC's `-dumpTrace json FILE` output for a model that EXTENDS webkit/tla/WebDB.tla.
TLC's raw counterexample lists every variable in every state; this tool keeps what a web
engineer needs to judge it:

  - one lane per actor (request / job), one row per step, in the order TLC interleaved them;
  - each step described in SQL terms from WebDB's own variables (BEGIN at an isolation level,
    the rows a SELECT returned, an UPDATE/INSERT starting and finishing, lock waits, what a
    COMMIT changed, the HTTP status the handler chose);
  - interleaving points (where control switched between actors);
  - the committed rows that differ from the initial state, and the violated invariant.

TLC remains the checker and its counterexample the ground truth; this is a view of it.
--schedule-out writes the reproduction schedule for webkit/repro: the order in which each actor
issues its SQL statements (from WebDB's per-session statement counter `sq`), plus each actor's
inputs (the arguments of its arrival step), from which a repro script builds the HTTP requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WRITE_SQL = {"update": "UPDATE", "delete": "DELETE", "lock": "SELECT ... FOR UPDATE", "insert": "INSERT"}


def fmt_row(row) -> str:
    if row == "Absent":
        return "(none)"
    if isinstance(row, dict):
        return "{" + ", ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in row.items()) + "}"
    return str(row)


def fmt_rows(rows) -> str:
    if not rows:
        return "no rows"
    return "; ".join(f"#{k} {fmt_row(r)}" for k, r in rows)


def latest(ver, tb, k):
    return ver[tb][k - 1][-1][1]


def describe(action: str, actor: str, before: dict, after: dict) -> tuple[str, str]:
    """(kind, text) for one step, from WebDB's variables."""
    if actor == "db":
        locks = [(tb, i + 1, after["lk"][tb][i]) for tb in after["lk"] for i in range(len(after["lk"][tb]))
                 if after["lk"][tb][i] != before["lk"][tb][i] and after["lk"][tb][i] != "NoTxn"]
        if locks:
            return "lock", "; ".join(f"{who} locks {tb} #{k}" for tb, k, who in locks)
        fails = [t for t in after["res"] if after["res"][t] != before["res"][t] and not after["res"][t]["ok"]]
        if fails:
            return "abort", "; ".join(f"{t}: {after['res'][t]['code']}" for t in fails)
        return "db", "(internal step)"

    b_tx, a_tx = before["tx"][actor], after["tx"][actor]
    b_st, a_st = before["stmt"][actor], after["stmt"][actor]
    res = after["res"][actor]
    parts = []
    kind = "app"
    if b_tx["st"] == "none" and a_tx["st"] == "active" and not a_tx["auto"]:
        kind, parts = "begin", [f"BEGIN ({a_tx['iso']})"]
    elif b_st["kind"] == "none" and a_st["kind"] != "none":
        kind = "start"
        sql = WRITE_SQL.get(a_st["kind"], a_st["kind"].upper())
        if a_st["kind"] == "insert":
            parts = [f"{sql} {a_st['tb']} #{a_st['key']} {fmt_row(a_st['row'])} (starts)"]
        else:
            cands = sorted(set(a_st["todo"]) | set(a_st["own"]))
            parts = [f"{sql} {a_st['tb']} (starts; snapshot sees #{', #'.join(map(str, cands)) or 'none'})"]
    elif b_st["kind"] != "none" and a_st["kind"] == "none":
        kind = "finish"
        sql = WRITE_SQL.get(b_st["kind"], b_st["kind"].upper())
        parts = [f"{sql} {b_st['tb']} -> " + (fmt_rows(res["rows"]) if res["ok"] else f"ERROR {res['code']}")]
    elif before["ver"] != after["ver"] or (b_tx["st"] != "none" and a_tx["st"] == "none"):
        kind = "commit" if res.get("tag") == "COMMIT" or before["ver"] != after["ver"] else "end"
        changes = []
        for tb in after["ver"]:
            for i, (bv, av) in enumerate(zip(before["ver"][tb], after["ver"][tb])):
                if bv != av:
                    changes.append(f"{tb} #{i + 1}: {fmt_row(bv[-1][1])} => {fmt_row(av[-1][1])}")
        tag = res.get("tag") or ("ERROR " + res["code"] if not res["ok"] else "")
        parts = [tag or "COMMIT"] + (["commits " + "; ".join(changes)] if changes else [])
    elif before["res"][actor] != res or (b_st["kind"] == "none" and before["res"][actor] == res and "Select" in action):
        kind = "select"
        parts = ["SELECT -> " + (fmt_rows(res["rows"]) if res["ok"] else f"ERROR {res['code']}")]
    if "rs" in after and after["rs"][actor] != before["rs"][actor] and after["rs"][actor]["status"]:
        r = after["rs"][actor]
        parts.append(f"HTTP {r['status']}" + (f" (id {r['id']})" if r.get("id", -1) not in (-1, 0) else ""))
    return kind, "; ".join(parts) or "(handler step)"


def changed_rows(first: dict, last: dict) -> list[str]:
    out = []
    for tb in last["ver"]:
        for i in range(len(last["ver"][tb])):
            a, b = latest(first["ver"], tb, i + 1), latest(last["ver"], tb, i + 1)
            if a != b:
                out.append(f"{tb} #{i + 1}: {fmt_row(a)} => {fmt_row(b)}")
    return out


WEBDB_VARS = {"ver", "clock", "tx", "wr", "lk", "stmt", "res", "sread", "serlog", "sq"}


def app_actor_vars(state: dict) -> list[str]:
    """The model's own per-actor variables (functions keyed by the actors), not WebDB's."""
    actors = set(state["sq"])
    return [v for v, val in state.items()
            if v not in WEBDB_VARS and isinstance(val, dict) and val and set(val) == actors]


def infer_actor(before: dict, after: dict) -> str:
    """The actor whose own app variables (pc, inputs, locals, response) changed in this step; "db"
    when only WebDB's variables changed (lock grants, deadlock and serialization failures). Used when
    the action has no actor parameter, e.g. a model whose Next is a wrapper action."""
    changed = sorted({a for v in app_actor_vars(after) for a in after[v] if after[v][a] != before[v].get(a)})
    if len(changed) == 1:
        return changed[0]
    if not changed:
        statement = sorted(a for a in after["sq"] if after["sq"][a] != before["sq"][a])
        return statement[0] if len(statement) == 1 else "db"
    return changed[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ce", type=Path)
    ap.add_argument("--invariant", default="")
    ap.add_argument("--actor-param", default="r",
                    help="name of the action parameter that identifies the actor (inferred from the state "
                         "change when the action has none)")
    ap.add_argument("--schedule-out", type=Path)
    args = ap.parse_args()

    ce = json.loads(args.ce.read_text())["counterexample"]
    steps = []
    inputs: dict[str, dict] = {}
    for (_, before), info, (_, after) in ce["action"]:
        ctx = info.get("context", {})
        actor = ctx.get(args.actor_param) or infer_actor(before, after)
        kind, text = describe(info["name"], actor, before, after)
        issued = actor != "db" and after["sq"][actor] > before["sq"][actor]
        if actor != "db" and before["sq"][actor] == 0 and not issued and actor not in inputs:
            given = {k: v for k, v in ctx.items() if k != args.actor_param}
            # Without action parameters (a wrapper action), take the actor's own app state after its
            # first step (e.g. its request record) as its inputs.
            inputs[actor] = given or {v: after[v][actor] for v in app_actor_vars(after)
                                      if after[v][actor] != before[v][actor]}
        # The model lets the statement wait on locks only if it is still in flight after the step.
        may_wait = issued and after["stmt"][actor]["kind"] != "none"
        steps.append({"actor": actor, "action": info["name"], "kind": kind, "text": text,
                      "statement": issued, "may_wait": may_wait,
                      "line": info.get("location", {}).get("beginLine")})
    first = ce["action"][0][0][1]
    last = ce["action"][-1][2][1]
    actors = sorted({s["actor"] for s in steps if s["actor"] != "db"}) + ["db"]

    print(f"Counterexample: {len(steps)} steps, actors {', '.join(a for a in actors if a != 'db')}")
    if args.invariant:
        print(f"Violated invariant: {args.invariant}")
    print()
    prev = None
    for n, s in enumerate(steps, 1):
        switch = "  <- interleaving" if prev not in (None, s["actor"]) and s["kind"] != "app" else ""
        indent = "    " * actors.index(s["actor"])
        print(f"{n:3} {indent}[{s['actor']}] {s['action']}: {s['text']}{switch}")
        if s["actor"] != "db":
            prev = s["actor"]
    print("\nCommitted rows that differ from the initial state:")
    for line in changed_rows(first, last) or ["(none)"]:
        print(f"  {line}")

    if args.schedule_out:
        order = [{"step": n, "actor": s["actor"], "action": s["action"], "text": s["text"],
                  "may_wait": s["may_wait"]}
                 for n, s in enumerate(steps, 1) if s["statement"]]
        sched = {"invariant": args.invariant, "actors": sorted(inputs), "inputs": inputs, "order": order}
        args.schedule_out.write_text(json.dumps(sched, indent=2) + "\n")
        print(f"\nReproduction schedule ({len(order)} statements, in counterexample order): {args.schedule_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
