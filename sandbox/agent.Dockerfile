# Agent sandbox: Specula's agents run in here and see only what is mounted (Specula, webkit, the
# app copy, the run's config). They drive the app's stack through the host's Docker daemon.
#
#   docker build -t specula-sandbox:latest sandbox
#   docker build -t specula-agent:latest -f sandbox/agent.Dockerfile sandbox
FROM docker:29-cli AS dockercli
FROM node:24-bookworm-slim AS node

FROM specula-sandbox:latest
USER root

# Docker CLI + compose plugin (to run the app's stack through the host daemon).
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# Node 24 (Ubuntu's apt Node is too old for the agent CLIs), then the Codex CLI pinned to the
# host's version.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g @openai/codex@0.164.0 opencode-ai@1.19.6 \
 && rm -rf /root/.npm

# Python environment for Specula's TLC task tools (MCP: start_tlc / wait_tlc), outside the mounted
# checkout; Specula finds it through SPECULA_TLC_TOOLS_PYTHON.
RUN python3 -m venv /opt/tlc-tools && /opt/tlc-tools/bin/pip install --no-cache-dir 'mcp>=1.28.1,<2'
ENV SPECULA_TLC_TOOLS_PYTHON=/opt/tlc-tools/bin/python

COPY agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod 755 /usr/local/bin/agent-entrypoint.sh
USER specula
ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
