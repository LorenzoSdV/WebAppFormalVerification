#!/usr/bin/env python3
"""Wrap any app's Docker Compose stack with the webkit tracing recorders.

The app's own compose file is never edited. This tool generates a compose
override from a small per-app stack config (TOML) and runs both together:

    wkstack.py up       --config stack.toml   start app + recorders, new run dir
    wkstack.py selftest --config stack.toml   check every signal source (result: <run>/selftest.json)
    wkstack.py ensure   --config stack.toml   reuse the running, self-tested stack or (re)start it
    wkstack.py status   --config stack.toml   JSON: running?, run id, URLs, last self-test
    wkstack.py reset    --config stack.toml   reseed the database with the app's own command
    wkstack.py down     --config stack.toml   stop and remove containers (recordings are kept)
    wkstack.py overlay  --config stack.toml   print the generated override

All commands take --runs-dir DIR (env WK_RUNS_DIR; default <repo>/runs): where run directories
and the current-stack state file live. WK_HOST (default localhost) is the host name of the
published ports, e.g. host.docker.internal when this runs inside a container.

Recorders added around the app (all output goes to <runs-dir>/<run-id>/):
    wk-inbound  reverse proxy in front of the app      -> inbound.ndjson
    wk-pgproxy  PostgreSQL wire proxy (+ hold/release control API on host port 6433) -> pgproxy.ndjson
    wk-cdc      logical decoding of committed changes  -> cdc.ndjson
    wk-otelcol  OpenTelemetry collector                -> otel-spans.ndjson

Stack config (TOML):

    [app]
    compose = "path/to/compose.yaml"   # relative to the config file
    project = "booking"                # compose project name
    service = "app"                    # app service name
    port = 3000                        # port the app listens on inside its container
    runtime = "node"                   # "node" loads the OTel bootstrap; "none" skips it
    probe_path = "/seats/1"            # GET path that touches the DB, for the self-test
    health_path = "/health"
    reset = ["npm", "run", "-s", "db:seed"]  # app's own seed/reset command, run in the app container

    [app.env]                          # extra env for the app (optional)
    WEB_CONCURRENCY = "1"

    [db]
    service = "db"
    kind = "postgres"
    port = 5432
    user = "app"
    password = "app"
    database = "app"
    url_env = "DATABASE_URL"           # env var the app reads its DB URL from

    [inbound]
    host_port = 8080
    control_port = 6433                # pgproxy hold/release API (reproduction)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

WEBKIT = Path(__file__).resolve().parent.parent
REPO = WEBKIT.parent
RUNS = REPO / "runs"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.165.0"
RECORDER_IMAGE = "webkit-recorders:local"
# Host name under which the stack's published ports are reachable from where this tool runs:
# "localhost" on the host itself, "host.docker.internal" from inside a container (env WK_HOST).
HOST = os.environ.get("WK_HOST", "localhost")


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_dir"] = path.resolve().parent
    cfg["_path"] = path.resolve()
    cfg["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return cfg


def q(s: str) -> str:
    """Quote a string for YAML (JSON strings are valid YAML scalars)."""
    return json.dumps(s)


def overlay_yaml(cfg: dict, out_dir: Path) -> str:
    app, db = cfg["app"], cfg["db"]
    if db.get("kind", "postgres") != "postgres":
        sys.exit(f"unsupported db kind {db.get('kind')!r}: only postgres is implemented")
    inbound_port = cfg.get("inbound", {}).get("host_port", 8080)
    creds = f"{db['user']}:{db['password']}"
    proxied_url = f"postgresql://{creds}@wk-pgproxy:6432/{db['database']}"
    direct_url = f"postgresql://{creds}@{db['service']}:{db['port']}/{db['database']}"

    app_env = {db["url_env"]: proxied_url, **cfg["app"].get("env", {})}
    app_volumes = []
    if app.get("runtime", "none") == "node":
        app_env.update({
            "NODE_OPTIONS": "--import /opt/webkit/otel/register.mjs",
            "OTEL_SERVICE_NAME": app["service"],
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://wk-otelcol:4318",
            "OTEL_BSP_SCHEDULE_DELAY": "200",
        })
        app_volumes.append(f"{WEBKIT / 'otel' / 'node'}:/opt/webkit/otel:ro")

    out = str(out_dir)
    lines = ["services:"]
    lines += [
        f"  {db['service']}:",
        "    command: [\"postgres\", \"-c\", \"wal_level=logical\", \"-c\", \"max_replication_slots=10\", "
        "\"-c\", \"max_wal_senders=10\"]",
        f"  {app['service']}:",
        "    ports: !reset []",
        "    environment:",
        *[f"      {k}: {q(str(v))}" for k, v in app_env.items()],
    ]
    if app_volumes:
        lines += ["    volumes:", *[f"      - {q(v)}" for v in app_volumes]]
    lines += [
        "    depends_on:",
        "      wk-pgproxy: {condition: service_started}",
        "      wk-cdc: {condition: service_healthy}",
        "      wk-otelcol: {condition: service_started}",
    ]

    def recorder(name: str, binary: str, env: dict, extra: list[str] | None = None) -> list[str]:
        block = [
            f"  {name}:",
            f"    image: {RECORDER_IMAGE}",
            f"    build: {{context: {q(str(WEBKIT))}}}",
            f"    command: [\"{binary}\"]",
            "    environment:",
            *[f"      {k}: {q(str(v))}" for k, v in env.items()],
            "    volumes:",
            f"      - {q(out + ':/wk/out')}",
        ]
        return block + (extra or [])

    control_port = cfg.get("inbound", {}).get("control_port", 6433)
    lines += recorder("wk-pgproxy", "pgproxy", {"WK_UPSTREAM": f"{db['service']}:{db['port']}", "WK_DB_URL": direct_url},
                      ["    ports:", f"      - \"{control_port}:6433\"",
                       "    depends_on:", f"      {db['service']}: {{condition: service_healthy}}"])
    lines += recorder("wk-cdc", "cdc", {"WK_DB_URL": direct_url}, [
        "    depends_on:", f"      {db['service']}: {{condition: service_healthy}}",
        "    healthcheck:",
        "      test: [\"CMD\", \"grep\", \"-q\", \"\\\"ev\\\":\\\"ready\\\"\", \"/wk/out/cdc.ndjson\"]",
        "      interval: 1s",
        "      retries: 60",
    ])
    lines += recorder("wk-inbound", "inbound", {"WK_UPSTREAM": f"http://{app['service']}:{app['port']}"}, [
        "    ports:", f"      - \"{inbound_port}:8080\"",
    ])
    lines += [
        "  wk-otelcol:",
        f"    image: {COLLECTOR_IMAGE}",
        "    command: [\"--config=/etc/wk/otelcol.yaml\"]",
        "    user: \"0\"",
        "    volumes:",
        f"      - {q(str(WEBKIT / 'stack' / 'otelcol.yaml') + ':/etc/wk/otelcol.yaml:ro')}",
        f"      - {q(out + ':/wk/out')}",
    ]
    return "\n".join(lines) + "\n"


def compose_cmd(cfg: dict, overlay: Path) -> list[str]:
    app_compose = (cfg["_dir"] / cfg["app"]["compose"]).resolve()
    return ["docker", "compose", "-p", cfg["app"]["project"], "-f", str(app_compose), "-f", str(overlay)]


def state_file(cfg: dict) -> Path:
    return RUNS / f".current-{cfg['app']['project']}.json"


def base_url(cfg: dict) -> str:
    return f"http://{HOST}:{cfg.get('inbound', {}).get('host_port', 8080)}"


def cmd_up(cfg: dict, args: argparse.Namespace) -> None:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    if cfg["app"].get("runtime") == "node" and not (WEBKIT / "otel" / "node" / "node_modules").exists():
        print("+ npm ci (webkit/otel/node)", flush=True)
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=WEBKIT / "otel" / "node", check=True)
    out_dir = RUNS / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    overlay = out_dir / "compose.webkit.yaml"
    overlay.write_text(overlay_yaml(cfg, out_dir))
    # Start from a clean project: containers or volumes left by another stack instance of the same
    # compose project (e.g. a different runs dir) would otherwise skip migrations and seeding.
    subprocess.run(compose_cmd(cfg, overlay) + ["down", "-v", "--remove-orphans"], capture_output=True)
    cmd = compose_cmd(cfg, overlay) + ["up", "-d", "--build", "--wait"]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    state = {"run_id": run_id, "out_dir": str(out_dir), "overlay": str(overlay),
             "config": str(cfg["_path"]), "config_sha256": cfg["_sha256"], "base_url": base_url(cfg),
             "control_url": f"http://{HOST}:{cfg.get('inbound', {}).get('control_port', 6433)}",
             "started_at": datetime.now(timezone.utc).isoformat()}
    state_file(cfg).write_text(json.dumps(state, indent=2))
    wait_http(base_url(cfg) + cfg["app"].get("health_path", "/"), timeout=120)
    print(f"up: run {run_id}; app via {base_url(cfg)}; recordings in {out_dir}")


def current(cfg: dict) -> dict:
    p = state_file(cfg)
    if not p.exists():
        sys.exit("no running stack for this config; run `up` first")
    return json.loads(p.read_text())


def cmd_down(cfg: dict, _args: argparse.Namespace) -> None:
    if not state_file(cfg).exists():
        print(f"down: no running stack for project {cfg['app']['project']!r} (already stopped)")
        return
    st = current(cfg)
    subprocess.run(compose_cmd(cfg, Path(st["overlay"])) + ["down", "-v", "--remove-orphans"], check=True)
    state_file(cfg).unlink()
    print(f"down: recordings kept in {st['out_dir']}")


def wait_http(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    sys.exit(f"timed out waiting for {url}")


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def span_trace_ids(path: Path) -> set[str]:
    """Trace ids in the collector's file exporter output (OTLP JSON, one batch per line)."""
    ids: set[str] = set()
    for batch in read_events(path):
        for rs in batch.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    ids.add(span.get("traceId", ""))
    return ids


