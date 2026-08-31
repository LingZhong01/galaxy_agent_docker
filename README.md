# Communication Service

`communication_service` 是平台侧与 GALFIT 智能拟合工作流之间的中转服务。它接收平台或测试客户端提交的拟合请求，创建持久化任务，启动独立的 Codex CLI 或 Claude Code 子进程，并通过 stdio 方式挂载 `galaxy_morphology_mcp`，由所选 Agent 按工作流调用 GALFIT 工具完成多轮拟合。

## 核心能力

- 接收平台模拟器或业务平台提交的本地 GALFIT 拟合请求。
- 为每个任务维护 SQLite 状态、事件流和最终结果。
- 启动隔离的 Codex 或 Claude 进程，并为其配置 `galaxy_morphology_mcp` stdio MCP 服务。
- 通过 MCP 启动环境注入的事件地址接收每轮 `fit_round_finished`，记录产物、统计量和归档路径；拟合 tools 不暴露通信参数。
- 提供 SSE 实时事件流，方便前端或平台侧跟踪拟合进度。
- 保留旧版 MCP 外部 Agent 中转接口，兼容 `request_agent` / `wait_for_agent_response` 工具。

## 目录结构

```text
communication_service/
├── main.py              # FastAPI 服务入口、任务编排、Agent 子进程管理
├── direct_fit_runner.py # human 请求的单轮直接拟合入口
├── test_agent_backend.py
├── test_oss_backend.py
├── requirements.txt     # 服务依赖
├── .env                 # 服务启动时自动读取的本地配置
├── .env.example         # 不含凭据的部署配置模板
├── log/                 # 默认日志目录，可用环境变量覆盖
└── README.md
```

## 环境要求

- Python 3.10+
- Codex CLI 或 Claude Code，且运行服务的系统用户已完成对应 CLI 的认证
- 可用的 `galaxy_morphology_mcp` 工程
- `galaxy_morphology_mcp` 的运行环境中已配置 `GALFIT_BIN`

安装依赖：

```bash
pip install -r requirements.txt
cp .env.example .env
```

复制后请至少确认 MCP 工程路径、任务暂存目录、数据库目录、日志目录以及 Agent/Python 可执行文件路径。部署用户必须对 `LOCAL_GALFIT_STAGE_ROOT`、`COMMUNICATION_SERVICE_DB` 的父目录和 `COMMUNICATION_SERVICE_LOG_DIR` 具有写权限。

## 配置

