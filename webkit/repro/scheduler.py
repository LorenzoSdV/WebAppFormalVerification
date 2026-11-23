"""Deterministic reproduction of a counterexample's interleaving (generic; step 7, Level 1).

A schedule (written by webkit/timeline/timeline.py --schedule-out) lists the order in which
actors issue their SQL statements in a TLC counterexample. Replaying it needs no app changes:

  1. every actor's HTTP request gets its own traceparent, and the pgproxy gate holds every
     statement carrying one of those trace ids before it reaches PostgreSQL;
  2. the requests are sent concurrently; each one runs until its first statement is held;
  3. statements are released one at a time, in schedule order. After each release the
     controller waits until that statement has either completed or is confirmed (by
     pg_stat_activity) to be waiting on a row lock, i.e. the database has done what the
     counterexample says it does at that point;
  4. at the end the gate is cleared and every request finishes on its own.

It stops with a clear error when the real app diverges from the schedule: a scheduled
statement never arrives (the app took another path, or waits on something outside the
database such as a full connection pool), or a request finishes early.

A LockBlocked error is evidence, not a failed attempt: the counterexample needs an actor to
issue its next statement while PostgreSQL holds it on a row lock taken by another actor, so
the model lets something run that the database serializes (missing lock / FOR UPDATE in the
model: a citable SPEC_REPAIR in step 7).

Holding statements at the proxy is Specula's Level 1 (timing assistance): the app's code and
logic are unchanged, only the timing of its database calls is controlled.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


class ScheduleError(RuntimeError):
    pass


class LockBlocked(ScheduleError):
    """The schedule needs an actor to proceed while the database holds it on a lock."""

    def __init__(self, message: str, actor: str, blocked_by: list[str]) -> None:
        super().__init__(message)
        self.actor, self.blocked_by = actor, blocked_by


class Gate:
    """Client for the pgproxy control API (webkit/cmd/pgproxy/gate.go)."""

    def __init__(self, url: str = f"http://{os.environ.get('WK_HOST', 'localhost')}:6433") -> None:
        self.url = url.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")

    def gate(self, trace_ids: list[str]) -> None:
        self._call("POST", "/gate", {"trace_ids": trace_ids})

    def clear(self) -> None:
        self._call("DELETE", "/gate")

    def held(self) -> list[dict]:
        return self._call("GET", "/held") or []

    def release(self, hold_id: int) -> bool:
        return bool(self._call("POST", "/release", {"id": hold_id})["released"])

    def conns(self) -> list[dict]:
        return self._call("GET", "/conns") or []


@dataclass
class Step:
    n: int
    actor: str
    expected: str
    sql: str = ""
    outcome: str = ""      # "completed" | "lock-wait"


@dataclass
class Result:
    steps: list[Step] = field(default_factory=list)
    responses: dict[str, object] = field(default_factory=dict)
    trace_ids: dict[str, str] = field(default_factory=dict)
    t_start_ns: int = 0
    t_end_ns: int = 0


def new_traceparent() -> tuple[str, str]:
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


def replay(schedule: dict, send: Callable[[str, str], object], gate: Gate | None = None,
           step_timeout: float = 10.0, poll: float = 0.005) -> Result:
    """Replay schedule["order"]. send(actor, traceparent) performs that actor's HTTP request
    (blocking) and returns its response; it runs on its own thread."""
    gate = gate or Gate()
    actors = list(schedule["actors"])
    result = Result()
    parents = {}
    for a in actors:
        result.trace_ids[a], parents[a] = new_traceparent()
    done: dict[str, bool] = {a: False for a in actors}
    conn_actor: dict[int, str] = {}       # connection -> actor whose statement it last ran

    def run(actor: str) -> None:
        try:
            result.responses[actor] = send(actor, parents[actor])
        except Exception as e:  # noqa: BLE001 - recorded as the actor's outcome
            result.responses[actor] = e
        done[actor] = True

    gate.clear()
    gate.gate(list(result.trace_ids.values()))
    result.t_start_ns = time.time_ns()
    threads = [threading.Thread(target=run, args=(a,), daemon=True) for a in actors]
    try:
        for t in threads:
            t.start()
        for i, entry in enumerate(schedule["order"], 1):
            actor = entry["actor"]
            step = Step(n=i, actor=actor, expected=entry.get("action", ""))
            hold = _wait_for_hold(gate, result.trace_ids[actor], actor, done, step, step_timeout, poll,
                                  conn_actor)
            conn_actor[hold["conn"]] = actor
            step.sql = hold["sql"].strip()
            before = _conn(gate, hold["conn"])
            gate.release(hold["id"])
            step.outcome = _wait_settled(gate, hold, before["sent"] if before else 0, step_timeout, poll)
            result.steps.append(step)
            if step.outcome == "lock-wait" and not entry.get("may_wait", True):
                blockers = _blockers(gate, hold["conn"], conn_actor)
                raise LockBlocked(
                    f"step {i}: {actor}'s statement ({step.expected}: {step.sql[:80]}) waits on a row lock held "
                    f"by {', '.join(blockers)}, but the model treats it as a statement that never waits. The code "
                    f"takes a lock the model misses, e.g. SELECT ... FOR UPDATE modelled as a plain SELECT "
                    f"(SPEC_REPAIR evidence).", actor, blockers)
    finally:
        gate.clear()
        for t in threads:
            t.join(timeout=30)
        result.t_end_ns = time.time_ns()
    return result


def _wait_for_hold(gate: Gate, trace_id: str, actor: str, done: dict, step: Step,
                   timeout: float, poll: float, conn_actor: dict[int, str]) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for h in gate.held():
            if h["trace_id"] == trace_id:
                return h
        mine = [c for c in gate.conns() if conn_actor.get(c["conn"]) == actor and c["lock_wait"]]
        if mine:
            blockers = _blockers(gate, mine[0]["conn"], conn_actor)
            raise LockBlocked(
                f"step {step.n}: the schedule needs {actor} to issue its next statement ({step.expected}), "
                f"but its previous statement is waiting on a row lock held by {', '.join(blockers)}. "
                f"The model lets {actor} proceed where PostgreSQL serializes it: missing lock in the model "
                f"(SPEC_REPAIR evidence).", actor, blockers)
        if done[actor]:
            raise ScheduleError(f"step {step.n}: {actor} finished its request before issuing "
                                f"the scheduled statement ({step.expected}); the app took a different path")
        time.sleep(poll)
    held = ", ".join(f"{h['trace_id'][:8]}: {h['sql'][:60]}" for h in gate.held()) or "nothing"
    raise ScheduleError(f"step {step.n}: {actor} never issued the scheduled statement ({step.expected}) "
                        f"within {timeout}s (held: {held}). Is it waiting outside the database, e.g. on "
                        f"the connection pool?")


def _blockers(gate: Gate, conn_id: int, conn_actor: dict[int, str]) -> list[str]:
    conns = gate.conns()
    pid_actor = {c["pid"]: conn_actor.get(c["conn"], f"conn {c['conn']}") for c in conns}
    me = next((c for c in conns if c["conn"] == conn_id), None)
    return sorted({pid_actor.get(p, f"pid {p}") for p in (me or {}).get("blocked_by") or []})


def _conn(gate: Gate, conn_id: int) -> dict | None:
    return next((c for c in gate.conns() if c["conn"] == conn_id), None)


def _wait_settled(gate: Gate, hold: dict, sent_before: int, timeout: float, poll: float) -> str:
    """Wait until the released statement was sent and then completed or is waiting on a lock."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = _conn(gate, hold["conn"])
        if c is None:
            return "completed"                     # the connection closed
        if c["sent"] > sent_before:
            if c["lock_wait"]:
                return "lock-wait"
            if c["finished"] >= c["sent"]:
                return "completed"
        time.sleep(poll)
    raise ScheduleError(f"statement on conn {hold['conn']} neither completed nor waited on a lock "
                        f"within {timeout}s: {hold['sql'][:80]}")
