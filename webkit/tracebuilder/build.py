#!/usr/bin/env python3
"""Build a model-level trace from a webkit recording (generic; driven by a mapping file).

    build.py RUN_DIR --mapping mapping.toml --since-ns T0 [--until-ns T1] --out trace.ndjson

Reads the recorders' output in RUN_DIR (inbound.ndjson, pgproxy.ndjson, cdc.ndjson) and writes
one NDJSON trace for a TLA+ trace spec built on webkit/tla/WebTrace.tla:

  line 1   {"ev": "init", "tables": {...}, "seq": {...}, "actors": [...], "maxkey": n}
             committed state when the window starts (replayed from logical decoding), and the
             lowest possible next value of each table's id sequence
  then     one event per model action, in time order:
             <Route>_Begin / <Route>_End   per HTTP request (inputs; response status and fields)
             <SqlAction>                   per SELECT / BEGIN / COMMIT / ROLLBACK
             <SqlAction>_start, _finish    per UPDATE / DELETE / INSERT (which can wait on locks)

Request events list in "absent" the mapped body fields and headers the request did not send (their
args hold the same placeholder as an empty value). Every event has an interval [lo, hi] in which it took effect. Events of one actor are in
program order; events of different actors are ordered only when their intervals don't overlap.
COMMIT events carry the rows the transaction committed (joined from logical decoding by xid),
so the trace spec can check the model's committed state, not just statement results.

The mapping file is the only app-specific input (format: the harness-generation skill).
Unmapped requests or statements are reported and fail the build.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

NONE_INT = -1
NONE_TEXT = ""
COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


class BuildError(SystemExit):
    """Unmapped requests or statements, or records that cannot be joined (exits like sys.exit)."""


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def convert(value, typ: str):
    if value is None:
        return NONE_INT if typ == "int" else NONE_TEXT
    if typ == "int":
        return int(value)
    if typ == "bool":
        return value in (True, "t", "true", "1", 1)
    return str(value)


class Mapping:
    def __init__(self, path: Path) -> None:
        with path.open("rb") as f:
            m = tomllib.load(f)
        self.tables: dict[str, dict] = m.get("tables", {})
        self.routes = [dict(r, _re=re.compile(r["match"])) for r in m.get("route", [])]
        self.sql = [dict(s, _re=re.compile(s["match"])) for s in m.get("sql", [])]

    def project(self, table: str, row: dict) -> dict:
        """Keep the modeled columns of a row (given as column -> raw value), plus its key as k."""
        spec = self.tables[table]
        out = {"k": int(row[spec["key"]])}
        for col, typ in spec["columns"].items():
            out[col] = convert(row.get(col), typ)
        return out

    def route(self, method: str, path: str):
        target = f"{method} {path.split('?', 1)[0]}"
        for r in self.routes:
            m = r["_re"].match(target)
            if m:
                return r, m.groupdict()
        return None, None

    def statement(self, sql: str, route_action: str):
        for s in self.sql:
            if s.get("route") not in (None, route_action):
                continue
            if s["_re"].search(sql):
                return s
        return None


def initial_state(cdc: list[dict], mapping: Mapping, since: int) -> dict[str, dict[int, dict]]:
    """Replay committed transactions before the window, in commit order."""
    state: dict[str, dict[int, dict]] = {t: {} for t in mapping.tables}
    for txn in sorted((e for e in cdc if e.get("ev") == "txn" and e["commit_time_ns"] < since),
                      key=lambda e: e["commit_lsn_int"]):
        for ch in txn["changes"]:
            t = ch["table"]
            if t not in state:
                continue
            key = mapping.tables[t]["key"]
            if ch["op"] == "truncate":
                state[t].clear()
            elif ch["op"] in ("insert", "update"):
                row = mapping.project(t, ch["new"])
                state[t][row["k"]] = row
            elif ch["op"] == "delete" and ch.get("old"):
                state[t].pop(int(ch["old"][key]), None)
    return state


def committed_writes(cdc_txn: dict, mapping: Mapping) -> list[dict]:
    out = []
    for ch in cdc_txn["changes"]:
        t = ch["table"]
        if t not in mapping.tables:
            continue
        key = mapping.tables[t]["key"]
        if ch["op"] in ("insert", "update"):
            row = mapping.project(t, ch["new"])
            out.append({"table": t, "k": row["k"], "present": True, "row": row})
        elif ch["op"] == "delete":
            k = int(ch["old"][key])
            out.append({"table": t, "k": k, "present": False, "row": {"k": k}})
    return out


def build(run: Path, mapping: Mapping, since: int, until: int | None) -> list[dict]:
    inbound = read(run / "inbound.ndjson")
    proxy = read(run / "pgproxy.ndjson")
    cdc = read(run / "cdc.ndjson")

    in_window = lambda t: t >= since and (until is None or t < until)  # noqa: E731
    requests = sorted((e for e in inbound if e["ev"] == "request" and in_window(e["t_start_ns"])),
                      key=lambda e: e["t_start_ns"])
    responses = {e["trace_id"]: e for e in inbound if e["ev"] == "response"}
    actors = {r["trace_id"]: f"r{i + 1}" for i, r in enumerate(requests)}

    stmts_by_trace: dict[str, list[dict]] = defaultdict(list)
    for e in proxy:
        if e["ev"] == "stmt" and e.get("trace_id") in actors:
            stmts_by_trace[e["trace_id"]].append(e)
    txn_ends = {(e["conn"], e["txn"]): e for e in proxy if e["ev"] == "txn_end"}
    cdc_by_xid = {str(e["xid"]): e for e in cdc if e.get("ev") == "txn"}

    errors: list[str] = []
    events: list[dict] = []
    inserts: dict[str, int] = defaultdict(int)
    max_key = 0

    def ev(actor: str, action: str, kind: str, lo: int, hi: int, **extra) -> dict:
        base = {"actor": actor, "action": action, "kind": kind, "phase": "", "lo": lo, "hi": hi,
                "limit1": False, "args": {"none": 0}, "absent": [], "rows": [], "has_rows": False, "count": 0,
                "err": "", "writes": [], "status": 0, "resp": {"none": 0}}
        base.update(extra)
        return base

    for req in requests:
        tid, actor = req["trace_id"], actors[req["trace_id"]]
        route, groups = mapping.route(req["method"], req["path"])
        if route is None:
            errors.append(f"{actor}: unmapped request {req['method']} {req['path']}")
            continue
        args: dict = {k: int(v) if v.isdigit() else v for k, v in (groups or {}).items()}
        body = {}
        if req.get("body"):
            try:
                body = json.loads(req["body"])
            except json.JSONDecodeError:
                body = {}
        # Fields and headers the request did not send at all: their args hold the same placeholder as an
        # empty value (-1 / ""), so "absent" lets a trace spec tell "not sent" from "sent empty".
        absent: list[str] = []
        for field, typ in route.get("body", {}).items():
            if not isinstance(body, dict) or field not in body:
                absent.append(field)
            args[field] = convert(body.get(field) if isinstance(body, dict) else None, typ)
        headers = {k.lower(): v for k, v in req.get("headers", {}).items()}
        for name, typ in route.get("headers", {}).items():
            raw = headers.get(name)
            if name not in headers:
                absent.append(name.replace("-", "_"))
            if typ == "bearer":
                raw = raw[len("Bearer "):] if raw and raw.startswith("Bearer ") else NONE_TEXT
            args[name.replace("-", "_")] = convert(raw, "int" if typ == "int" else "text")
        seq = [ev(actor, f"{route['action']}_Begin", "request", req["t_start_ns"], req["t_start_ns"],
                  args=args or {"none": 0}, absent=sorted(absent))]

        for s in sorted(stmts_by_trace[tid], key=lambda s: s["t_send_ns"]):
            sql = COMMENT_RE.sub("", s["sql"]).strip()
            rule = mapping.statement(sql, route["action"])
            if rule is None:
                errors.append(f"{actor} ({route['action']}): unmapped SQL: {sql[:160]}")
                continue
            kind, table = rule["kind"], rule.get("table")
            rows = []
            check_rows = rule.get("rows", True)       # rows = false: the result lacks the key; check counts only
            if table and check_rows and s.get("columns") and s.get("rows"):
                rows = [mapping.project(table, dict(zip(s["columns"], r))) for r in s["rows"]]
                max_key = max([max_key] + [r["k"] for r in rows])
            tag = s.get("tag") or ""
            count = int(tag.split()[-1]) if tag and tag.split()[-1].isdigit() else len(rows)
            common = dict(rows=rows, has_rows=bool(s.get("columns")) and check_rows, count=count,
                          err=s.get("error_code") or "", limit1=bool(rule.get("limit1", False)))
            lo, hi = s["t_send_ns"], s["t_done_ns"]
            if kind == "commit":
                end = txn_ends.get((s["conn"], s["txn"]))
                writes = []
                if end and end["outcome"] == "COMMIT":
                    if end.get("xid") and end["xid"] in cdc_by_xid:
                        writes = committed_writes(cdc_by_xid[end["xid"]], mapping)
                    elif end.get("xid"):
                        errors.append(f"{actor}: COMMIT xid {end['xid']} has no logical-decoding record")
                for w in writes:
                    max_key = max(max_key, w["k"])
                seq.append(ev(actor, rule["action"], kind, lo, hi, writes=writes,
                              **{k: v for k, v in common.items() if k != "rows"}, rows=[]))
            elif kind in ("write", "insert"):
                if kind == "insert":
                    inserts[table] += 1
                seq.append(ev(actor, f"{rule['action']}_start", kind, lo, hi, phase="start"))
                seq.append(ev(actor, f"{rule['action']}_finish", kind, lo, hi, phase="finish", **common))
            else:
                seq.append(ev(actor, rule["action"], kind, lo, hi, **common))

        resp = responses.get(tid)
        if resp is None:
            errors.append(f"{actor}: request has no recorded response")
            continue
        fields = {}
        if route.get("response"):
            try:
                rbody = json.loads(resp.get("body") or "{}")
            except json.JSONDecodeError:
                rbody = {}
            if not isinstance(rbody, dict):
                rbody = {}
            for field, typ in route["response"].items():
                fields[field] = convert(rbody.get(field), typ)
        seq.append(ev(actor, f"{route['action']}_End", "response", resp["t_end_ns"], resp["t_end_ns"],
                      status=resp["status"], resp=fields or {"none": 0}))
        for i, e in enumerate(seq):
            e["_order"] = i
        events.extend(seq)

    if errors:
        raise BuildError("trace build failed:\n  " + "\n  ".join(errors))

    state = initial_state(cdc, mapping, since)
    seq_floor = {t: (max(rows) + 1 if rows else 1) for t, rows in state.items()}
    max_key = max([max_key] + [max(rows) for rows in state.values() if rows]
                  + [seq_floor[t] + inserts[t] for t in state])
    header = {
        "ev": "init",
        "tables": {t: sorted(rows.values(), key=lambda r: r["k"]) for t, rows in state.items()},
        "seq": seq_floor,
        "actors": sorted(set(actors.values()), key=lambda a: int(a[1:])),
        "maxkey": max_key,
    }
    events.sort(key=lambda e: (e["lo"], e["actor"], e["_order"]))
    for e in events:
        del e["_order"]
    return [header] + events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--since-ns", type=int, required=True)
    ap.add_argument("--until-ns", type=int)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    lines = build(args.run, Mapping(args.mapping), args.since_ns, args.until_ns)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    print(f"wrote {args.out}: {len(lines) - 1} events, {len(lines[0]['actors'])} actors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