服务端统一控制 MCP、Agent 和数据根目录配置，请不要把这些配置暴露给客户端请求。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GALAXY_DATA_ROOT` | `/mnt/data` | 允许任务访问的数据根目录，`feedme` 和 `galaxy_dir` 必须位于此目录下 |
| `GALAXY_MCP_ROOT` | `/mnt/data/galaxy_decomposition_evaluation/code/agent/new_version/galaxy_morphology_mcp` | MCP 工程路径 |
| `COMMUNICATION_SERVICE_DB` | `/tmp/communication_service.sqlite3` | SQLite 任务数据库，生产环境建议放到持久卷 |
| `COMMUNICATION_SERVICE_PUBLIC_URL` | `http://127.0.0.1:8010` | 供同机 MCP 轮次事件发布器访问的内部回环地址 |
| `COMMUNICATION_SERVICE_LOG_DIR` | `communication_service/log` | 日志目录 |
| `AGENT_BACKEND` | `codex` | AI 请求执行后端，只能是 `codex` 或 `claude`；human 请求不使用 Agent |
| `AGENT_TIMEOUT_SEC` | `7200` | 单个 Agent 子进程超时时间 |
| `DIRECT_FIT_PYTHON` | `/home/ad/miniconda3/envs/mcp/bin/python` | human 单轮拟合使用的 MCP Python 环境 |
| `DIRECT_GALFITS_PYTHON` | `/home/ad/miniconda3/envs/galfits/bin/python` | human 多波段拟合使用的 GalfitS Python 环境 |
| `CODEX_BIN` | `codex` | Codex CLI 命令 |
| `CODEX_LOAD_CLAUDE_PROJECT` | `true` | Codex 是否兼容加载 MCP 工程的 `CLAUDE.md`、`.claude/skills` 和 agent 角色规范 |
| `CLAUDE_BIN` | `claude` | Claude Code CLI 命令 |
| `MCP_TIMEOUT` | `120000` | Claude 等待单个 MCP 服务启动的最长时间（毫秒） |
| `MCP_CONNECT_TIMEOUT_MS` | `120000` | Claude 首次请求等待 MCP 连接并生成工具快照的最长时间（毫秒） |
| `CLAUDE_MODEL` | 空 | 可选 Claude 模型，例如 `sonnet` |
| `CLAUDE_MAX_BUDGET_USD` | 空 | 可选的单任务 Claude API 费用上限 |
| `MAX_CONCURRENT_CODEX_WORKERS` | `1` | 最大并发工作进程数 |
| `LOCAL_GALFIT_STAGE_ROOT` | `code/agent/test_data` | 平台请求转换成本地任务时的暂存目录；生产环境必须使用独立目录，不要与人工测试数据混用 |
| `STAGE_CLEANUP_ENABLED` | `true` | 是否启用暂存目录定期清理 |
| `STAGE_RETENTION_DAYS` | `30` | 暂存星系目录的保留天数；运行中任务始终受到保护 |
| `STAGE_CLEANUP_INTERVAL_HOURS` | `24` | 暂存目录清理周期（小时）；服务启动时也会执行一次 |
| `COMMUNICATION_FILE_BACKEND` | `auto` | 文件后端：`local`、`oss` 或自动判断 |
| `COMMUNICATION_OSS_BASE_URL` | 平台 API 地址 | OSS 文件接口基础地址 |
| `COMMUNICATION_OSS_DOWNLOAD_PATH` | `/fitting/v2/oss/files/download` | OSS 单文件下载接口 |
| `COMMUNICATION_OSS_UPLOAD_PATH` | `/fitting/v2/oss/files` | OSS 单文件上传接口 |
| `COMMUNICATION_OSS_FOLDER_UPLOAD_PATH` | `/fitting/v2/oss/folders` | OSS ZIP/multipart 文件夹上传接口 |
| `COMMUNICATION_OSS_TIMEOUT_SEC` | `300` | OSS 下载及上传超时秒数 |
| `COMMUNICATION_SERVICE_MAX_LOG_BODY_CHARS` | `50000` | HTTP 请求体写入日志时的最大字符数 |
| `AGENT_STREAM_LIMIT_BYTES` | `16777216` | 单个 Agent 输出流允许保留的最大字节数 |
| `GALAXY_MCP_STDIO_COMMAND` | 内置 conda + `python -m mcp_server --transport stdio` 命令 | 覆盖 MCP stdio 启动命令 |

示例：

```bash
export GALAXY_DATA_ROOT=/mnt/data
export GALAXY_MCP_ROOT=/mnt/data/galaxy_decomposition_evaluation/code/agent/new_version/galaxy_morphology_mcp
export LOCAL_GALFIT_STAGE_ROOT=/mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service_staging
export COMMUNICATION_SERVICE_PUBLIC_URL=http://127.0.0.1:8010
export COMMUNICATION_SERVICE_DB=/tmp/communication_service.sqlite3
```

切换 Agent 只需修改 `.env` 后重启 8010 服务：

```dotenv
# 二选一
AGENT_BACKEND=codex
# AGENT_BACKEND=claude

CODEX_BIN=/path/to/codex
CLAUDE_BIN=/path/to/claude
```

Claude 模式会以 `GALAXY_MCP_ROOT` 为工作目录，因此会自动加载该项目的
`CLAUDE.md`、`.claude/agents` 和 `.claude/skills`；拟合暂存目录通过
`--add-dir` 提供。客户端仍然请求同一个 `/api/local-fit-requests` API，
请求 body 无需增加 Agent 类型。

`fit_type=human` 时，服务不会启动 Codex 或 Claude。OSS 输入暂存完成后，
`direct_fit_runner.py` 会在 MCP Python 环境中直接调用一次拟合函数：
单波段使用 `run_galfit`；多波段根据 `fitting_mode` 使用对应的
`run_galfits_*` 函数。轮次事件、OSS 产物上传和平台 callback 仍由
communication service 的原有流程处理。若直接拟合结束但 8010 没有收到
`fit_round_finished` 事件，任务会标记为失败。

### 启动检查和暂存清理

服务启动时会检查数据根目录、MCP 工程及工作流文件、所选 Agent、两套直接拟合 Python，以及暂存、数据库和日志目录的写权限。任何关键项不可用时，服务会拒绝启动并一次性列出全部问题。