def selftest(cfg: dict) -> bool:
    """Probe the app once and check every signal source; record the result in the run dir."""
    st = current(cfg)
    out = Path(st["out_dir"])
    started_ns = time.time_ns()
    reset_error = ""
    if cfg["app"].get("reset"):
        try:
            run_reset(cfg)
        except RuntimeError as e:
            reset_error = str(e)
    trace_id = uuid.uuid4().hex
    req = urllib.request.Request(base_url(cfg) + cfg["app"]["probe_path"],
                                 headers={"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            probe_status = r.status
    except urllib.error.HTTPError as e:
        probe_status = e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        probe_status = f"unreachable ({e})"
    time.sleep(1.5)  # let the span exporter and file writers flush

    inbound = read_events(out / "inbound.ndjson")
    pgproxy = read_events(out / "pgproxy.ndjson")
    cdc = read_events(out / "cdc.ndjson")
    spans = span_trace_ids(out / "otel-spans.ndjson")

    checks = [
        ("inbound: probe request recorded",
         any(e.get("ev") == "response" and e.get("trace_id") == trace_id for e in inbound)),
        ("pgproxy: ready and app connected",
         any(e.get("ev") == "ready" for e in pgproxy) and any(e.get("ev") == "conn_open" for e in pgproxy)),
        ("pgproxy: probe SQL attributed to the probe request (sqlcommenter)",
         any(e.get("ev") == "stmt" and e.get("trace_id") == trace_id for e in pgproxy)),
        ("cdc: replication stream ready", any(e.get("ev") == "ready" for e in cdc)),
        *([("app: reset command ([app].reset) succeeds", not reset_error),
           ("cdc: the reset's committed transactions captured",
            any(e.get("ev") == "txn" and int(e.get("ts_ns", 0)) >= started_ns
                for e in cdc))]
          if cfg["app"].get("reset") else
          [("cdc: committed transactions captured (migrations/seed)", any(e.get("ev") == "txn" for e in cdc))]),
        ("otel: spans for the probe request exported", trace_id in spans),
    ]
    ok = all(passed for _, passed in checks)
    width = max(len(name) for name, _ in checks)
    print(f"selftest: run {st['run_id']}, probe GET {cfg['app']['probe_path']} -> {probe_status}")
    if reset_error:
        print(f"  reset failed: {reset_error}")
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name.ljust(width)}")
    (out / "selftest.json").write_text(json.dumps({
        "ok": ok, "at": datetime.now(timezone.utc).isoformat(), "at_ns": time.time_ns(),
        "run_id": st["run_id"], "config_sha256": cfg["_sha256"], "probe_status": probe_status,
        "checks": [{"name": n, "ok": p} for n, p in checks]}, indent=2))
    return ok


def cmd_selftest(cfg: dict, _args: argparse.Namespace) -> None:
    if not selftest(cfg):
        sys.exit(1)


def running(cfg: dict) -> bool:
    """The recorded stack's app and recorders are up and the app answers through the inbound proxy."""
    if not state_file(cfg).exists():
        return False
    st = current(cfg)
    if st.get("config_sha256") != cfg["_sha256"] or not Path(st["overlay"]).exists():
        return False
    ps = subprocess.run(compose_cmd(cfg, Path(st["overlay"])) + ["ps", "--status", "running", "--services"],
                        capture_output=True, text=True)
    services = set(ps.stdout.split())
    if ps.returncode != 0 or not {cfg["app"]["service"], "wk-pgproxy", "wk-cdc", "wk-inbound"} <= services:
        return False
    try:
        with urllib.request.urlopen(base_url(cfg) + cfg["app"].get("health_path", "/"), timeout=3) as r:
            return r.status < 500
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def cmd_status(cfg: dict, _args: argparse.Namespace) -> None:
    st = json.loads(state_file(cfg).read_text()) if state_file(cfg).exists() else {}
    result = Path(st["out_dir"]) / "selftest.json" if st else None
    print(json.dumps({"running": running(cfg), "config_sha256": cfg["_sha256"], **st,
                      "selftest": json.loads(result.read_text()) if result and result.exists() else None},
                     indent=2))


def cmd_ensure(cfg: dict, args: argparse.Namespace) -> None:
    """Reuse the recorded stack if it is running this exact config and passes the self-test;
    otherwise (re)start it. Exits non-zero unless a self-tested stack is running afterwards."""
    if running(cfg):
        print(f"ensure: reusing run {current(cfg)['run_id']}")
        if selftest(cfg):
            return
        print("ensure: self-test failed on the running stack; restarting it")
    if state_file(cfg).exists():
        cmd_down(cfg, args)
    cmd_up(cfg, args)
    if not selftest(cfg):
        sys.exit(1)


def run_reset(cfg: dict) -> None:
    """Return the database to the app's seeded state with the app's own command ([app].reset)."""
    reset = cfg["app"].get("reset")
    if not reset:
        raise RuntimeError("stack config has no [app].reset command (the app's own seed/reset command)")
    st = current(cfg)
    argv = reset if isinstance(reset, list) else ["sh", "-c", reset]
    p = subprocess.run(compose_cmd(cfg, Path(st["overlay"])) + ["exec", "-T", cfg["app"]["service"], *argv],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"exit {p.returncode}:\n{p.stdout}{p.stderr}")


def cmd_reset(cfg: dict, _args: argparse.Namespace) -> None:
    try:
        run_reset(cfg)
    except RuntimeError as e:
        sys.exit(f"reset failed: {e}")


def cmd_overlay(cfg: dict, _args: argparse.Namespace) -> None:
    print(overlay_yaml(cfg, RUNS / "<run-id>"), end="")


COMMANDS = {"up": cmd_up, "down": cmd_down, "selftest": cmd_selftest, "status": cmd_status,
            "ensure": cmd_ensure, "reset": cmd_reset, "overlay": cmd_overlay}


def main() -> None:
    global RUNS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--runs-dir", type=Path, default=Path(os.environ["WK_RUNS_DIR"]) if os.environ.get("WK_RUNS_DIR") else None,
                   help="where run directories and the current-stack state live (default: <repo>/runs; env WK_RUNS_DIR)")
    p.add_argument("--run-id")
    args = p.parse_args()
    if args.runs_dir:
        RUNS = args.runs_dir.resolve()
    cfg = load_config(args.config)
    RUNS.mkdir(parents=True, exist_ok=True)
    COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    os.environ.setdefault("COMPOSE_MENU", "false")
    main()
