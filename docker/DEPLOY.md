# Galaxy Agent 服务部署与启动说明

本文只说明如何导入运行配置和认证、启动及管理服务。如何通过 Dockerfile 构建和导出
镜像见 [BUILD.md](BUILD.md)。

## 部署前要求

- 可以访问 Harbor，或已导入 `galaxy-agent:gpu-local`
- Docker 与 Docker Compose
- NVIDIA 驱动
- NVIDIA Container Toolkit
- `docker-compose.yml` 和 `.env.example`

目标服务器不需要单独安装 GalfitS、GALFIT、Conda、Codex 或 Claude。

推荐直接从 Harbor 拉取 `0.0.2`：

```bash
docker login harbor.zhejianglab.com
docker pull \
  harbor.zhejianglab.com/galaxy_morphology_platform_module/galaxy-agent:0.0.2

# 当前 Compose 使用本地运行标签 galaxy-agent:gpu-local
docker tag \
  harbor.zhejianglab.com/galaxy_morphology_platform_module/galaxy-agent:0.0.2 \
  galaxy-agent:gpu-local
```

版本 `0.0.2` 的 Harbor manifest digest：

```text
sha256:088b4a5696b92cbfe572695b4a716131ce9823797987e300917dc368d9e614b4
```

## 1. 进入部署目录

```bash
cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service/docker
```

服务器上的实际部署路径可以不同，但 `docker-compose.yml`、`.env` 和 `runtime/` 应位于
同一部署目录下。

## 2. 准备运行配置

```bash
cp -n .env.example .env
chmod 600 .env
```

编辑 `.env`，至少确认：

```dotenv
# 主流程继续使用 Claude CLI
AGENT_BACKEND=claude

# Claude CLI 直接连接内部模型网关
ANTHROPIC_BASE_URL=http://IP:4000
ANTHROPIC_AUTH_TOKEN=replace-with-real-key
CLAUDE_MODEL=glm-5.3-csst-anthropic

AGENT_TIMEOUT_SEC=7200
COMMUNICATION_SERVICE_PUBLIC_URL=http://127.0.0.1:8010
MCP_TIMEOUT=120000
MCP_CONNECT_TIMEOUT_MS=120000

COMMUNICATION_FILE_BACKEND=auto
COMMUNICATION_OSS_BASE_URL=https://platform.example.com/api/csst
COMMUNICATION_OSS_DOWNLOAD_PATH=/fitting/v2/oss/files/download
COMMUNICATION_OSS_UPLOAD_PATH=/fitting/v2/oss/files
COMMUNICATION_OSS_FOLDER_UPLOAD_PATH=/fitting/v2/oss/folders
COMMUNICATION_OSS_TIMEOUT_SEC=300

# 禁止持久化 home 中的旧 Python 包覆盖镜像统一环境
PYTHONNOUSERSITE=1
```

`COMMUNICATION_SERVICE_PUBLIC_URL=http://127.0.0.1:8010` 用于同一容器内部的 MCP 事件
回传，不应改成 `0.0.0.0`。Uvicorn 已监听 `0.0.0.0:8010`，Compose 会将宿主机
`0.0.0.0:8010` 映射到容器。

如果图像或残差分析工具需要 VLM，可另外配置：

```dotenv
OPENAI_API_KEY=
OPENAI_BASE_URL=http://internal-ip:4000
OPENAI_MODEL=gemini-3.1-pro-preview-csst
ANALYSIS_MODE=vlm
```

平台提交 `/api/local-fit-requests` 时可在请求顶层携带任意 JSON `bizInfo`。服务不解析
其结构，只透传给子进程；MCP 会将原值注入 Gemini 请求的
`metadata.spend_logs_metadata.bizInfo`。Claude 主流程使用任务级
`x-litellm-spend-logs-metadata` Header，其中 `bizInfo` 按网关要求编码为 JSON 字符串。

Claude CLI 会调用网关的 `/v1/messages` Anthropic-compatible 接口。

不要将包含真实密钥的 `.env` 提交到 Git。

## 3. 准备持久化目录

```bash
mkdir -p runtime/home runtime/data
sudo chown -R 1000:1000 runtime/home runtime/data
```

容器以 UID 1000 运行。没有 sudo 权限时，需要让服务器管理员执行一次目录授权。

目录映射：

```text
runtime/home -> /home/agent
runtime/data -> /data
```

当前 Claude 主流程通过 `.env` 中的 `ANTHROPIC_AUTH_TOKEN` 鉴权，不需要在
`runtime/home` 中放置 `.claude.json`、`.claude/` 或其他 Claude 登录文件。
`runtime/home` 仅保留给可选的 CLI 缓存及 Codex 兼容模式。

运行数据位于：

```text
runtime/data/staging
runtime/data/state
runtime/data/log
```

不要将 `runtime/` 提交到 Git。

## 4. 可选：导入 Codex 认证

当前设置 `AGENT_BACKEND=claude` 时可跳过本节。只有切换回 Codex backend 时才需要：

推荐在目标服务器通过一次性容器登录：

```bash
docker compose run --rm -it galaxy-agent codex login
```

认证会写入 `runtime/home/.codex/`，后续重建容器仍可复用。

也可以复制宿主机已有认证：

```bash
mkdir -p runtime/home/.codex
cp /home/ad/.codex/auth.json runtime/home/.codex/auth.json
cp /home/ad/.codex/config.toml runtime/home/.codex/config.toml
sudo chown -R 1000:1000 runtime/home/.codex
chmod 700 runtime/home/.codex
chmod 600 runtime/home/.codex/auth.json
```

不要复制 `models_cache.json`，避免 Codex CLI 版本不同造成缓存格式冲突。

## 5. 启动服务

镜像已经导入或构建完成时，不再触发构建：

```bash
docker compose up -d --no-build
```

检查容器和健康状态：

```bash
docker compose ps
curl http://127.0.0.1:8010/health
```

其他机器可通过 `http://服务器IP:8010` 访问，但宿主机防火墙和安全组也必须允许该端口。

## 6. 查看日志

```bash
docker compose logs -f --tail=200 galaxy-agent
```

持久化日志位于：

```text
runtime/data/log
```

## 7. 修改配置后重新加载

仅执行 `docker compose restart` 不会重新注入修改后的 `.env`。应重新创建容器：

```bash
docker compose up -d --no-build --force-recreate
```

挂载在 `runtime/home` 中的认证和 `runtime/data` 中的业务数据不会丢失。

## 8. 验证统一环境和 GPU

```bash
docker compose exec galaxy-agent \
  /opt/conda/envs/mcp/bin/python -c \
  "import jax,jaxlib; print(jax.__version__); print(jaxlib.__version__); print(jax.default_backend()); print(jax.devices())"
```

预期应显示 JAX/JAXLIB 0.8.0、`gpu` 和至少一个 `CudaDevice`。

如果输出路径来自 `/home/agent/.local/lib/python3.11/site-packages`，说明用户目录中的旧包
覆盖了镜像环境。确认 `.env` 已设置 `PYTHONNOUSERSITE=1`，然后强制重新创建容器。

## 9. 停止服务

停止但保留容器：

```bash
docker compose stop
```

停止并删除容器，但保留绑定目录中的认证和业务数据：

```bash
docker compose down
```

不要随意添加 `-v`，以免删除 Compose 管理的其他数据卷。
