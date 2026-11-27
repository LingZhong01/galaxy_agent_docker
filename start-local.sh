#!/usr/bin/env bash
set -euo pipefail

cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service

export MODEL_SETTINGS_KEY_FILE="${MODEL_SETTINGS_KEY_FILE:-/mnt/data/galaxy_decomposition_evaluation/code/platform_v4_local/runtime/model-settings.key}"

# main.py loads the complete local configuration from the repository-root
# .env. Explicit shell variables still take precedence over that file.
exec python3 -m uvicorn main:app \
  --host "${COMMUNICATION_HOST:-0.0.0.0}" \
  --port "${COMMUNICATION_PORT:-8010}"