暂存清理只扫描 `LOCAL_GALFIT_STAGE_ROOT/{owner}/{project}/{galaxy}`，不会扫描该目录之外的文件。服务启动时清理一次，之后按 `STAGE_CLEANUP_INTERVAL_HOURS` 定期执行；只有整个星系暂存树超过 `STAGE_RETENTION_DAYS` 才会删除。数据库中处于 `queued`、`starting` 或 `running` 状态的任务始终受到保护。清理结果写入日志的 `stage.cleanup` 事件，也会在 `/health` 中展示当前清理配置。

单波段 human 请求可传 `"args": ["--step", "2000"]`（也接受字符串
`"--step 2000"`）；服务会将其转换为 GALFIT 参数 `-imax 2000`。不传
`args` 时保持原有默认参数 `-o`。多波段请求的 `args` 则原样作为 GalfitS
额外参数传递。

### 旧版 human 请求兼容

`/api/local-fit-requests` 同时兼容后端旧版 human 请求：

```json
{
  "fit_type": "human",
  "data_type": "single_band",
  "node_id": "229",
  "fitting_mode": "Image Fitting",
  "lyric_file": "/owner/project/galaxy/229/input/galfit.feedme",
  "workplace": null,
  "output_path": "/owner/project/galaxy/229/output/",
  "args": ["--step", "100"],
  "callback_url": "http://backend/fitting/v2/nodes/callback"
}
```

服务从 `lyric_file` 的 OSS key 解析 owner、project、galaxy 和 node。单波段
会下载 feedme，解析其中 `G)` 并从当前 `input/` 下载约束文件，再根据
`A/C/D/F` 从星系 `raw/` 下载数据；`--step N` 转成 GALFIT `-imax N`。
多波段会下载 lyric 和其中引用的 raw 文件；`--readpar`、`--readsummary`、
`--priorpath`、`--parconstrain` 后的 OSS 文件也会下载并替换成本地路径。
非空的 OSS `workplace` 会下载到任务暂存目录，目录响应按 ZIP 解压。

旧版 human 提交成功只返回：

```json
{"status": "success", "task_id": "..."}
```

任务结束后只向 `callback_url` 回调一次：

```json
{"task_id": "...", "status": "success", "message": ""}
```

失败时 `status` 为 `failed`，错误信息放在 `message`。完整版平台请求及其
逐轮事件契约保持不变。

启用 `CODEX_LOAD_CLAUDE_PROJECT=true` 时，Codex 同样以 `GALAXY_MCP_ROOT`
为项目目录，并把 `CLAUDE.md` 作为项目指令的回退文件，将
`.claude/skills/*/SKILL.md` 注册为 Codex skills。`.claude/agents` 不是
Codex 的原生目录，因此服务会在工作流 prompt 中要求 Codex 在锁定最优轮次前读取
`best-round-verifier.md` 并执行相同的只读审计。

使用 OSS 后端时，`current_metadata.input_dir`、`output_dir` 和
`input_dir_file_list` 应填写 OSS key（例如 `/owner/project/galaxy/node/input/file.feedme`）。
服务会把输入和 raw 数据下载到 `LOCAL_GALFIT_STAGE_ROOT`，拟合完成后将每轮产物上传到
对应节点的 OSS `input`/`output` 目录；文本通过单文件接口按 UTF-8 上传，FITS、PNG
等二进制文件通过 ZIP/multipart folder 接口上传，以保持原始字节不变。
`platform_callback_url` 收到的 `artifacts` 也是 OSS key。

## 启动、停止与重启

服务启动时会自动读取本目录下的 `.env`。修改 `.env` 后不会自动重载，
必须重启 8010 服务才能生效。

首次启动先安装依赖：

```bash
cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service
python -m pip install -r requirements.txt
```

前台启动（调试时推荐）：

```bash
cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

后台启动：

```bash
cd /mnt/data/galaxy_decomposition_evaluation/code/agent/communication_service
nohup python -m uvicorn main:app --host 0.0.0.0 --port 8010 \
  > service.log 2>&1 &
