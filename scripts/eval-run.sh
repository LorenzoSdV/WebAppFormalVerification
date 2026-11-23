#!/usr/bin/env bash
# Run the full pipeline from scratch on an evaluation app, without letting the agents see anything
# that reveals the answers.
#
#   scripts/eval-run.sh --app apps/seeded-shop --name seeded-shop --guidance eval/guidance/seeded-shop.md \
#       --agent-config config/agents/nemotron.json [--hide PATH ...] [--dry-run] [-- extra specula args]
#
#   Container mode (agents see only what is mounted; required for paid runs):
#   scripts/eval-run.sh --container ... [--cap-usd 70 --prices 2,0.10,10] [--openai-key-file FILE]
#
#   Resume an earlier container run (e.g. re-confirm on another agent); the app copy and settings come
#   from the run's run.json, and --agent-config may switch the agent:
#   scripts/eval-run.sh --container --resume RUN_ID [--agent-config FILE] [--cap-usd N --prices ...] [-- args]
#
#   scripts/eval-run.sh --restore        # put hidden paths back after a crash
#
# 1. Copies the app to $EVAL_ROOT/<name>-<stamp>/<name> (default EVAL_ROOT: ~/specula-eval) as a fresh git
#    repository with one neutral commit: the agents get the code, not our history.
# 2. Moves every --hide path out of reach ($PRIVATE_ROOT, default ~/.specula-private/<stamp>) for the
#    duration of the run, and restores it on exit (also after Ctrl-C). A manifest allows --restore.
#    In container mode, earlier runs under Specula/runs/ are hidden too (they hold earlier models).
# 3. Runs `specula run` on the copy with the web toolkit from this repo: on this machine, or (with
#    --container) in the specula-agent image, which sees only Specula, webkit, the app copy, the agent
#    config and guidance, and drives the app's stack through the host's Docker daemon.
#    --cap-usd stops the run when the Codex sessions' cost (priced with --prices, USD per million
#    input,cached-input,output tokens) reaches the cap.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
eval_root="${EVAL_ROOT:-$HOME/specula-eval}"
private_root="${PRIVATE_ROOT:-$HOME/.specula-private}"
manifest="$private_root/current.manifest"
image="${AGENT_IMAGE:-specula-agent:latest}"

restore() {
    [ -f "$manifest" ] || return 0
    while IFS=$'\t' read -r original hidden; do
        # -L too: a symlink (e.g. runs/latest) dangles while its target is hidden.
        if { [ -e "$hidden" ] || [ -L "$hidden" ]; } && [ ! -e "$original" ] && [ ! -L "$original" ]; then
            mkdir -p "$(dirname "$original")"
            mv "$hidden" "$original"
            echo "restored $original"
        elif [ -L "$hidden" ]; then
            rm -f "$hidden"   # a pointer the run recreated (e.g. runs/latest); nothing to keep
        elif [ -e "$hidden" ]; then
            echo "WARNING: both $original and $hidden exist; left $hidden in place" >&2
        fi
    done < "$manifest"
    rm -f "$manifest"
}

if [ "${1:-}" = "--restore" ]; then
    restore
    exit 0
fi

app= name= guidance= agent_config= dry_run= container= cap_usd= prices= github= resume=
key_file="$HOME/.config/specula/openai_key"
hide=()
while [ $# -gt 0 ]; do
    case "$1" in
        --app) app="$2"; shift 2 ;;
        --name) name="$2"; shift 2 ;;
        --guidance) guidance="$2"; shift 2 ;;
        --agent-config) agent_config="$2"; shift 2 ;;
        --hide) hide+=("$2"); shift 2 ;;
        --dry-run) dry_run=--dry-run; shift ;;
        --container) container=1; shift ;;
        --cap-usd) cap_usd="$2"; shift 2 ;;
        --prices) prices="$2"; shift 2 ;;
        --openai-key-file) key_file="$2"; shift 2 ;;
        --github) github="$2"; shift 2 ;;
        --resume) resume="$2"; shift 2 ;;
        --) shift; break ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
