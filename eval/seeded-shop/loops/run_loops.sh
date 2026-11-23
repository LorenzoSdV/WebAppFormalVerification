#!/usr/bin/env bash
# Run the M5b loop tests with the agent profile given (default: free-tier Nemotron).
#   eval/seeded-shop/loops/run_loops.sh [AGENT_CONFIG] [L1 L2 L3]
# Workspaces must exist (eval/seeded-shop/prepare_specula.py --variant ...), and the tracing
# stack must be up for L2/L3 (their confirmation phase replays requests against the app).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
CONFIG="${1:-$REPO/config/agents/nemotron.json}"; shift || true
CASES=("${@:-L1 L2 L3}")
export PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:$PATH"
TARGET="seeded-shop|LorenzoSdV/WebAppFormalVerification|TypeScript|seeded shop API"
for c in ${CASES[@]}; do
  ws="$REPO/runs/loop-$c"
  echo "=== $c: $(date)"
  cd "$ws" || exit 1
  if [ "$c" = "L1" ]; then
    specula validate --agent=opencode --model=nvidia/nvidia/nemotron-3-ultra-550b-a55b \
      --artifact="$REPO/apps/seeded-shop" seeded-shop > "$ws/loop.log" 2>&1
  else
    specula run --no-isolate --skip-analysis --skip-specgen --skip-harness --max-repair-rounds=3 \
      --agent-config="$CONFIG" --artifact="$REPO/apps/seeded-shop" "$TARGET" > "$ws/loop.log" 2>&1
  fi
  echo "=== $c finished with exit $? at $(date)"
done
