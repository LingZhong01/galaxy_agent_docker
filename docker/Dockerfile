# syntax=docker/dockerfile:1.7

ARG CUDA_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
ARG NODE_IMAGE=node:22.23.0-bookworm-slim
ARG MICROMAMBA_IMAGE=mambaorg/micromamba:2.3.2
ARG LEGACY_GALFITS_IMAGE=galaxy_morphology_mcp:0.0.15

FROM ${NODE_IMAGE} AS node_runtime
FROM ${MICROMAMBA_IMAGE} AS micromamba_runtime
FROM ${LEGACY_GALFITS_IMAGE} AS legacy_galfits
FROM ${CUDA_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG CODEX_VERSION=0.144.6

ENV TZ=UTC \
    MAMBA_ROOT_PREFIX=/opt/conda \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_ROOT_USER_ACTION=ignore \
    MPLCONFIGDIR=/tmp/matplotlib-galaxy-mcp \
    XLA_PYTHON_CLIENT_PREALLOCATE=false \
    PATH=/opt/conda/envs/mcp/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git libgomp1 libncurses5 libtinfo5 passwd tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

COPY --from=micromamba_runtime /bin/micromamba /usr/local/bin/micromamba
COPY requirements-galfits-gpu.txt /tmp/requirements-galfits-gpu.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    micromamba create -y -n mcp -c conda-forge python=3.11 pip \
    && micromamba run -n mcp python -m pip install \
         -r /tmp/requirements-galfits-gpu.txt \
    && micromamba clean --all --yes

# The known-good legacy image includes the CUPTI wheel required by JAX's CUDA
# plugin.  The CUDA runtime base alone does not provide this library.
COPY --from=legacy_galfits /usr/local/lib/python3.11/dist-packages/nvidia/cuda_cupti \
  /opt/conda/envs/mcp/lib/python3.11/site-packages/nvidia/cuda_cupti
COPY --from=legacy_galfits /usr/local/lib/python3.11/dist-packages/nvidia_cuda_cupti_cu12-12.9.79.dist-info \
  /opt/conda/envs/mcp/lib/python3.11/site-packages/nvidia_cuda_cupti_cu12-12.9.79.dist-info
COPY --from=legacy_galfits /usr/local/lib/python3.11/dist-packages/nvidia/cuda_nvcc \
  /opt/conda/envs/mcp/lib/python3.11/site-packages/nvidia/cuda_nvcc
COPY --from=legacy_galfits /usr/local/lib/python3.11/dist-packages/nvidia_cuda_nvcc_cu12-12.9.86.dist-info \
  /opt/conda/envs/mcp/lib/python3.11/site-packages/nvidia_cuda_nvcc_cu12-12.9.86.dist-info

# These named BuildKit contexts are supplied by build-local.sh/docker-compose.yml.
# Copy only explicit paths so source .env files, Git metadata, logs and credentials
# are never baked into the image.
COPY --from=mcp_source pyproject.toml README.md CLAUDE.md AGENTS.md /opt/app/galaxy_morphology_mcp/
COPY --from=mcp_source src /opt/app/galaxy_morphology_mcp/src
COPY --from=mcp_source .claude /opt/app/galaxy_morphology_mcp/.claude
COPY --from=mcp_source .codex /opt/app/galaxy_morphology_mcp/.codex
COPY --from=mcp_source .agents /opt/app/galaxy_morphology_mcp/.agents

COPY --from=communication_source main.py direct_fit_runner.py requirements.txt README.md \
  /opt/app/communication_service/

COPY --from=galfits_source src /opt/galfits/GalfitS/src
COPY --from=galfits_source examples/data /opt/galfits/data
COPY --from=legacy_galfits /app/jnesty /opt/vendor/jnesty
COPY --from=galfit_source galfit /opt/bin/galfit
COPY --from=claude_source 2.1.186 /usr/local/bin/claude

RUN --mount=type=cache,target=/root/.cache/pip \
    chmod 0755 /opt/bin/galfit /usr/local/bin/claude \
    && micromamba run -n mcp python -m pip install --no-deps /opt/vendor/jnesty \
    && micromamba run -n mcp python -m pip install \
         /opt/app/galaxy_morphology_mcp \
         -r /opt/app/communication_service/requirements.txt \
    && GS_DATA_PATH=/opt/galfits/data \
       PYTHONPATH=/opt/app/galaxy_morphology_mcp/src:/opt/galfits/GalfitS/src \
       micromamba run -n mcp python -c \
         "import astroquery, evosax, galfits, jax, jaxopt, lmfit, optax, regions, reproject, sep; from galfits import gsutils; print('unified MCP/GalfitS imports OK'); print('JAX', jax.__version__)" \
    && micromamba run -n mcp python -m pip check \
    && mkdir -p /data/staging /data/state /data/log /tmp/matplotlib-galaxy-mcp \
    && /usr/sbin/useradd --create-home --uid 1000 --shell /bin/bash agent \
    && chown -R agent:agent /data /tmp/matplotlib-galaxy-mcp /opt/app /opt/galfits

ENV GALAXY_DATA_ROOT=/data \
    GALAXY_MCP_ROOT=/opt/app/galaxy_morphology_mcp \
    LOCAL_GALFIT_STAGE_ROOT=/data/staging \
    COMMUNICATION_SERVICE_DB=/data/state/communication_service.sqlite3 \
    COMMUNICATION_SERVICE_LOG_DIR=/data/log \
    COMMUNICATION_SERVICE_PUBLIC_URL=http://127.0.0.1:8010 \
    CODEX_BIN=/usr/local/bin/codex \
    CLAUDE_BIN=/usr/local/bin/claude \
    DIRECT_FIT_PYTHON=/opt/conda/envs/mcp/bin/python \
    DIRECT_GALFITS_PYTHON=/opt/conda/envs/mcp/bin/python \
    GALFIT_BIN=/opt/bin/galfit \
    GALFITS_BIN="/opt/conda/envs/mcp/bin/python /opt/galfits/GalfitS/src/galfits/galfitS.py" \
    GS_DATA_PATH=/opt/galfits/data \
    PYTHONPATH=/opt/app/galaxy_morphology_mcp/src:/opt/galfits/GalfitS/src \
    GALAXY_MCP_STDIO_COMMAND="cd {mcp_root} && PYTHONPATH=src:/opt/galfits/GalfitS/src MPLCONFIGDIR=/tmp/matplotlib-galaxy-mcp FIT_ROUND_EVENT_URL={event_url} /opt/conda/envs/mcp/bin/python -m mcp_server --transport stdio"

WORKDIR /opt/app/communication_service
USER agent
EXPOSE 8010
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8010/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/conda/envs/mcp/bin/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
