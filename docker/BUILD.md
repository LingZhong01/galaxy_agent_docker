# Galaxy Agent GPU 镜像构建说明

本文只说明如何基于 Dockerfile 构建、验证和导出镜像。服务配置与启动方法见
[DEPLOY.md](DEPLOY.md)。

## 镜像内容

镜像基于 CUDA 12.4.1，包含：

- communication service
- galaxy morphology MCP
- GalfitS 与 GALFIT
- Codex CLI 与 Claude CLI
- 统一的 Python 3.11 `mcp` 环境
- JAX 0.8.0 CUDA 12 plugin/PJRT
- CUPTI、NVCC/PTXAS 与 `jnesty`

MCP、GalfitS 和结果后处理均使用 `/opt/conda/envs/mcp/bin/python`，不再维护第二套
GalfitS Python 环境。

## 构建机要求

- Docker 28+ 与 Buildx
- NVIDIA 驱动
- NVIDIA Container Toolkit（构建后 GPU 验证需要）
- 以下源码或二进制：
  - `../../new_version/galaxy_morphology_mcp`
  - `..`（communication service 工程根目录）
  - `/home/ad/galfits/GalfitS`
  - `/usr/local/bin/galfit`
  - `/home/ad/.local/share/claude/versions/2.1.186`
  - 本地镜像 `galaxy_morphology_mcp:0.0.15`
  - 本地上一版镜像 `galaxy-agent:gpu-local`（复用已验证的 Claude Agent SDK）

旧镜像仅作为构建阶段来源，用于复制非 PyPI 的 MIT `jnesty` 源码及已验证版本的
CUPTI/NVCC CUDA 组件。最终镜像运行时不依赖旧镜像。

Dockerfile 只复制明确列出的源码和数据，不复制工程 `.env`、Git 元数据、日志或认证文件。

## 构建

进入构建目录：

```bash
cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service/docker
```

使用构建脚本：

```bash
chmod +x build-local.sh
./build-local.sh
```

或者使用 Compose 构建：

```bash
docker compose build galaxy-agent
```

首次构建需要下载 CUDA 基础镜像、Python/Conda 包和 Codex npm 包，耗时较长且需要
足够的 Docker 存储空间。

## 查看构建结果

```bash
docker image inspect galaxy-agent:gpu-local \
  --format 'ID={{.Id}} CREATED={{.Created}} SIZE={{.Size}}'
```

## 依赖和 GPU 验证

验证统一 Python 环境、GalfitS 导入及 GPU：

```bash
docker run --rm --gpus all \
  --entrypoint /bin/bash \
  galaxy-agent:gpu-local -lc '
    /opt/conda/envs/mcp/bin/python -m pip check &&
    GS_DATA_PATH=/opt/galfits/data \
    PYTHONPATH=/opt/app/galaxy_morphology_mcp/src:/opt/galfits/GalfitS/src \
    /opt/conda/envs/mcp/bin/python -c \
      "from galfits import gsutils; import jax, jnesty, regions; print(jax.__version__); print(jax.default_backend()); print(jax.devices())"
  '
```

正确结果应包含：

```text
No broken requirements found.
0.8.0
gpu
[CudaDevice(id=0), CudaDevice(id=1)]
```

GPU 数量以目标服务器实际配置为准。如果只显示 CPU，检查 NVIDIA 驱动、NVIDIA
Container Toolkit 和 `--gpus all`。

为了避免挂载的 `/home/agent/.local` 覆盖镜像依赖，运行服务时应设置：

```dotenv
PYTHONNOUSERSITE=1
```

## 导出镜像

```bash
docker save galaxy-agent:gpu-local \
  | gzip > galaxy-agent-gpu-local.tar.gz
```

查看文件大小：

```bash
ls -lh galaxy-agent-gpu-local.tar.gz
```

完整性校验：

```bash
gzip -t galaxy-agent-gpu-local.tar.gz
```

命令无输出且退出码为 0 表示 gzip 文件完整：

```bash
echo $?
```

## 在目标服务器导入镜像

```bash
gunzip -c galaxy-agent-gpu-local.tar.gz | docker load
```

确认导入结果：

```bash
docker image inspect galaxy-agent:gpu-local \
  --format 'ID={{.Id}} SIZE={{.Size}}'
```

导入后的服务配置和启动流程见 [DEPLOY.md](DEPLOY.md)。

## 发布到 Harbor

本次版本为 `0.0.2`：

```bash
docker tag galaxy-agent:gpu-local \
  harbor.zhejianglab.com/galaxy_morphology_platform_module/galaxy-agent:0.0.2

docker login harbor.zhejianglab.com

docker push \
  harbor.zhejianglab.com/galaxy_morphology_platform_module/galaxy-agent:0.0.2
```

发布完成后记录 `docker push` 输出的 digest，用于部署端核对镜像版本。