if [ -n "$resume" ]; then
    [ -n "$container" ] || { echo "--resume needs --container" >&2; exit 2; }
    run_json="$repo/Specula/runs/$resume/run.json"
    [ -f "$run_json" ] || { echo "no run $resume ($run_json)" >&2; exit 2; }
    read_run() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or '')" "$run_json" "$1"; }
    artifact="$(read_run artifact)"
    [ -n "$agent_config" ] || agent_config="$(read_run agent_config)"
    [ -n "$guidance" ] || guidance="$(read_run guidance)"
    name="$(basename "$artifact")"
    app="$artifact"
fi
[ -n "$app" ] && [ -n "$name" ] && [ -n "$agent_config" ] || { echo "need --app, --name, --agent-config" >&2; exit 2; }
if [ -n "$cap_usd" ] && { [ -z "$container" ] || [ -z "$prices" ]; }; then
    echo "--cap-usd needs --container and --prices" >&2; exit 2
fi
if [ -f "$manifest" ]; then
    echo "a previous run left hidden paths ($manifest); run: $0 --restore" >&2
    exit 1
fi

app="$(cd "$app" && pwd)"
agent_config="$(cd "$(dirname "$agent_config")" && pwd)/$(basename "$agent_config")"
[ -z "$guidance" ] || guidance="$(cd "$(dirname "$guidance")" && pwd)/$(basename "$guidance")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
uses_codex=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(int(any(p.get('agent')=='codex' for p in d['profiles'].values())))" "$agent_config")
if [ -n "$container" ] && [ "$uses_codex" = 1 ] && [ ! -s "$key_file" ]; then
    echo "OpenAI key file missing or empty: $key_file" >&2; exit 1
fi

# 1. Clean copy of the app, no history (a resumed run reuses its own copy).
if [ -n "$resume" ]; then
    copy="$app"
    run_root="$(dirname "$copy")"
    echo "resuming run $resume; app copy: $copy"
else
    run_root="$eval_root/$name-$stamp"
    copy="$run_root/$name"
    mkdir -p "$copy"
    rsync -a --exclude .git --exclude node_modules --exclude .specula-output "$app/" "$copy/"
    git -C "$copy" init -q
    git -C "$copy" add -A
    git -C "$copy" -c user.name=eval -c user.email=eval@localhost commit -qm "Initial import"
    echo "app copy: $copy"
fi

