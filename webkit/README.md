# webkit

Generic tooling for verifying web apps with the Specula workflow. Nothing here is specific to one app:
each app supplies a small stack config, and its own code and compose file stay untouched.

## Tracing stack (M1)

`stack/wkstack.py` wraps an app's Docker Compose stack with recorders at the app's boundaries:

| Service | What it records | Output |
|---|---|---|
| `wk-inbound` | Every HTTP request and response; assigns each request a W3C `traceparent` (its actor id) | `inbound.ndjson` |
| `wk-pgproxy` | Every SQL statement: text, parameters, result rows (capped), tag, SQLSTATE, timing, connection, transaction, and the request's trace id from the sqlcommenter comment. Captures each committing transaction's xid | `pgproxy.ndjson` |
| `wk-cdc` | Every committed transaction from logical decoding (pgoutput): xid, commit LSN, row changes | `cdc.ndjson` |
| `wk-otelcol` | OpenTelemetry spans from the app | `otel-spans.ndjson` |
| `otel/node/register.mjs` | Loaded into Node apps with `NODE_OPTIONS` (no code change). Continues the request's trace and writes its `traceparent` into every SQL statement as a comment | — |

The generated override also:
- turns on `wal_level=logical`;
- points the app's database URL at the proxy;
- removes the app's host port, so every request goes through `wk-inbound` and gets recorded.

```bash
python3 webkit/stack/wkstack.py up       --config <app>/stack.toml   # new run in runs/<id>/
python3 webkit/stack/wkstack.py selftest --config <app>/stack.toml   # checks every signal
python3 webkit/stack/wkstack.py down     --config <app>/stack.toml
```

See the docstring of `stack/wkstack.py` for the stack config format.

### How events are linked

- **Request → SQL.** The trace id in `inbound` equals `trace_id` on each `pgproxy` `stmt`, carried by the sqlcommenter comment.
- **Proxy transaction → committed rows.** The `xid` on the `pgproxy` `txn_end` equals the `xid` on the `cdc` `txn`. This works for explicit transactions; autocommit writes have no captured xid yet (see Limits).
- **Order:**
  - within one connection, statements are ordered by `seq`;
  - across requests, by the `[t_send_ns, t_done_ns]` intervals;
  - committed writes are totally ordered by `commit_lsn_int`.

### Event schemas

Every line has `src`, `ev` and `ts_ns` (wall-clock nanoseconds when written).

| `src` | `ev` | Fields |
|---|---|---|
| inbound | `request` | `trace_id, method, path, headers, body, remote, t_start_ns` |
| inbound | `response` | `trace_id, method, path, status, headers, body, body_bytes, t_start_ns, t_end_ns, client_aborted` |
| pgproxy | `conn_open` / `conn_close` | `conn, client, user, database, application_name` / `conn, statements` |
| pgproxy | `stmt` | `conn, seq, protocol (simple\|extended), sql, sql_norm, params, trace_id, span_id, txn, t_send_ns, t_done_ns, columns, rows, rows_seen, rows_truncated, tag, error_code, error_message` |
| pgproxy | `txn_end` | `conn, txn, trace_id, outcome (COMMIT\|ROLLBACK), xid` |
| cdc | `txn` | `xid, commit_lsn, commit_lsn_int, commit_time_ns, changes: [{op, table, new, old}]` |
| all | `ready` | Recorder started |

## Trace validation (M3)

| Piece | What it does |
|---|---|
| `tracebuilder/build.py` | Turns a recording into a model-level trace, driven by the app's **mapping file** (format: the harness-generation skill): routes and SQL patterns to action names, modelled tables and columns. It emits one event per action, each with its time interval, results, errors, the rows each COMMIT committed (from CDC), and the initial state (replayed from CDC) |
| `tla/WebTrace.tla` | Generic ordering. A request's own events stay in program order. Different requests' events are ordered only when their intervals don't overlap, so TLC searches the interleavings the recording leaves open (as in Specula's Category B traces) |
| `tracecheck/check.py` | Runs TLC on traces for any app trace spec. For a rejected trace it reports the most events the model can match and the recorded events it could not match next, which is where the model and the app diverge |

The app-specific parts are three files. The model (`<App>.tla`, built on `WebDB`) and its trace spec
(`Trace<App>.tla`) are what the agent writes in a pipeline run. The mapping file is configuration.

## Model checking and reproduction (M4, M5)

| Piece | What it does |
|---|---|
| `timeline/timeline.py` | Turns TLC's JSON counterexample into request lanes in SQL terms: reads, lock waits, commits, HTTP status. With `--schedule-out` it writes the order of statement starts (from WebDB's `sq` counter) plus each actor's inputs |
| `repro/schedule_from_tlc.py` | Runs a hunt config, then the timeline, to produce a schedule |
| `cmd/pgproxy` gate (`gate.go`) | Holds every statement of the gated requests before it reaches PostgreSQL, until released. Control API on port 6433: `/gate`, `/held`, `/release`, `/conns` (sent/finished counts, lock waits, blockers) |
| `repro/scheduler.py` | Replays a schedule: concurrent requests, statements released one at a time in counterexample order, waiting after each until it completes or is waiting on a lock (Level 1 reproduction: no app changes). `LockBlocked` means the schedule needs a request to proceed while the database holds it on a lock, which is evidence of a missing lock in the model |
| `tracecheck/check.py --violates INV` | Same-bug check: the reproduction's own trace must be matched by the model **and** end violating `INV` |

### Limits (known, to address in later milestones)

- **Autocommit writes have no xid.** The xid is captured only before an explicit `COMMIT`. Such writes are matched to CDC by table and time interval in the trace builder (M3).
- **Request attribution needs sqlcommenter.** That's available for Node (via `otel/node`). Other runtimes need their equivalent, or a fallback that matches OTel DB spans by time. A Python bootstrap comes in M8.
- **No egress proxy yet.** It's added with the payment-provider module in M7.
- **`COPY` and multi-statement simple queries** are forwarded correctly but recorded only coarsely.

## M6: pipeline integration (stack lifecycle, workloads, coverage)

Specula's orchestrator now owns the stack (see `Specula/src/specula/stacklib.py`):

- `stack/wkstack.py ensure|status|reset --config stack.toml --runs-dir DIR`: reuse the running
  stack if it runs this exact config and passes the self-test, otherwise (re)start it from a clean
  project; `status` prints JSON (URLs, recordings dir, last self-test); `reset` reseeds the database
  with the app's own command (`[app].reset`). The self-test also runs `reset` and requires its
  commit in logical decoding, and writes `<run>/selftest.json`.
- `workload/runner.py run|rebuild`: run an app's `SCENARIOS` (a `workloads.py`) through the stack,
  reset the database before each run, record windows in `traces/windows.json`, build one trace per
  run; `rebuild` rebuilds every trace from the recordings after a mapping change.
- `tracecheck/coverage.py --mapping mapping.toml traces/`: every mapped action in some trace, and
  overlapping actors in every concurrent scenario.
- `tracecheck/check.py --explain`: for a rejected trace, the stuck events' recorded fields and the
  model state after the matched prefix.

```bash
python3 webkit/stack/wkstack.py ensure --config <app>/stack.toml
python3 webkit/workload/runner.py run --stack <app>/stack.toml \
    --mapping <app>/mapping.toml --workloads <app>/workloads.py --traces /tmp/traces
python3 webkit/tracecheck/coverage.py --mapping <app>/mapping.toml /tmp/traces
```