```

查看端口和进程：

```bash
ss -ltnp | grep ':8010'
ps -ef | grep '[u]vicorn main:app.*8010'
```

前台运行时按 `Ctrl+C` 停止。后台运行时，先从上面的命令找到 PID，再优雅停止：

```bash
kill <PID>
```

确认 8010 已释放后，重新执行后台或前台启动命令即可。不要同时启动两个
`main:app`，否则会出现 `Address already in use`。

查看日志：

```bash
tail -f service.log
ls -lt log/communication_service_*.log | head
```

如果使用时间戳日志，可以对最新文件执行：

```bash
tail -f "$(ls -t log/communication_service_*.log | head -1)"
```

健康检查：

```bash
curl http://10.15.48.208:8010/health
```

健康检查应返回 `status=ok`，同时显示当前实际生效的 `agent_backend`、
`agent_bin`、数据库、MCP 根目录和日志文件。MCP 与 8010 在同机运行时，
`.env` 中的 `COMMUNICATION_SERVICE_PUBLIC_URL` 应保留为
`http://127.0.0.1:8010`；外部平台仍可通过 `10.15.48.208:8010` 请求服务。

## 直接提交 AI GALFIT 工作流

`feedme` 和 `galaxy_dir` 必须是绝对路径，并且位于 `GALAXY_DATA_ROOT` 下。同一个星系目录同一时间只会运行一个任务，避免 GALFIT 工作文件互相覆盖。

```bash
curl -X POST http://10.15.48.208:8010/api/galfit-workflows \
  -H 'content-type: application/json' \
  -d '{
    "feedme": "/mnt/data/galaxy/g123/input/g123.feedme",
    "galaxy_dir": "/mnt/data/galaxy/g123",
    "options": ["-o"],
    "max_rounds": 8,
    "instructions": "执行核球-盘分解，优先使用物理上可解释的最简模型",
    "idempotency_key": "g123-20260623"
  }'
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 服务健康检查 |
| `POST` | `/api/galfit-workflows` | 直接提交当前 AI Agent 驱动的 GALFIT 工作流 |
| `POST` | `/api/local-fit-requests` | 接收平台模拟器格式的拟合请求 |
| `GET` | `/api/galfit-workflows/{task_id}` | 查询任务状态和最终结果 |
| `GET` | `/api/galfit-workflows/{task_id}/events?after=0` | 增量查询任务事件 |
| `GET` | `/api/galfit-workflows/{task_id}/events/stream` | 订阅任务 SSE 事件流 |
| `POST` | `/api/galfit-workflows/{task_id}/fit-events` | 接收 MCP 每轮拟合事件 |
| `POST` | `/api/galfit-workflows/{task_id}/cancel` | 取消任务并终止对应的 Agent 或直接拟合子进程 |
| `POST` | `/api/fit-events` | 旧版全局拟合事件接口 |
| `POST` | `/api/agent/requests` | 旧版外部 Agent 请求接口 |

任务状态包括：

- `queued`
- `starting`
- `running`
- `succeeded`
- `failed`
- `cancelled`

查询与取消示例：

```bash
curl http://10.15.48.208:8010/api/galfit-workflows/TASK_ID
curl 'http://10.15.48.208:8010/api/galfit-workflows/TASK_ID/events?after=0'
curl -X POST http://10.15.48.208:8010/api/galfit-workflows/TASK_ID/cancel
```

## 与平台模拟器联调

1. 启动本服务：

   ```bash
   python -m uvicorn main:app --host 0.0.0.0 --port 8010
   ```

2. 启动 `platform_simulation_service`：

   ```bash
   cd ../platform_simulation_service
   python -m uvicorn main:app --host 0.0.0.0 --port 8020
   ```

3. 使用返回的 `task_id` 查看事件：

   ```bash
   curl -N http://10.15.48.208:8010/api/galfit-workflows/TASK_ID/events/stream
   ```

## 注意事项

- Codex/Claude 认证由运行服务的系统用户提供，服务不会接受或保存请求方的认证信息。
- `idempotency_key` 可避免重复提交导致重复启动任务。
- 服务重启后历史任务和事件仍可查询，但重启时仍在运行的 Agent/直接拟合子进程不会自动恢复。
- 生产环境建议用进程管理器守护服务，并把 `COMMUNICATION_SERVICE_DB` 和日志目录挂到持久存储。

## Docker GPU 镜像

完整的 Galaxy Agent GPU 镜像构建与部署文件位于 `docker/`。构建和启动说明分开维护：

- [docker/BUILD.md](docker/BUILD.md)：基于 Dockerfile 构建、验证和导出镜像。
- [docker/DEPLOY.md](docker/DEPLOY.md)：导入运行配置与认证、启动和管理服务。

`docker/runtime/`、真实 `.env`、CLI 认证文件以及导出的 `*.tar.gz` 均为本地运行数据，
已由 `.gitignore` 排除，不应提交到 Git。
