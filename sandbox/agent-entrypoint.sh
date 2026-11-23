#!/usr/bin/env bash
# Prepare the container's own agent environment, then run the given command (e.g. `specula run ...`).
#   SPECULA_DIR   mounted Specula checkout (required)
#   OPENAI_API_KEY  if set, Codex is logged in with it (into $CODEX_HOME, never the host's ~/.codex)
set -euo pipefail
: "${SPECULA_DIR:?SPECULA_DIR must point at the mounted Specula checkout}"

# Python environment (inside the container; never in the mounted checkout).
(cd "$SPECULA_DIR" && uv sync --locked --quiet)
export PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

# Skills for the agent CLIs, installed into this container's home.
python3 -I "$SPECULA_DIR/src/specula/skill_install.py" --source "$SPECULA_DIR/skills" --target "$HOME/.agents/skills" >/dev/null
python3 -I "$SPECULA_DIR/src/specula/skill_install.py" --source "$SPECULA_DIR/skills" \
    --target "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills" >/dev/null

if [ -n "${OPENAI_API_KEY:-}" ]; then
    mkdir -p "${CODEX_HOME:-$HOME/.codex}"
    printenv OPENAI_API_KEY | codex login --with-api-key >/dev/null
fi

docker info --format 'docker: {{.ServerVersion}} ({{.OperatingSystem}})' || {
    echo "agent-entrypoint: cannot reach the Docker daemon (mount /var/run/docker.sock)" >&2; exit 1; }

exec "$@"