# 2. Hide private paths (restored on exit).
if [ -n "$container" ] && [ -d "$repo/Specula/runs" ]; then
    for old in "$repo/Specula/runs"/*; do
        { [ -e "$old" ] || [ -L "$old" ]; } && [ "$(basename "$old")" != "$resume" ] && hide+=("$old")
    done
fi
mkdir -p "$private_root/$stamp"
: > "$manifest"
cleanup() {
    restore
    rmdir "$private_root/$stamp" 2>/dev/null || true
    [ -z "${container_name:-}" ] || docker rm -f "$container_name" >/dev/null 2>&1 || true
    [ -z "${codex_home:-}" ] || rm -f "$codex_home/auth.json"
}
trap cleanup EXIT
for path in "${hide[@]}"; do
    original="$(cd "$(dirname "$path")" && pwd)/$(basename "$path")"
    { [ -e "$original" ] || [ -L "$original" ]; } || { echo "skip hide (missing): $original"; continue; }
    hidden="$private_root/$stamp/$(printf '%s' "$original" | tr '/' '_')"
    mv "$original" "$hidden"
    printf '%s\t%s\n' "$original" "$hidden" >> "$manifest"
    echo "hidden: $original"
done

# 3. Run the pipeline on the copy.
if [ -n "$resume" ]; then
    args=(run --run-id="$resume" --agent-config="$agent_config" --webkit="$repo/webkit")
    # Specula requires fresh agent conversations when the agent configuration changes.
    [ "$agent_config" = "$(read_run agent_config)" ] || args+=(--fresh-context)
    target=()
else
    args=(run --agent-config="$agent_config" --artifact="$copy" --webkit="$repo/webkit")
    [ -z "$guidance" ] || args+=(--guidance="$guidance")
    target=("$name|$github||")
fi
[ -z "$dry_run" ] || args+=("$dry_run")

if [ -z "$container" ]; then
    cd "$repo/Specula"
    set +e
    specula "${args[@]}" "$@" ${target[@]+"${target[@]}"}
    status=$?
    set -e
    echo "specula exited $status; app copy: $copy"
    exit "$status"
fi

codex_home="$run_root/codex-home"
mkdir -p "$codex_home" && chmod 700 "$codex_home"
container_name="specula-agent-$stamp"
mounts=(
    -v /var/run/docker.sock:/var/run/docker.sock
    -v "$repo/Specula:$repo/Specula"
    -v "$repo/webkit:$repo/webkit"
    -v "$run_root:$run_root"
    -v "$agent_config:$agent_config:ro"
    -v "$codex_home:/home/specula/.codex"
)
[ -z "$guidance" ] || mounts+=(-v "$guidance:$guidance:ro")
envs=(-e SPECULA_DIR="$repo/Specula" -e SPECULA_WEBKIT="$repo/webkit" -e WK_HOST=host.docker.internal
      -e CODEX_HOME=/home/specula/.codex -e COMPOSE_MENU=false)
[ -z "${NVIDIA_API_KEY:-}" ] || envs+=(-e NVIDIA_API_KEY)
if [ "$uses_codex" = 1 ]; then
    mounts+=(-v "$key_file:/run/secrets/openai_key:ro")
fi

# The cap applies to this invocation's spending (a resumed run's Codex home already has earlier sessions).
baseline_usd=0
if [ -n "$cap_usd" ]; then
    baseline_usd=$(python3 "$repo/scripts/codex_cost.py" "$codex_home" --prices "$prices" --json | python3 -c "import json,sys; print(json.load(sys.stdin)['usd'])")
fi
watchdog() {
    while sleep 60; do
        docker inspect "$container_name" >/dev/null 2>&1 || return 0
        line=$(python3 "$repo/scripts/codex_cost.py" "$codex_home" --prices "$prices" 2>/dev/null) || continue
        usd=${line%% *}; usd=${usd#\$}
        spent=$(python3 -c "import sys; print(round(float(sys.argv[1]) - float(sys.argv[2]), 4))" "$usd" "$baseline_usd")
        echo "[cost $(date +%H:%M)] this invocation \$$spent; total $line (cap \$$cap_usd)" >> "$run_root/cost.log"
        if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)" "$spent" "$cap_usd"; then
            echo "COST CAP REACHED: $line >= \$$cap_usd; stopping the run" | tee -a "$run_root/cost.log" >&2
            docker stop -t 120 "$container_name" >/dev/null 2>&1 || true
            return 0
        fi
    done
}
[ -z "$cap_usd" ] || { watchdog & watchdog_pid=$!; }

echo "container: $container_name (sees: Specula, webkit, $run_root)"
set +e
docker run --rm --name "$container_name" --group-add 0 "${mounts[@]}" "${envs[@]}" \
    -w "$repo/Specula" --entrypoint bash "$image" \
    -c '[ ! -f /run/secrets/openai_key ] || export OPENAI_API_KEY="$(cat /run/secrets/openai_key)"; exec /usr/local/bin/agent-entrypoint.sh specula "$@"' \
    _ "${args[@]}" "$@" ${target[@]+"${target[@]}"}
status=$?
set -e
[ -z "${watchdog_pid:-}" ] || kill "$watchdog_pid" 2>/dev/null || true
[ -z "$cap_usd" ] || python3 "$repo/scripts/codex_cost.py" "$codex_home" --prices "$prices" | sed 's/^/final cost: /' | tee -a "$run_root/cost.log"

# The pipeline stops the app's stack only after a successful run; stop it in every other case.
if [ "$status" -ne 0 ]; then
    for cfg in "$repo"/Specula/runs/*/"$name"/.specula-output/stack/stack.toml; do
        [ -f "$cfg" ] || continue
        python3 "$repo/webkit/stack/wkstack.py" down --config "$cfg" --runs-dir "$(dirname "$cfg")/runs" >/dev/null 2>&1 || true
    done
fi
echo "specula exited $status; app copy: $copy"
exit "$status"
