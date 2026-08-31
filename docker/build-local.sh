#!/usr/bin/env bash
set -euo pipefail

docker buildx build \
  --load \
  --tag galaxy-agent:gpu-local \
  --build-context mcp_source=../../new_version/galaxy_morphology_mcp \
  --build-context communication_source=.. \
  --build-context galfits_source=/home/ad/galfits/GalfitS \
  --build-context galfit_source=/usr/local/bin \
  --build-context claude_source=/home/ad/.local/share/claude/versions \
  --build-arg CODEX_VERSION=0.144.6 \
  .
