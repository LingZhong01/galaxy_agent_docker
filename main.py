"""Communication service and Codex-driven GALFIT workflow runner.

The legacy ``/api/agent`` endpoints are kept for the MCP external-agent
handoff.  The ``/api/galfit-workflows`` endpoints are a separate task API:
each task starts one isolated ``codex exec`` process and gives it a stdio MCP
server for galaxy-morphology-mcp.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import shlex
import sqlite3
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from cryptography.fernet import Fernet, InvalidToken
from google_service_account import (
    service_account_access_token,
    vertex_model_name,
    vertex_openai_base_url,
)


def load_local_env() -> None:
    """Load the local service environment from one ``.env`` file.

    Local execution uses the repository-root ``.env`` as its complete source
    of defaults.  ``COMMUNICATION_ENV_FILE`` may point to another complete env
    file when an explicit deployment profile is needed.  Values already set
    in the process environment always have the highest priority.
    """

    service_root = Path(__file__).resolve().parent
    explicit_keys = set(os.environ)
    configured_file = os.getenv("COMMUNICATION_ENV_FILE", "").strip()
    env_file = Path(configured_file).expanduser() if configured_file else service_root / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in explicit_keys:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


load_local_env()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Settings:
    """Server-owned configuration.  None of these values come from requests."""

    service_root = Path(__file__).resolve().parent
    data_root = Path(os.getenv("GALAXY_DATA_ROOT", "/mnt/data")).resolve()
    mcp_root = Path(
        os.getenv(
            "GALAXY_MCP_ROOT",
            "/mnt/data/galaxy_decomposition_evaluation/code/agent/new_version/galaxy_morphology_mcp",
        )
    ).resolve()
    workflow_file = mcp_root / "src/prompts/workflow_galfit.md"
    workflow_galfits_file = mcp_root / "src/prompts/workflow_galfits.md"
    # Source trees are often mounted read-only in deployment.  Operators should
    # set COMMUNICATION_SERVICE_DB to a persistent volume; /tmp keeps local
    # development usable without writing into the repository.
    database_file = Path(os.getenv("COMMUNICATION_SERVICE_DB", "/tmp/communication_service.sqlite3")).resolve()
    # The MCP subprocess runs on the same host and may be network-sandboxed,
    # so its round callbacks must use loopback rather than the host LAN IP.
    public_url = os.getenv("COMMUNICATION_SERVICE_PUBLIC_URL", "http://127.0.0.1:8010").rstrip("/")
    log_dir = Path(os.getenv("COMMUNICATION_SERVICE_LOG_DIR", service_root / "log")).resolve()
    log_file = log_dir / f"communication_service_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    max_log_body_chars = int(os.getenv("COMMUNICATION_SERVICE_MAX_LOG_BODY_CHARS", "50000"))
    local_stage_root = Path(
        os.getenv(
            "LOCAL_GALFIT_STAGE_ROOT",
            "/mnt/data/galaxy_decomposition_evaluation/code/agent/test_data",
        )
    ).resolve()
    stage_cleanup_enabled = os.getenv("STAGE_CLEANUP_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    stage_retention_days = max(1, int(os.getenv("STAGE_RETENTION_DAYS", "30")))
    stage_cleanup_interval_hours = max(1, int(os.getenv("STAGE_CLEANUP_INTERVAL_HOURS", "24")))
    file_backend = os.getenv("COMMUNICATION_FILE_BACKEND", "auto").strip().lower()
    oss_base_url = os.getenv(
        "COMMUNICATION_OSS_BASE_URL",
        "https://astro-workbench.eva24002.lab.zverse.space:32443/api/csst",
    ).rstrip("/")
    oss_download_path = os.getenv("COMMUNICATION_OSS_DOWNLOAD_PATH", "/fitting/v2/oss/files/download")
    oss_upload_path = os.getenv("COMMUNICATION_OSS_UPLOAD_PATH", "/fitting/v2/oss/files")
    oss_folder_upload_path = os.getenv(
        "COMMUNICATION_OSS_FOLDER_UPLOAD_PATH", "/fitting/v2/oss/folders"
    )
    oss_timeout_sec = max(10, int(os.getenv("COMMUNICATION_OSS_TIMEOUT_SEC", "300")))
    agent_backend = os.getenv("AGENT_BACKEND", "codex").strip().lower()
    codex_bin = os.getenv("CODEX_BIN", "codex")
    codex_load_claude_project = os.getenv("CODEX_LOAD_CLAUDE_PROJECT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    claude_model = os.getenv("CLAUDE_MODEL", "").strip()
    claude_max_budget_usd = os.getenv("CLAUDE_MAX_BUDGET_USD", "").strip()
    direct_fit_python = os.getenv(
        "DIRECT_FIT_PYTHON",
        "/home/ad/miniconda3/envs/mcp/bin/python",
    )
    direct_galfits_python = os.getenv(
        "DIRECT_GALFITS_PYTHON",
        "/home/ad/miniconda3/envs/galfits/bin/python",
    )
    agent_timeout_sec = max(
        60,
        int(os.getenv("AGENT_TIMEOUT_SEC", os.getenv("CODEX_TIMEOUT_SEC", "7200"))),
    )
    agent_stream_limit_bytes = max(
        1024 * 1024,
        int(os.getenv("AGENT_STREAM_LIMIT_BYTES", str(16 * 1024 * 1024))),
    )
    stream_drain_timeout_sec = max(
        1,
        int(os.getenv("COMMUNICATION_STREAM_DRAIN_TIMEOUT_SEC", "10")),
    )
    max_concurrent_workers = max(1, int(os.getenv("MAX_CONCURRENT_CODEX_WORKERS", "1")))
    # This is server configuration, not a client-provided command.  It starts
    # the MCP in stdio mode and lets its own .env load GALFIT_BIN.
    # modified by zl: inject a task-scoped event URL into the stdio MCP process.
    mcp_launch_command = os.getenv(
        "GALAXY_MCP_STDIO_COMMAND",
        "source /home/ad/miniconda3/etc/profile.d/conda.sh && conda activate mcp "
        "&& cd {mcp_root} && PYTHONPATH=src MPLCONFIGDIR=/tmp/matplotlib-galaxy-mcp "
        "FIT_ROUND_EVENT_URL={event_url} "
        "python -m mcp_server --transport stdio",
    )


SETTINGS = Settings()
SETTINGS.log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(SETTINGS.log_file)],
)
LOGGER = logging.getLogger("communication_service")
REQUESTS: dict[str, dict[str, Any]] = {}
FIT_EVENTS: list[dict[str, Any]] = []
CONDITION = asyncio.Condition()
RUNNING_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
WORKER_SEMAPHORE = asyncio.Semaphore(SETTINGS.max_concurrent_workers)
GALAXY_LOCKS: dict[str, asyncio.Lock] = {}


class AgentRequestIn(BaseModel):
    request: dict[str, Any]
    request_id: str | None = None


class AgentResponseIn(BaseModel):
    status: Literal["success", "failure", "needs_action"]
    result: dict[str, Any] = Field(default_factory=dict)


class FitRoundEventIn(BaseModel):
    event: Literal["fit_round_finished"]
    fitter: str
    status: Literal["success"]
    round_id: str
    archive_dir: str
    round_status_file: str
    artifacts: list[str] = Field(default_factory=list)
    fit_statistics: dict[str, Any] = Field(default_factory=dict)


class GalfitWorkflowIn(BaseModel):
    feedme: str
    galaxy_dir: str | None = None
    options: list[str] = Field(default_factory=lambda: ["-o"])
    max_rounds: int = Field(default=8, ge=1, le=30)
    instructions: str = Field(default="", max_length=8000)
    # modified by zl: preserve opaque platform bizInfo without parsing it.
    biz_info: Any = Field(default=None, alias="bizInfo")
    idempotency_key: str | None = Field(default=None, max_length=128)


def _legacy_oss_path_context(config_path: str) -> dict[str, str]:
    parts = [part for part in PurePosixPath(config_path).parts if part != "/"]
    if len(parts) < 6 or parts[-2] != "input":
        raise ValueError(
            "legacy human lyric_file must be an OSS path like "
            "/owner/project_name/galaxy_id/node_id/input/config"
        )
    return {
        "owner": parts[-6],
        "project_name": parts[-5],
        "galaxy_id": parts[-4],
        "node_id": parts[-3],
        "input_dir": "/" + "/".join(parts[:-1]) + "/",
    }


_MULTIBAND_FILE_ARG_NAMES = {"--readpar", "--readsummary", "--priorpath", "--parconstrain"}


def _legacy_arg_file_paths(args: list[str] | str | None) -> list[str]:
    values = args.split() if isinstance(args, str) else list(args or [])
    paths: list[str] = []
    for index, value in enumerate(values[:-1]):
        if value in _MULTIBAND_FILE_ARG_NAMES and str(values[index + 1]).startswith("/"):
            paths.append(str(values[index + 1]))
    return paths


class LocalFitRequestIn(BaseModel):
    owner: str
    parent_metadata: dict[str, Any] | None = None
    current_metadata: dict[str, Any]
    fit_type: str = "AI"
    data_type: str | None = None
    # Human requests are normalized to one direct fitting round later, so the
    # platform may omit this field or explicitly send zero.
    fit_max_round: int = Field(default=1, ge=0, le=30)
    # Scientific requirements belong to the fitting contract itself. Keep
    # bizInfo opaque for deployment/accounting integrations.
    fit_instruction: str = Field(default="", max_length=8000)
    fitting_mode: str | None = None
    args: list[str] | str | None = None
    workplace: str | None = None
    project_name: str
    galaxy_id: str
    node_id: str
    parent_node_id: str | None = None
    platform_callback_url: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    # Encrypted per-user model profiles. The service persists only this opaque
    # token; plaintext API keys exist only in the spawned process environment.
    model_settings_token: str | None = Field(default=None, max_length=20000)
    # Compatibility fields used by the previous backend human-fit contract.
    lyric_file: str | None = None
    output_path: str | None = None
    callback_url: str | None = None
    # modified by zl: preserve opaque platform bizInfo without parsing it.
    biz_info: Any = Field(default=None, alias="bizInfo")
    legacy_human_request: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_human_request(cls, value):
        if not isinstance(value, dict):
            return value
        fit_type = str(value.get("fit_type") or "").strip().lower()
        config_path = value.get("lyric_file")
        if fit_type != "human" or not config_path or value.get("current_metadata") is not None:
            return value

        context = _legacy_oss_path_context(str(config_path))
        requested_node_id = str(value.get("node_id") or context["node_id"])
        if requested_node_id != context["node_id"]:
            raise ValueError("legacy human node_id does not match lyric_file OSS path")
        output_path = str(
            value.get("output_path")
            or _oss_join(
                context["owner"],
                context["project_name"],
                context["galaxy_id"],
                context["node_id"],
                "output",
            )
        )
        input_files = [str(config_path)]
        input_files.extend(
            path for path in _legacy_arg_file_paths(value.get("args")) if path not in input_files
        )
        return {
            **value,
            **context,
            "node_id": requested_node_id,
            "parent_metadata": None,
            "parent_node_id": None,
            "fit_max_round": 0,
            "platform_callback_url": value.get("callback_url"),
            "current_metadata": {
                "owner": context["owner"],
                "project_name": context["project_name"],
                "galaxy_id": context["galaxy_id"],
                "node_id": requested_node_id,
                "round_index": 1,
                "fitting_mode": value.get("fitting_mode") or "Image Fitting",
                "fit_type": "human",
                "input_dir": context["input_dir"],
                "output_dir": output_path,
                "input_dir_file_list": input_files,
                "output_dir_file_list": [],
            },
            "legacy_human_request": True,
        }

    # fit_max_round=0 means that the platform does not impose a scientific
    # round cap. The Agent/MCP workflow decides when the result is sufficient;
    # process timeout and cancellation remain operational safety controls.


def _connect() -> sqlite3.Connection:
    SETTINGS.database_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SETTINGS.database_file)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS galfit_tasks (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                state TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER
            );
            CREATE TABLE IF NOT EXISTS galfit_task_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES galfit_tasks(task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_galfit_task_events_task_sequence
              ON galfit_task_events(task_id, sequence);
            """
        )


def recover_interrupted_tasks() -> None:
    """Fail tasks orphaned by a previous communication-service process."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT task_id FROM galfit_tasks WHERE state IN ('starting', 'running')"
        ).fetchall()
    for row in rows:
        task_id = str(row["task_id"])
        result = {
            "error": "communication_service restarted while workflow was active",
            "exit_code": None,
            "business_state": "failed",
        }
        _set_task(task_id, "failed", result, finished_at=now(), pid=None)
        _append_event(task_id, "workflow.failed", result)


def resume_queued_tasks() -> list[asyncio.Task[None]]:
    """Recreate in-memory workers for durable tasks that have not started yet.

    Queued workflows are persisted before their asyncio task is created.  A
    service restart therefore needs to recreate those asyncio tasks; otherwise
    the database continues to report ``queued`` while no worker can consume
    them.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT task_id, request_json FROM galfit_tasks WHERE state = 'queued' "
            "ORDER BY created_at, task_id"
        ).fetchall()
    resumed: list[asyncio.Task[None]] = []
    for row in rows:
        task_id = str(row["task_id"])
        try:
            request = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            result = {
                "error": f"invalid persisted workflow request: {exc}",
                "exit_code": None,
                "business_state": "failed",
            }
            _set_task(task_id, "failed", result, finished_at=now(), pid=None)
            _append_event(task_id, "workflow.failed", result)
            continue
        _append_event(task_id, "workflow.resumed", {"reason": "service_restart"})
        resumed.append(asyncio.create_task(_run_workflow(task_id, request)))
    return resumed


def validate_deployment() -> None:
    """Fail fast when required deployment paths or executables are unavailable."""
    errors: list[str] = []

    if not SETTINGS.data_root.is_dir():
        errors.append(f"GALAXY_DATA_ROOT is not a directory: {SETTINGS.data_root}")
    if not SETTINGS.mcp_root.is_dir():
        errors.append(f"GALAXY_MCP_ROOT is not a directory: {SETTINGS.mcp_root}")
    mcp_entrypoint = SETTINGS.mcp_root / "src/mcp_server.py"
    if not mcp_entrypoint.is_file():
        errors.append(f"MCP entrypoint is missing: {mcp_entrypoint}")
    for workflow_file in (SETTINGS.workflow_file, SETTINGS.workflow_galfits_file):
        if not workflow_file.is_file():
            errors.append(f"required MCP workflow file is missing: {workflow_file}")

    try:
        SETTINGS.local_stage_root.relative_to(SETTINGS.data_root)
    except ValueError:
        errors.append(
            f"LOCAL_GALFIT_STAGE_ROOT must be under GALAXY_DATA_ROOT: "
            f"{SETTINGS.local_stage_root} is not under {SETTINGS.data_root}"
        )

    writable_dirs = {
        "LOCAL_GALFIT_STAGE_ROOT": SETTINGS.local_stage_root,
        "COMMUNICATION_SERVICE_DB parent": SETTINGS.database_file.parent,
        "COMMUNICATION_SERVICE_LOG_DIR": SETTINGS.log_dir,
    }
    for label, directory in writable_dirs.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"{label} cannot be created ({directory}): {exc}")
            continue
        if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
            errors.append(f"{label} is not writable: {directory}")

    executable_settings = {
        "selected Agent executable": (
            SETTINGS.claude_bin if SETTINGS.agent_backend == "claude" else SETTINGS.codex_bin
        ),
        "DIRECT_FIT_PYTHON": SETTINGS.direct_fit_python,
        "DIRECT_GALFITS_PYTHON": SETTINGS.direct_galfits_python,
    }
    for label, executable in executable_settings.items():
        if shutil.which(executable) is None:
            errors.append(f"{label} is not executable or not on PATH: {executable}")

    if SETTINGS.agent_backend not in {"codex", "claude"}:
        errors.append(f"AGENT_BACKEND must be codex or claude, got: {SETTINGS.agent_backend!r}")

    if errors:
        raise RuntimeError("deployment validation failed:\n- " + "\n- ".join(errors))
    LOGGER.info(
        "deployment.validation_ok %s",
        json.dumps(
            {
                "data_root": str(SETTINGS.data_root),
                "mcp_root": str(SETTINGS.mcp_root),
                "stage_root": str(SETTINGS.local_stage_root),
                "database": str(SETTINGS.database_file),
                "log_dir": str(SETTINGS.log_dir),
                "agent_backend": SETTINGS.agent_backend,
            },
            ensure_ascii=False,
        ),
    )


def _active_stage_galaxy_dirs() -> set[Path]:
    protected: set[Path] = set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT request_json FROM galfit_tasks WHERE state IN ('queued', 'starting', 'running')"
        ).fetchall()
    for row in rows:
        try:
            request = json.loads(row["request_json"])
            local = request.get("local_platform") or {}
            value = local.get("stage_galaxy_dir")
            if value:
                path = Path(value).resolve()
                path.relative_to(SETTINGS.local_stage_root)
                protected.add(path)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return protected


def _latest_tree_mtime(root: Path) -> float:
    latest = root.stat().st_mtime
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in (*dirnames, *filenames):
            try:
                latest = max(
                    latest,
                    os.stat(current / name, follow_symlinks=False).st_mtime,
                )
            except FileNotFoundError:
                continue
    return latest


def cleanup_staging_once() -> dict[str, Any]:
    """Remove expired owner/project/galaxy staging trees, never active tasks."""
    if not SETTINGS.stage_cleanup_enabled:
        return {"enabled": False, "removed": [], "protected": []}
    root = SETTINGS.local_stage_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SETTINGS.stage_retention_days)).timestamp()
    protected = _active_stage_galaxy_dirs()
    removed: list[str] = []
    for candidate in root.glob("*/*/*"):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if any(active == resolved or resolved in active.parents for active in protected):
            continue
        try:
            if _latest_tree_mtime(resolved) >= cutoff:
                continue
            shutil.rmtree(resolved)
            removed.append(str(resolved))
        except FileNotFoundError:
            continue
        except OSError:
            LOGGER.exception("stage.cleanup_failed path=%s", resolved)
    result = {
        "enabled": True,
        "retention_days": SETTINGS.stage_retention_days,
        "removed": removed,
        "protected": [str(path) for path in sorted(protected)],
    }
    LOGGER.info("stage.cleanup %s", json.dumps(result, ensure_ascii=False))
    return result


async def _stage_cleanup_loop() -> None:
    interval = SETTINGS.stage_cleanup_interval_hours * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cleanup_staging_once)
        except Exception:
            LOGGER.exception("stage.cleanup_loop_failed")


def _task_row(task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM galfit_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    task["request"] = json.loads(task.pop("request_json"))
    raw_result = task.pop("result_json")
    task["result"] = json.loads(raw_result) if raw_result else None
    return task


def _append_event(task_id: str, event_type: str, payload: dict[str, Any]) -> int:
    created_at = now()
    LOGGER.info(
        "workflow.event %s",
        json.dumps(
            {"task_id": task_id, "created_at": created_at, "type": event_type, "payload": payload},
            ensure_ascii=False,
            default=str,
        ),
    )
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO galfit_task_events(task_id, created_at, type, payload_json) VALUES (?, ?, ?, ?)",
            (task_id, created_at, event_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        conn.execute("UPDATE galfit_tasks SET updated_at = ? WHERE task_id = ?", (created_at, task_id))
        return int(cursor.lastrowid)


def _set_task(task_id: str, state: str, result: dict[str, Any] | None = None, **columns: Any) -> None:
    updates: dict[str, Any] = {"state": state, "updated_at": now(), **columns}
    if result is not None:
        updates["result_json"] = json.dumps(result, ensure_ascii=False, default=str)
    keys = list(updates)
    with _connect() as conn:
        conn.execute(
            f"UPDATE galfit_tasks SET {', '.join(f'{key} = ?' for key in keys)} WHERE task_id = ?",
            [updates[key] for key in keys] + [task_id],
        )


def _events_after(task_id: str, after: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sequence, created_at, type, payload_json FROM galfit_task_events "
            "WHERE task_id = ? AND sequence > ? ORDER BY sequence",
            (task_id, max(0, after)),
        ).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


def _safe_path(value: str, field: str) -> Path:
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(SETTINGS.data_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be under {SETTINGS.data_root}") from exc
    return path


def _decode_body_for_log(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if len(text) > SETTINGS.max_log_body_chars:
        text = text[: SETTINGS.max_log_body_chars] + f"... <truncated {len(text) - SETTINGS.max_log_body_chars} chars>"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _validate_workflow_request(body: GalfitWorkflowIn) -> dict[str, Any]:
    feedme = _safe_path(body.feedme, "feedme")
    if not feedme.is_file():
        raise HTTPException(status_code=422, detail=f"feedme does not exist: {feedme}")
    galaxy_dir = _safe_path(body.galaxy_dir, "galaxy_dir") if body.galaxy_dir else feedme.parent
    if not galaxy_dir.is_dir():
        raise HTTPException(status_code=422, detail=f"galaxy_dir does not exist: {galaxy_dir}")
    if feedme.parent != galaxy_dir and galaxy_dir not in feedme.parents:
        raise HTTPException(status_code=422, detail="feedme must be inside galaxy_dir")
    if not all(option and "\x00" not in option for option in body.options):
        raise HTTPException(status_code=422, detail="options must be non-empty strings")
    return {
        "feedme": str(feedme),
        "galaxy_dir": str(galaxy_dir),
        "options": body.options,
        "max_rounds": body.max_rounds,
        "instructions": body.instructions,
        "bizInfo": body.biz_info,
    }


_FEEDME_PATH_RE = re.compile(r"^\s*([A-Z])\)\s*([^\s#]+)")
_INPUT_FEEDME_FIELDS = {"A", "C", "D", "F", "G"}
_SKIP_PATH_VALUES = {"", "none", "null", "nil", "0"}


def _copy_file(src: Path, dst: Path) -> None:
    src = src.resolve()
    dst = dst.resolve()
    if src == dst:
        return
    if dst.is_file():
        try:
            src_stat = src.stat()
            dst_stat = dst.stat()
            if src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime) == int(dst_stat.st_mtime):
                return
        except OSError:
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _select_file_backend(input_dir: str) -> str:
    backend = SETTINGS.file_backend
    if backend in {"local", "oss"}:
        return backend
    if backend != "auto":
        raise HTTPException(status_code=500, detail="COMMUNICATION_FILE_BACKEND must be local, oss, or auto")
    return "local" if Path(input_dir).expanduser().exists() else "oss"


def _oss_join(*parts: str) -> str:
    clean = [str(part).strip("/") for part in parts if str(part).strip("/")]
    return "/" + "/".join(clean)


def _oss_parent(path: str, levels: int = 1) -> str:
    posix = PurePosixPath(path)
    for _ in range(levels):
        posix = posix.parent
    return str(posix)


def _download_oss_file(oss_path: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst.with_name(f".{dst.name}.download")
    endpoint = SETTINGS.oss_base_url + SETTINGS.oss_download_path
    query = json.dumps({"filePath": oss_path}, ensure_ascii=False)
    LOGGER.info("oss.download %s", query)
    request = UrlRequest(f"{endpoint}?{urlencode({'filePath': oss_path})}", method="GET")
    try:
        with urlopen(request, timeout=SETTINGS.oss_timeout_sec) as response, open(tmp_path, "wb") as f:
            shutil.copyfileobj(response, f)
        tmp_path.replace(dst)
    except HTTPError as exc:
        tmp_path.unlink(missing_ok=True)
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"OSS download failed for {oss_path}: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"OSS download failed for {oss_path}: {exc.reason}") from exc


def _download_oss_folder(oss_path: str, dst: Path) -> Path:
    """Download the OSS folder ZIP response and extract it under dst."""
    dst.mkdir(parents=True, exist_ok=True)
    folder_name = PurePosixPath(oss_path.rstrip("/")).name
    final_path = dst / folder_name
    zip_path = dst / f".{folder_name}.zip"
    endpoint = SETTINGS.oss_base_url + SETTINGS.oss_download_path
    request = UrlRequest(
        f"{endpoint}?{urlencode({'filePath': oss_path})}",
        method="GET",
    )
    LOGGER.info("oss.download %s", json.dumps({"filePath": oss_path}, ensure_ascii=False))
    try:
        with urlopen(request, timeout=SETTINGS.oss_timeout_sec) as response, open(zip_path, "wb") as f:
            shutil.copyfileobj(response, f)
        with zipfile.ZipFile(zip_path) as archive:
            root = final_path.resolve()
            for member in archive.infolist():
                target = (final_path / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise HTTPException(status_code=502, detail="OSS workplace ZIP contains an unsafe path")
            archive.extractall(final_path)
        return final_path
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"OSS folder download failed for {oss_path}: HTTP {exc.code}: {detail}",
        ) from exc
    except (URLError, zipfile.BadZipFile) as exc:
        detail = exc.reason if isinstance(exc, URLError) else str(exc)
        raise HTTPException(
            status_code=502,
            detail=f"OSS folder download failed for {oss_path}: {detail}",
        ) from exc
    finally:
        zip_path.unlink(missing_ok=True)


def _upload_oss_file(src: Path, oss_path: str) -> None:
    """Upload one local artifact through the platform OSS file API."""
    raw = src.read_bytes()
    text_suffixes = {
        ".con", ".cons", ".constrain", ".feedme", ".json", ".log", ".lyric",
        ".md", ".prior", ".py", ".txt", ".yaml", ".yml",
    }
    if src.suffix.lower() not in text_suffixes:
        _upload_oss_binary_file(src, oss_path, raw)
        return

    endpoint = SETTINGS.oss_base_url + SETTINGS.oss_upload_path
    payload = {"filePath": oss_path, "content": raw.decode("utf-8"), "encoding": "utf-8"}
    LOGGER.info(
        "oss.upload %s",
        json.dumps({"filePath": oss_path, "encoding": "utf-8", "bytes": len(raw)}, ensure_ascii=False),
    )
    request = UrlRequest(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=SETTINGS.oss_timeout_sec) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"OSS upload failed for {oss_path}: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"OSS upload failed for {oss_path}: {exc.reason}") from exc


def _upload_oss_binary_file(src: Path, oss_path: str, raw: bytes | None = None) -> None:
    """Upload a binary file through the OSS folder ZIP endpoint.

    The single-file JSON endpoint stores base64 text verbatim in this
    deployment.  The multipart folder endpoint preserves the original bytes.
    """
    raw = src.read_bytes() if raw is None else raw
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr(PurePosixPath(oss_path).name, raw)
    zip_bytes = archive.getvalue()
    boundary = f"----communication-service-{uuid4().hex}"
    target_path = str(PurePosixPath(oss_path).parent).rstrip("/") + "/"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="targetPath"\r\n\r\n'
        f"{target_path}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{src.name}.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8") + zip_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = UrlRequest(
        SETTINGS.oss_base_url + SETTINGS.oss_folder_upload_path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    LOGGER.info(
        "oss.upload %s",
        json.dumps({"filePath": oss_path, "encoding": "zip", "bytes": len(raw)}, ensure_ascii=False),
    )
    try:
        with urlopen(request, timeout=SETTINGS.oss_timeout_sec) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"OSS binary upload failed for {oss_path}: HTTP {exc.code}: {detail}",
        ) from exc
    except URLError as exc:
        raise HTTPException(
            status_code=502, detail=f"OSS binary upload failed for {oss_path}: {exc.reason}"
        ) from exc


def _platform_parent(path: str, backend: str) -> str:
    return _oss_parent(path) if backend == "oss" else str(Path(path).parent)


def _platform_target(directory: str, filename: str, backend: str) -> str:
    return _oss_join(directory, filename) if backend == "oss" else str(Path(directory) / filename)


def _store_platform_file(src: Path, target: str, backend: str) -> None:
    if backend == "oss":
        _upload_oss_file(src, target)
    else:
        _copy_file(src, Path(target))


def _fetch_input_file(source: str, dst_dir: Path, backend: str, input_dir: str | None = None) -> Path:
    basename = PurePosixPath(source).name if backend == "oss" else Path(source).name
    if not basename:
        raise HTTPException(status_code=422, detail=f"invalid input file path: {source}")
    dst = dst_dir / basename
    if backend == "local":
        src = _safe_path(source, "current_metadata.input_dir_file_list")
        if not src.is_file():
            raise HTTPException(status_code=422, detail=f"input file does not exist: {src}")
        if input_dir:
            try:
                src.relative_to(_safe_path(input_dir, "current_metadata.input_dir"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"input file is not under input_dir: {src}") from exc
        _copy_file(src, dst)
    else:
        _download_oss_file(source, dst)
    return dst


def _copy_tree_files(src_dir: Path, dst_dir: Path) -> list[str]:
    copied: list[str] = []
    if not src_dir.is_dir():
        return copied
    for src in sorted(path for path in src_dir.iterdir() if path.is_file()):
        dst = dst_dir / src.name
        _copy_file(src, dst)
        copied.append(str(dst))
    return copied


def _split_comment(line: str) -> tuple[str, str]:
    if "#" not in line:
        return line.rstrip("\n"), "\n"
    body, comment = line.rstrip("\n").split("#", 1)
    return body.rstrip(), f"  # {comment}\n"


def _raw_data_candidates(raw_dir: Path | str, basename: str, *, prefer_psf_dir: bool = False) -> list[Path | str]:
    candidates: list[Path | str] = []
    if prefer_psf_dir:
        candidates.append(raw_dir / "PSF" / basename if isinstance(raw_dir, Path) else _oss_join(raw_dir, "PSF", basename))
    candidates.append(raw_dir / basename if isinstance(raw_dir, Path) else _oss_join(raw_dir, basename))
    if not prefer_psf_dir:
        candidates.append(raw_dir / "PSF" / basename if isinstance(raw_dir, Path) else _oss_join(raw_dir, "PSF", basename))
    return candidates


def _raw_data_source(raw_dir: Path, basename: str, *, prefer_psf_dir: bool = False) -> Path:
    for candidate in _raw_data_candidates(raw_dir, basename, prefer_psf_dir=prefer_psf_dir):
        assert isinstance(candidate, Path)
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=422, detail=f"raw data file does not exist: {basename}")


def _copy_raw_data_file(raw_dir: Path | str, local_data_dir: Path, basename: str, *, prefer_psf_dir: bool = False, backend: str = "local") -> Path:
    dst = local_data_dir / basename
    if backend == "local":
        src = _raw_data_source(raw_dir if isinstance(raw_dir, Path) else Path(raw_dir), basename, prefer_psf_dir=prefer_psf_dir)
        _copy_file(src, dst)
        return dst

    last_error: HTTPException | None = None
    for candidate in _raw_data_candidates(raw_dir if isinstance(raw_dir, str) else str(raw_dir), basename, prefer_psf_dir=prefer_psf_dir):
        try:
            _download_oss_file(str(candidate), dst)
            return dst
        except HTTPException as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise HTTPException(status_code=422, detail=f"raw data file does not exist: {basename}")


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _feedme_path_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Z])\)\s*([^\s#]+)", line)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _rewrite_staged_feedme_paths(feedme: Path, raw_dir: Path | str, local_data_dir: Path, *, backend: str) -> dict[str, Any]:
    """Rewrite staged GALFIT feedme paths for local execution.

    A/C/D/F data files are copied from galaxy raw data by basename. G) stays in
    the staged input directory because it is supplied by input_dir_file_list.
    B) is GALFIT output and is forced to a local relative output filename.
    """
    local_data_dir.mkdir(parents=True, exist_ok=True)
    copied_raw_files: list[str] = []
    rewritten: dict[str, str] = {}
    lines = feedme.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    new_lines: list[str] = []

    for line in lines:
        parsed = _feedme_path_value(line)
        if not parsed:
            new_lines.append(line)
            continue
        label, value = parsed
        lower_value = value.lower()
        if label == "B":
            body, comment = _split_comment(line)
            indent = re.match(r"^(\s*)", body).group(1)
            new_value = "galfit.fit"
            new_lines.append(f"{indent}B) {new_value}{comment}")
            rewritten[label] = new_value
            continue
        if label in {"A", "C", "D", "F"}:
            if lower_value in {"", "none", "null", "nil", "0"}:
                new_lines.append(line)
                continue
            basename = Path(value).name
            local_file = _copy_raw_data_file(
                raw_dir,
                local_data_dir,
                basename,
                prefer_psf_dir=(label == "D"),
                backend=backend,
            )
            copied_raw_files.append(str(local_file))
            body, comment = _split_comment(line)
            indent = re.match(r"^(\s*)", body).group(1)
            new_value = f"./fits_files/{basename}"
            new_lines.append(f"{indent}{label}) {new_value}{comment}")
            rewritten[label] = new_value
            continue
        if label == "G" and lower_value not in {"", "none", "null", "nil", "0"}:
            basename = Path(value).name
            staged_constraint = feedme.parent / basename
            if staged_constraint.is_file():
                body, comment = _split_comment(line)
                indent = re.match(r"^(\s*)", body).group(1)
                new_value = f"./{basename}"
                new_lines.append(f"{indent}G) {new_value}{comment}")
                rewritten[label] = new_value
                continue
        new_lines.append(line)

    _atomic_write_text(feedme, "".join(new_lines))
    return {"rewritten_paths": rewritten, "copied_raw_files": sorted(set(copied_raw_files))}


_LYRIC_DATA_LINE_RE = re.compile(r"^(\s*I[a-z]([1346])\)\s*)\[(.+?)([^/\],]+?\.(?:fits?|fit))\s*,\s*([^\]]+)\](.*)$", re.IGNORECASE)


def _rewrite_staged_lyric_paths(lyric: Path, raw_dir: Path | str, local_data_dir: Path, *, backend: str) -> dict[str, Any]:
    """Rewrite staged GalfitS lyric data paths, mirroring the old FileManager."""
    local_data_dir.mkdir(parents=True, exist_ok=True)
    copied_raw_files: list[str] = []
    rewritten: dict[str, str] = {}
    lines = lyric.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    new_lines: list[str] = []

    for line in lines:
        match = _LYRIC_DATA_LINE_RE.match(line.rstrip("\n"))
        if not match:
            new_lines.append(line)
            continue
        prefix, number, _old_prefix, basename, suffix, tail = match.groups()
        local_file = _copy_raw_data_file(
            raw_dir,
            local_data_dir,
            basename,
            prefer_psf_dir=(number == "4"),
            backend=backend,
        )
        copied_raw_files.append(str(local_file))
        new_value = f"./fits_files/{basename}"
        new_lines.append(f"{prefix}[{new_value},{suffix}]{tail}\n")
        rewritten[prefix.strip().rstrip(")")] = new_value

    _atomic_write_text(lyric, "".join(new_lines))
    return {"rewritten_paths": rewritten, "copied_raw_files": sorted(set(copied_raw_files))}


def _find_feedme(input_files: list[Path], input_dir: Path) -> Path:
    candidates = [path for path in input_files if path.suffix.lower() == ".feedme"]
    if not candidates:
        candidates = sorted(input_dir.glob("*.feedme"))
    if not candidates:
        raise HTTPException(status_code=422, detail="input_dir_file_list must contain a .feedme file")
    if len(candidates) > 1:
        raise HTTPException(status_code=422, detail=f"multiple .feedme files found: {[str(path) for path in candidates]}")
    return candidates[0]


def _stage_feedme_constraint(
    feedme: Path,
    staged_input_files: list[Path],
    input_dir: str,
    backend: str,
) -> Path | None:
    """Fetch the G) constraint referenced by a legacy single-band feedme."""
    for line in feedme.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = _feedme_path_value(line)
        if not parsed or parsed[0] != "G":
            continue
        value = parsed[1]
        if value.lower() in {"", "none", "null", "nil", "0"}:
            return None
        basename = PurePosixPath(value).name
        existing = next((path for path in staged_input_files if path.name == basename), None)
        if existing:
            return existing
        source = value if backend == "oss" and value.startswith("/") else _platform_target(
            input_dir, basename, backend
        )
        staged = _fetch_input_file(source, feedme.parent, backend, input_dir)
        staged_input_files.append(staged)
        return staged
    return None


def _localize_multiband_file_args(
    args: list[str] | str | None,
    staged_input_files: list[Path],
) -> list[str]:
    values = args.split() if isinstance(args, str) else list(args or [])
    by_name = {path.name: path for path in staged_input_files}
    for index, value in enumerate(values[:-1]):
        if value not in _MULTIBAND_FILE_ARG_NAMES:
            continue
        source = str(values[index + 1])
        local_path = by_name.get(PurePosixPath(source).name)
        if local_path:
            values[index + 1] = str(local_path)
    return values


def _find_lyric(input_files: list[Path], input_dir: Path) -> Path:
    candidates = [path for path in input_files if path.suffix.lower() == ".lyric"]
    if not candidates:
        candidates = sorted(input_dir.glob("*.lyric"))
    if not candidates:
        raise HTTPException(status_code=422, detail="input_dir_file_list must contain a .lyric file")
    if len(candidates) > 1:
        raise HTTPException(status_code=422, detail=f"multiple .lyric files found: {[str(path) for path in candidates]}")
    return candidates[0]


def _normalize_data_type(body: LocalFitRequestIn) -> str:
    raw = body.data_type or body.current_metadata.get("data_type") or "single_band"
    value = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "single": "single_band",
        "singleband": "single_band",
        "galfit": "single_band",
        "multi": "multi_band",
        "multiband": "multi_band",
        "galfits": "multi_band",
    }
    value = aliases.get(value, value)
    if value not in {"single_band", "multi_band"}:
        raise HTTPException(status_code=422, detail="data_type must be single_band or multi_band")
    return value


def _normalize_fit_type(body: LocalFitRequestIn) -> str:
    value = str(body.fit_type or "AI").strip().lower()
    aliases = {"auto": "ai", "agent": "ai", "manual": "human", "one_round": "human"}
    value = aliases.get(value, value)
    if value not in {"ai", "human"}:
        raise HTTPException(status_code=422, detail="fit_type must be AI or human")
    return value


def _single_band_human_options(args: list[str] | str | None) -> list[str]:
    """Translate the platform's friendly --step option to GALFIT -imax."""
    if args is None:
        return ["-o"]
    values = args.split() if isinstance(args, str) else args
    if len(values) != 2 or values[0] != "--step":
        raise HTTPException(
            status_code=422,
            detail="single_band human args must be ['--step', '<positive integer>']",
        )
    try:
        step = int(values[1])
    except (TypeError, ValueError):
        step = 0
    if step <= 0:
        raise HTTPException(status_code=422, detail="single_band human --step must be a positive integer")
    return ["-imax", str(step)]


def _validate_feedme_input_paths(feedme: Path) -> dict[str, str]:
    """Validate GALFIT input paths in a feedme file.

    ``B)`` is intentionally skipped because it is GALFIT's output image block,
    not an input dependency.  ``G) none`` is also valid and skipped.
    """

    found: dict[str, str] = {}
    missing: list[str] = []
    for line in feedme.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _FEEDME_PATH_RE.match(line)
        if not match:
            continue
        label, raw_value = match.groups()
        if label not in _INPUT_FEEDME_FIELDS:
            continue
        value = raw_value.strip()
        found[label] = value
        if value.lower() in _SKIP_PATH_VALUES:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = feedme.parent / candidate
        if not candidate.exists():
            missing.append(f"{label}) {value} -> {candidate.resolve()}")
    if missing:
        raise HTTPException(status_code=422, detail={"message": "feedme input path validation failed", "missing": missing})
    return found


def _stage_local_fit_request(body: LocalFitRequestIn) -> dict[str, Any]:
    data_type = _normalize_data_type(body)
    fit_type = _normalize_fit_type(body)
    effective_max_rounds = 1 if fit_type == "human" else body.fit_max_round
    metadata = body.current_metadata
    input_dir_value = str(metadata.get("input_dir", ""))
    output_dir_value = str(metadata.get("output_dir", ""))
    backend = _select_file_backend(input_dir_value)

    if backend == "local":
        input_dir = _safe_path(input_dir_value, "current_metadata.input_dir")
        output_dir = _safe_path(output_dir_value, "current_metadata.output_dir")
        if not input_dir.is_dir():
            raise HTTPException(status_code=422, detail=f"input_dir does not exist: {input_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        platform_node_dir: Path | str = input_dir.parent
        platform_galaxy_dir: Path | str = platform_node_dir.parent
        raw_dir: Path | str = platform_galaxy_dir / "raw"
        if not raw_dir.is_dir():
            raise HTTPException(status_code=422, detail=f"raw directory does not exist: {raw_dir}")
        raw_input_files = metadata.get("input_dir_file_list") or sorted(str(path) for path in input_dir.iterdir() if path.is_file())
    else:
        if not input_dir_value:
            raise HTTPException(status_code=422, detail="current_metadata.input_dir is required")
        platform_node_dir = _oss_parent(input_dir_value, 1)
        platform_galaxy_dir = _oss_parent(input_dir_value, 2)
        raw_dir = _oss_join(platform_galaxy_dir, "raw")
        raw_input_files = metadata.get("input_dir_file_list") or []
        if not raw_input_files:
            raise HTTPException(status_code=422, detail="current_metadata.input_dir_file_list is required for OSS backend")

    owner = body.owner
    project_name = body.project_name
    galaxy_id = body.galaxy_id
    node_id = body.node_id
    stage_galaxy_dir = SETTINGS.local_stage_root / owner / project_name / galaxy_id
    stage_node_dir = stage_galaxy_dir / node_id
    # modified by zl: stage fitting inputs at the node workspace root so GALFIT
    # archives and GalfitS output become siblings of the configs, not input/ children.
    stage_work_dir = stage_node_dir
    stage_output_dir = stage_node_dir / "output"
    stage_raw_dir = stage_galaxy_dir / "raw"
    stage_work_dir.mkdir(parents=True, exist_ok=True)
    stage_output_dir.mkdir(parents=True, exist_ok=True)
    stage_raw_dir.mkdir(parents=True, exist_ok=True)

    copied_input_files = []
    staged_input_files: list[Path] = []
    for src in raw_input_files:
        dst = _fetch_input_file(str(src), stage_work_dir, backend, input_dir_value)
        staged_input_files.append(dst)
        copied_input_files.append(str(dst))
    copied_raw_files = _copy_tree_files(raw_dir, stage_raw_dir) if backend == "local" and isinstance(raw_dir, Path) else []
    local_data_dir = stage_work_dir / "fits_files"

    request_config: dict[str, Any]
    if data_type == "single_band":
        original_feedme = _find_feedme(staged_input_files, stage_work_dir)
        stage_feedme = stage_work_dir / original_feedme.name
        if body.legacy_human_request:
            staged_constraint = _stage_feedme_constraint(
                stage_feedme, staged_input_files, input_dir_value, backend
            )
            if staged_constraint:
                copied_input_files.append(str(staged_constraint))
        rewrite_info = _rewrite_staged_feedme_paths(stage_feedme, raw_dir, local_data_dir, backend=backend)
        copied_raw_files.extend(rewrite_info["copied_raw_files"])
        feedme_paths = _validate_feedme_input_paths(stage_feedme)
        request_config = {
            "feedme": str(stage_feedme),
            "galaxy_dir": str(stage_work_dir),
            "options": _single_band_human_options(body.args) if fit_type == "human" else ["-o"],
            "feedme_paths": feedme_paths,
            "config_rewrite": rewrite_info,
        }
    else:
        original_lyric = _find_lyric(staged_input_files, stage_work_dir)
        stage_lyric = stage_work_dir / original_lyric.name
        rewrite_info = _rewrite_staged_lyric_paths(stage_lyric, raw_dir, local_data_dir, backend=backend)
        copied_raw_files.extend(rewrite_info["copied_raw_files"])
        extra_args = _localize_multiband_file_args(body.args, staged_input_files)
        workplace = (
            body.workplace
            or metadata.get("workplace")
            or body.current_metadata.get("image_fitting_workplace")
        )
        if body.legacy_human_request and backend == "oss" and workplace:
            if str(workplace).endswith("/"):
                workplace = str(_download_oss_folder(str(workplace), stage_node_dir / "workplace"))
            else:
                workplace = str(
                    _fetch_input_file(str(workplace), stage_node_dir / "workplace", backend)
                )
        request_config = {
            "lyric_file": str(stage_lyric),
            "galaxy_dir": str(stage_work_dir),
            "extra_args": extra_args,
            "fitting_mode": body.fitting_mode or str(metadata.get("fitting_mode") or "Image Fitting"),
            "workplace": workplace,
            "feedme_paths": {},
            "config_rewrite": rewrite_info,
        }

    platform_instruction = body.fit_instruction.strip()
    workflow_instruction = (
        "严格遵循对应 workflow，完成本地平台提交的星系分解。"
        f" 平台 owner={owner}, project_name={project_name}, galaxy_id={galaxy_id},"
        f" initial_node_id={node_id}, data_type={data_type}, fit_type={fit_type}。"
    )
    if platform_instruction:
        workflow_instruction += f"\n\n平台拟合要求：\n{platform_instruction}"

    return {
        **{key: value for key, value in request_config.items() if key != "feedme_paths"},
        "data_type": data_type,
        "fit_type": fit_type,
        "max_rounds": effective_max_rounds,
        "instructions": workflow_instruction,
        "bizInfo": body.biz_info,
        "model_settings_token": body.model_settings_token,
        "local_platform": {
            "owner": owner,
            "project_name": project_name,
            "galaxy_id": galaxy_id,
            "data_type": data_type,
            "fit_type": fit_type,
            "initial_node_id": node_id,
            "current_node_id": node_id,
            "last_completed_node_id": None,
            "parent_node_id": body.parent_node_id,
            "fit_max_round": effective_max_rounds,
            "round_index": int(metadata.get("round_index") or 1),
            "platform_callback_url": body.platform_callback_url,
            "file_backend": backend,
            "platform_galaxy_dir": str(platform_galaxy_dir),
            "platform_node_dir": str(platform_node_dir),
            "platform_input_dir": input_dir_value,
            "platform_output_dir": output_dir_value,
            "round_nodes": {
                "1": {
                    "node_id": node_id,
                    "node_dir": str(platform_node_dir),
                    "input_dir": input_dir_value,
                    "output_dir": output_dir_value,
                }
            },
            "stage_node_dir": str(stage_node_dir),
            "stage_work_dir": str(stage_work_dir),
            # Compatibility alias retained for existing task/event consumers.
            "stage_input_dir": str(stage_work_dir),
            "stage_output_dir": str(stage_output_dir),
            "stage_raw_dir": str(stage_raw_dir),
            "copied_input_files": copied_input_files,
            "copied_raw_files": sorted(set(copied_raw_files)),
            "feedme_paths": request_config.get("feedme_paths", {}),
            "config_rewrite": request_config.get("config_rewrite", {}),
            "legacy_human_request": body.legacy_human_request,
        },
    }


def _count_events(task_id: str, event_type: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM galfit_task_events WHERE task_id = ? AND type = ?",
            (task_id, event_type),
        ).fetchone()
    return int(row["count"] if row else 0)


def _update_task_request(task_id: str, request: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE galfit_tasks SET request_json = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(request, ensure_ascii=False, default=str), now(), task_id),
        )


async def _ensure_platform_node_for_round(task_id: str, request: dict[str, Any], round_index: int) -> None:
    local = request.get("local_platform")
    if not local or round_index <= 1:
        return
    round_nodes = local.setdefault("round_nodes", {})
    round_key = str(round_index)
    if round_key in round_nodes:
        return
    raise RuntimeError(
        f"platform callback did not allocate node information for fitting round {round_index}"
    )


def _round_artifact_upload_name(data_type: str, name: str) -> str | None:
    normalized_type = data_type.strip().lower()
    normalized_name = name.lower()
    # FITS data remain in the immutable per-galaxy source/raw area (or in the
    # disposable communication staging workspace). Never duplicate them into
    # a persistent result-node input/output directory.
    if Path(normalized_name).suffix in {".fit", ".fits", ".fts", ".fz"} or re.fullmatch(r"galfit\.\d+", normalized_name):
        return None
    if normalized_type == "single_band":
        if normalized_name == "galfit_comparison.png":
            return "galfit_comparisonimage_fit.png"
    elif normalized_type == "multi_band":
        if normalized_name == "all_bands_comparison.png":
            return "all_bands_comparisonimage_fit.png"
        if normalized_name.endswith("image_fit.png"):
            return None
    return name


def _copy_round_artifacts_to_platform(task_id: str, request: dict[str, Any], event: dict[str, Any], round_index: int) -> dict[str, Any]:
    local = request.get("local_platform")
    if not local:
        return {}
    round_nodes = local.setdefault("round_nodes", {})
    node_info = round_nodes.get(str(round_index)) or {
        "node_id": local["current_node_id"],
        "node_dir": local["platform_node_dir"],
        "input_dir": local["platform_input_dir"],
        "output_dir": local["platform_output_dir"],
    }
    backend = local.get("file_backend", "local")
    data_type = str(request.get("data_type") or local.get("data_type") or "")
    node_id = node_info["node_id"]
    platform_node_dir = str(node_info["node_dir"])
    platform_input_dir = str(node_info["input_dir"])
    platform_output_dir = str(node_info["output_dir"])
    if backend == "local":
        Path(platform_input_dir).mkdir(parents=True, exist_ok=True)
        Path(platform_output_dir).mkdir(parents=True, exist_ok=True)

    sources: list[Path] = []
    for key in ("round_status_file",):
        value = event.get(key)
        if value:
            sources.append(Path(value))
    for value in event.get("artifacts") or []:
        sources.append(Path(value))
    archive_dir = Path(event["archive_dir"]) if event.get("archive_dir") else None
    if archive_dir and archive_dir.is_dir():
        sources.extend(path for path in archive_dir.iterdir() if path.is_file())

    copied: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[Path] = set()
    for src in sources:
        try:
            resolved = src.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        upload_name = _round_artifact_upload_name(data_type, resolved.name)
        if upload_name is None:
            continue
        suffix = resolved.suffix.lower()
        is_input_config = suffix in {".feedme", ".lyric", ".con", ".cons", ".prior", ".constrain"} or resolved.name.upper() == "GALFIT.CON"
        target_dir = platform_input_dir if is_input_config else platform_output_dir
        target = _platform_target(target_dir, upload_name, backend)
        if round_index == 1 and is_input_config:
            failed.append(
                {
                    "source": str(resolved),
                    "target": target,
                    "error": "initial round input config is not copied back",
                    "action": "skipped_initial_input",
                }
            )
            continue
        try:
            _store_platform_file(resolved, target, backend)
            copied.append(target)
        except (OSError, HTTPException) as exc:
            # A single artifact copy failure must not break the GALFIT callback
            # chain.  This is especially important for first-round input files
            # such as GALFIT.con that may already exist in the platform node
            # directory with restrictive permissions.
            if backend == "local" and Path(target).exists():
                failed.append({"source": str(resolved), "target": target, "error": str(exc), "action": "kept_existing"})
            else:
                failed.append(
                    {
                        "source": str(resolved),
                        "target": target,
                        "error": str(exc),
                        "action": "not_uploaded" if backend == "oss" else "not_copied",
                    }
                )

    _append_event(
        task_id,
        "local_platform.files_copied",
        {
            "round_index": round_index,
            "node_id": node_id,
            "platform_node_dir": platform_node_dir,
            "copied_files": copied,
            "failed_files": failed,
        },
    )
    local["last_completed_node_id"] = node_id
    local["last_completed_round_index"] = round_index
    _update_task_request(task_id, request)
    return {
        "node_id": node_id,
        "platform_node_dir": platform_node_dir,
        "input_dir": platform_input_dir,
        "output_dir": platform_output_dir,
        "copied_files": copied,
        "failed_files": failed,
    }


def _post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = UrlRequest(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status": "error", "error": f"HTTP {exc.code}: {detail}"}
    except URLError as exc:
        return {"status": "error", "error": str(exc.reason)}


async def _callback_local_platform(task_id: str, request: dict[str, Any], fit_event: dict[str, Any], round_index: int) -> None:
    local = request.get("local_platform")
    if not local or not local.get("platform_callback_url"):
        return
    await _ensure_platform_node_for_round(task_id, request, round_index)
    copied = _copy_round_artifacts_to_platform(task_id, request, fit_event, round_index)
    fit_max_round = int(local["fit_max_round"])
    has_next_round = fit_max_round == 0 or round_index < fit_max_round
    payload = {
        "task_id": task_id,
        "event": "fit_round_finished",
        "status": fit_event.get("status", "success"),
        "owner": local["owner"],
        "project_name": local["project_name"],
        "galaxy_id": local["galaxy_id"],
        "data_type": local["data_type"],
        "initial_node_id": local.get("initial_node_id"),
        "node_id": copied.get("node_id", local["current_node_id"]),
        "round_index": round_index,
        "fit_max_round": fit_max_round,
        "has_next_round": has_next_round,
        "staged_round_dir": fit_event.get("archive_dir"),
        "platform_node_dir": copied.get("platform_node_dir"),
        "input_dir": copied.get("input_dir"),
        "output_dir": copied.get("output_dir"),
        "artifacts": copied.get("copied_files", []),
        "message": fit_event.get("message"),
        "failed_artifacts": copied.get("failed_files", []),
        "fit_statistics": fit_event.get("fit_statistics", {}),
    }
    response = await asyncio.to_thread(_post_json, local["platform_callback_url"], payload)
    _append_event(task_id, "local_platform.callback_sent", {"request": payload, "response": response})
    if has_next_round:
        required_next_fields = ("next_node_id", "next_input_dir", "next_output_dir")
        missing_next_fields = [field for field in required_next_fields if not response.get(field)]
        if response.get("status") != "success" or missing_next_fields:
            detail = response.get("error") or f"missing fields: {', '.join(missing_next_fields)}"
            raise RuntimeError(f"invalid platform callback response for next fitting round: {detail}")

        next_node_id = response["next_node_id"]
        next_input_dir = response["next_input_dir"]
        next_output_dir = response["next_output_dir"]
        round_nodes = local.setdefault("round_nodes", {})
        next_round_key = str(round_index + 1)
        backend = local.get("file_backend", "local")
        round_nodes[next_round_key] = {
            "node_id": str(next_node_id),
            "node_dir": _platform_parent(str(next_input_dir), backend),
            "input_dir": str(next_input_dir),
            "output_dir": str(next_output_dir),
        }
        local["parent_node_id"] = local["current_node_id"]
        local["current_node_id"] = str(next_node_id)
        local["platform_input_dir"] = str(next_input_dir)
        local["platform_output_dir"] = str(next_output_dir)
        local["platform_node_dir"] = _platform_parent(str(next_input_dir), backend)
        _update_task_request(task_id, request)


def _copy_final_agent_artifacts_to_platform(task_id: str, request: dict[str, Any]) -> dict[str, Any]:
    local = request.get("local_platform")
    if not local:
        return {"final_artifacts": [], "failed_final_artifacts": []}
    last_round = local.get("last_completed_round_index")
    if last_round is None:
        return {"final_artifacts": [], "failed_final_artifacts": []}
    node_info = local.get("round_nodes", {}).get(str(last_round))
    if not node_info:
        return {"final_artifacts": [], "failed_final_artifacts": []}

    stage_dir = Path(request["galaxy_dir"])
    backend = local.get("file_backend", "local")
    output_dir = str(node_info["output_dir"])
    sources: list[tuple[Path, str]] = []
    working_note = stage_dir / "working_note.md"
    if working_note.is_file():
        sources.append((working_note, output_dir))
    sources.extend((path, output_dir) for path in sorted(stage_dir.glob("analysis_report_*.md")))

    with _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM galfit_task_events WHERE task_id = ? AND type = 'fit_round_finished' ORDER BY sequence",
            (task_id,),
        ).fetchall()
    for row in rows:
        event = json.loads(row["payload_json"])
        round_node = local.get("round_nodes", {}).get(str(event.get("round_index")))
        archive_dir = Path(str(event.get("archive_dir") or ""))
        comparison = archive_dir / "best_round_comparison.md"
        if round_node and comparison.is_file():
            sources.append((comparison, str(round_node["output_dir"])))

    copied: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[tuple[Path, str]] = set()
    for src, destination_dir in sources:
        resolved = src.resolve()
        source_target = (resolved, destination_dir)
        if source_target in seen:
            continue
        seen.add(source_target)
        if backend == "local":
            Path(destination_dir).mkdir(parents=True, exist_ok=True)
        target = _platform_target(destination_dir, resolved.name, backend)
        try:
            _store_platform_file(resolved, target, backend)
            copied.append(target)
        except (OSError, HTTPException) as exc:
            failed.append(
                {
                    "source": str(resolved),
                    "target": target,
                    "error": str(exc),
                    "action": "not_uploaded" if backend == "oss" else "not_copied",
                }
            )

    _append_event(
        task_id,
        "local_platform.final_artifacts_copied",
        {
            "last_completed_round_index": last_round,
            "node_id": node_info.get("node_id"),
            "output_dir": output_dir,
            "final_artifacts": copied,
            "failed_final_artifacts": failed,
        },
    )
    return {"final_artifacts": copied, "failed_final_artifacts": failed}


def _best_round_selection(task_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Map the agent archive selected by the final audit back to a platform node.

    The morphology workflow records the winning archive in ``.best_round.json``.
    Platform node IDs are allocated independently, so the stable bridge is the
    fit-round event that contains both the archive directory and round index.
    """
    local = request.get("local_platform") or {}
    stage_dir = Path(str(request.get("galaxy_dir") or ""))
    manifest_path = stage_dir / ".best_round.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    round_label = str(manifest.get("round_label") or "").strip()
    if not round_label:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM galfit_task_events WHERE task_id = ? AND type = 'fit_round_finished' ORDER BY sequence",
            (task_id,),
        ).fetchall()
    for row in rows:
        try:
            event = json.loads(row["payload_json"])
        except (ValueError, TypeError, KeyError):
            continue
        archive_dir = Path(str(event.get("archive_dir") or ""))
        if archive_dir.name != round_label:
            continue
        try:
            round_index = int(event.get("round_index"))
        except (TypeError, ValueError):
            continue
        node_info = local.get("round_nodes", {}).get(str(round_index)) or {}
        node_id = node_info.get("node_id")
        if node_id:
            return {
                "selected_node_id": str(node_id),
                "selected_round_index": round_index,
                "selected_archive_label": round_label,
            }
    return {"selected_archive_label": round_label}


async def _callback_local_platform_workflow_finished(task_id: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    local = request.get("local_platform")
    if not local or not local.get("platform_callback_url"):
        return
    if local.get("legacy_human_request"):
        succeeded = result.get("exit_code") == 0 and result.get("business_state") != "failed"
        payload = {
            "task_id": task_id,
            "status": "success" if succeeded else "failed",
            "message": "" if succeeded else str(
                result.get("error") or (result.get("final_messages") or ["Fitting failed"])[-1]
            ),
        }
        response = await asyncio.to_thread(_post_json, local["platform_callback_url"], payload)
        _append_event(
            task_id,
            "legacy_human.callback_sent",
            {"request": payload, "response": response},
        )
        return
    final_copy = _copy_final_agent_artifacts_to_platform(task_id, request)
    best_round = _best_round_selection(task_id, request)
    payload = {
        "task_id": task_id,
        "event": "workflow_finished",
        "status": "success" if result.get("exit_code") == 0 else "failed",
        "business_state": result.get("business_state"),
        "owner": local["owner"],
        "project_name": local["project_name"],
        "galaxy_id": local["galaxy_id"],
        "initial_node_id": local.get("initial_node_id"),
        "node_id": local.get("last_completed_node_id") or local.get("current_node_id"),
        "selected_node_id": best_round.get("selected_node_id"),
        "selected_round_index": best_round.get("selected_round_index"),
        "selected_archive_label": best_round.get("selected_archive_label"),
        "last_completed_round_index": local.get("last_completed_round_index"),
        "fit_round_count": result.get("fit_round_count"),
        "requested_rounds": result.get("requested_rounds"),
        "has_next_round": False,
        "final_artifacts": final_copy["final_artifacts"],
        "failed_final_artifacts": final_copy["failed_final_artifacts"],
        "message": (
            (result.get("final_messages") or [None])[-1]
            or result.get("error")
            or ("Fitting failed" if result.get("business_state") == "failed" else "")
        ),
    }
    response = await asyncio.to_thread(_post_json, local["platform_callback_url"], payload)
    _append_event(task_id, "local_platform.workflow_finished_callback_sent", {"request": payload, "response": response})


def _callback_url(task_id: str) -> str:
    return f"{SETTINGS.public_url}/api/workflows/{task_id}/fit-events"


def _workflow_file_for_request(request: dict[str, Any]) -> Path:
    return SETTINGS.workflow_galfits_file if request.get("data_type") == "multi_band" else SETTINGS.workflow_file


CLAUDE_MCP_READINESS_REQUIREMENT = """Before doing anything else, call WaitForMcpServers and wait until galaxy_morphology_mcp is connected.
Do not inspect data, edit files, or run commands before the MCP connection is ready.
If galaxy_morphology_mcp fails to connect, stop immediately and report the connection failure.
Never fall back to Bash, direct Python imports, or direct GALFIT/GalfitS execution."""


# modified by zl: use Claude's readiness tool only for Claude; Codex connects project MCP servers itself.
def _mcp_readiness_requirement() -> str:
    if SETTINGS.agent_backend == "claude":
        return CLAUDE_MCP_READINESS_REQUIREMENT
    return """galaxy_morphology_mcp is configured as a Codex MCP server for this task.
Call its MCP tools directly; no separate MCP readiness tool is required.
Never fall back to Bash, direct Python imports, or direct GALFIT/GalfitS execution."""


def _build_ai_prompt(task_id: str, request: dict[str, Any]) -> str:
    readiness_requirement = _mcp_readiness_requirement()
    round_policy = (
        "Agent-controlled (no platform-imposed round limit; stop when the scientific result is sufficient)"
        if int(request.get("max_rounds") or 0) == 0
        else str(request["max_rounds"])
    )
    if request.get("data_type") == "multi_band":
        fitting_mode = request.get("fitting_mode") or "Image Fitting"
        return f"""You are the execution agent for a multi-band galaxy decomposition task.

{readiness_requirement}

Strictly read and follow this workflow before taking action:
{SETTINGS.workflow_galfits_file}

Task id: {task_id}
Galaxy directory: {request['galaxy_dir']}
Initial GalfitS lyric file (absolute path): {request['lyric_file']}
Fitting mode: {fitting_mode}
Extra GalfitS args: {json.dumps(request.get('extra_args') or [])}
Maximum fitting rounds: {round_policy}
Task-specific fit-event delivery is configured by the service environment.
Scientific/user instructions: {request['instructions'] or 'None.'}

Requirements:
1. Use only the galaxy-morphology MCP tools for GalfitS execution. Never run GalfitS through a shell.
2. For image fitting, call run_galfits_image_fitting. For image-sed fitting, call run_galfits_image_sed_fitting. For sed fitting, call run_galfits_sed_fitting only if the required image fitting workplace is available.
3. Every fitting tool call must use an absolute config_file and the listed extra_args. Do not pass callback or communication parameters to MCP tools.
4. Follow workflow_galfits.md for analysis, model updates, round limits, and the final report.
5. If a decision requires unavailable information or human intervention, stop and explain that clearly in the final response.
6. In the final response, state status, final lyric/config, best output directory, report path, and selected components.
"""

    return f"""You are the execution agent for a single-band galaxy decomposition task.

{readiness_requirement}

Strictly read and follow this workflow before taking action:
{SETTINGS.workflow_file}

Task id: {task_id}
Galaxy directory: {request['galaxy_dir']}
Initial feedme (absolute path): {request['feedme']}
GALFIT options: {json.dumps(request['options'])}
Maximum fitting rounds: {round_policy}
Task-specific fit-event delivery is configured by the service environment.
Scientific/user instructions: {request['instructions'] or 'None.'}

Requirements:
1. Use only the galaxy-morphology MCP tools for GALFIT execution. Never run the galfit executable through a shell.
2. Use render_original and view_original_image before the iterative fitting stage when applicable.
3. Every run_galfit call must use an absolute config_file and the listed options. Do not pass callback or communication parameters to MCP tools.
4. Follow workflow_galfit.md for working_note.md updates, component analysis, physical checks, Fourier analysis, and the final report.
5. Only model Disk, Bulge, EdgeDisk, Bar, and Nucleus/AGN as permitted by the workflow.
6. If a decision requires unavailable information or human intervention, stop and explain that clearly in the final response.
7. In the final response, state status, final feedme, best archive, report path, and selected components.
"""


def _build_direct_prompt(task_id: str, request: dict[str, Any]) -> str:
    if request.get("data_type") == "multi_band":
        fitting_mode = str(request.get("fitting_mode") or "Image Fitting").strip().lower()
        tool_name = "run_galfits_image_fitting"
        extra = ""
        if fitting_mode == "sed fitting":
            tool_name = "run_galfits_sed_fitting"
            workplace = request.get("workplace") or ""
            extra = f"\nImage fitting workplace: {workplace}"
        elif fitting_mode in {"image-sed fitting", "image sed fitting", "imagesed fitting"}:
            tool_name = "run_galfits_image_sed_fitting"
        return f"""You are executing a human-requested one-round multi-band fitting task.

Task id: {task_id}
Galaxy directory: {request['galaxy_dir']}
GalfitS lyric file: {request['lyric_file']}
Fitting mode: {request.get('fitting_mode') or 'Image Fitting'}
Extra GalfitS args: {json.dumps(request.get('extra_args') or [])}
MCP tool to call exactly once: {tool_name}
Task-specific fit-event delivery is configured by the service environment.{extra}

Requirements:
1. Call the specified galaxy-morphology MCP fitting tool exactly once.
2. Use the absolute lyric path above as config_file and pass the listed extra_args. Do not pass callback or communication parameters.
3. Do not run component analysis, do not edit the lyric, and do not start another fitting round.
4. After the tool returns, summarize the status and output paths.
"""

    return f"""You are executing a human-requested one-round single-band fitting task.

Task id: {task_id}
Galaxy directory: {request['galaxy_dir']}
GALFIT feedme file: {request['feedme']}
GALFIT options: {json.dumps(request.get('options') or ['-o'])}
Task-specific fit-event delivery is configured by the service environment.

Requirements:
1. Call the galaxy-morphology MCP tool run_galfit exactly once.
2. Use the absolute feedme path above as config_file and the listed options. Do not pass callback or communication parameters.
3. Do not run component analysis, do not edit the feedme, and do not start another fitting round.
4. After the tool returns, summarize the status and output paths.
"""


def _build_prompt(task_id: str, request: dict[str, Any]) -> str:
    if request.get("fit_type") == "human":
        return _build_direct_prompt(task_id, request)
    return _build_ai_prompt(task_id, request)


# modified by zl: bind the current workflow's fit-event endpoint to the MCP environment.
def _mcp_command(task_id: str) -> str:
    event_url = shlex.quote(_callback_url(task_id))
    return SETTINGS.mcp_launch_command.format(
        mcp_root=shlex.quote(str(SETTINGS.mcp_root)),
        event_url=event_url,
        # Compatibility for operator-provided launch templates. New templates
        # should use event_url; these aliases are not MCP tool parameters.
        callback_url=event_url,
        service_url=shlex.quote(SETTINGS.public_url),
    )


def _request_model_settings(request: dict[str, Any]) -> dict[str, dict[str, str]]:
    token = str(request.get("model_settings_token") or "").strip()
    if not token:
        return {}
    key_path = Path(
        os.getenv("MODEL_SETTINGS_KEY_FILE", "/data/platform/model-settings.key")
    ).expanduser()
    try:
        key = key_path.read_bytes().strip()
        payload = Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
        parsed = json.loads(payload)
    except (OSError, InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Unable to decrypt per-user model settings") from exc
    return parsed if isinstance(parsed, dict) else {}


def _mcp_model_env(profile: dict[str, Any]) -> dict[str, str]:
    """Translate the platform's canonical MCP profile to tool environment variables."""
    if not profile:
        return {}
    api_format = str(profile.get("api_format") or "openai").lower()
    base_url = str(profile.get("base_url") or "").rstrip("/")
    api_key = str(profile.get("api_key") or "")
    model_name = str(profile.get("model_name") or "")
    if api_format == "vertex":
        access_token, credential = service_account_access_token(api_key)
        return {
            "OPENAI_API_KEY": access_token,
            "OPENAI_BASE_URL": vertex_openai_base_url(base_url, credential["project_id"]),
            "OPENAI_MODEL": vertex_model_name(model_name),
            "MODEL_API_FORMAT": "vertex",
            "GOOGLE_SERVICE_ACCOUNT_JSON": api_key,
            "VERTEX_API_ROOT": base_url,
        }
    if api_format == "gemini":
        # MCP VLM tools use the OpenAI SDK internally. Google's OpenAI
        # compatibility route preserves multimodal/history behavior while the
        # platform still validates the same key against Gemini native REST.
        for suffix in ("/v1beta/openai", "/v1beta"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)].rstrip("/")
                break
        compatible_url = f"{base_url}/v1beta/openai"
        return {
            "OPENAI_API_KEY": api_key,
            "OPENAI_BASE_URL": compatible_url,
            "OPENAI_MODEL": model_name,
            "MODEL_API_FORMAT": "gemini",
        }
    return {
        "OPENAI_API_KEY": api_key,
        "OPENAI_BASE_URL": base_url,
        "OPENAI_MODEL": model_name,
        "MODEL_API_FORMAT": "openai",
    }


# modified by zl: build the complete task-scoped environment required for
# conditional GALFIT/GalfitS tool registration and optional VLM analysis.
def _codex_mcp_env(task_id: str, request: dict[str, Any]) -> dict[str, str]:
    profiles = _request_model_settings(request)
    mcp_profile = profiles.get("mcp_analysis") or {}
    model_values = _mcp_model_env(mcp_profile)
    values = {
        "PYTHONPATH": os.getenv("PYTHONPATH", "src"),
        "MPLCONFIGDIR": os.getenv("MPLCONFIGDIR", "/tmp/matplotlib-galaxy-mcp"),
        "FIT_ROUND_EVENT_URL": _callback_url(task_id),
        "GALFIT_BIN": os.getenv("GALFIT_BIN", ""),
        "GALFITS_BIN": os.getenv("GALFITS_BIN", ""),
        "GS_DATA_PATH": os.getenv("GS_DATA_PATH", ""),
        "OPENAI_API_KEY": model_values.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": model_values.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL": model_values.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", ""),
        "MODEL_API_FORMAT": model_values.get("MODEL_API_FORMAT", ""),
        "GOOGLE_SERVICE_ACCOUNT_JSON": model_values.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        "VERTEX_API_ROOT": model_values.get("VERTEX_API_ROOT", ""),
        "ANALYSIS_MODE": os.getenv("ANALYSIS_MODE", ""),
        "MCP_ALLOWED_HOSTS": os.getenv("MCP_ALLOWED_HOSTS", ""),
    }
    if request.get("bizInfo") is not None:
        values["BIZ_INFO_JSON"] = json.dumps(request["bizInfo"], ensure_ascii=False)
    return {key: value for key, value in values.items() if value}


# modified by zl: Codex `-c` consumes TOML, so environment maps must be
# serialized as an inline TOML table instead of a JSON object string.
def _toml_inline_string_map(values: dict[str, str]) -> str:
    return "{ " + ", ".join(
        f"{key} = {json.dumps(value)}" for key, value in values.items()
    ) + " }"


def _codex_command(task_id: str, request: dict[str, Any]) -> list[str]:
    # Codex supports config overrides, so stdio MCP setup is fixed by the
    # service rather than inherited from the caller's Codex configuration.
    command = [
        SETTINGS.codex_bin,
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "-C",
        str(SETTINGS.mcp_root) if SETTINGS.codex_load_claude_project else request["galaxy_dir"],
        "--add-dir",
        request["galaxy_dir"] if SETTINGS.codex_load_claude_project else str(SETTINGS.mcp_root),
        "-c",
        # modified by zl: launch the stdio MCP directly with the configured
        # Python runtime. Codex can return ENOENT for a shell-based launcher
        # inside workspace-write even when /bin/bash exists in the container.
        "mcp_servers.galaxy_morphology_mcp.command="
        + json.dumps(SETTINGS.direct_fit_python),
        "-c",
        "mcp_servers.galaxy_morphology_mcp.args="
        + json.dumps(["-m", "mcp_server", "--transport", "stdio"]),
        "-c",
        "mcp_servers.galaxy_morphology_mcp.cwd=" + json.dumps(str(SETTINGS.mcp_root)),
        "-c",
        "mcp_servers.galaxy_morphology_mcp.env="
        + _toml_inline_string_map(_codex_mcp_env(task_id, request)),
        # `codex exec` has no interactive approver.  Explicitly authorize the
        # MCP tools used by workflow_galfit.md so a tool call cannot be turned
        # into a misleading "user cancelled" event.
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.render_original.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.view_original_image.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.run_galfit.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.run_galfits_image_fitting.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.run_galfits_sed_fitting.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.run_galfits_image_sed_fitting.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.galfits_analyze_by_vlm.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.analyze_multiband_components.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.component_analysis.approval_mode="approve"',
        "-c",
        'mcp_servers.galaxy_morphology_mcp.tools.fourier_mode_analysis.approval_mode="approve"',
    ]
    prompt = _build_prompt(task_id, request)
    if SETTINGS.codex_load_claude_project:
        # CLAUDE.md can serve as Codex's repository-instruction fallback.
        # Register Claude-format skills directly so their SKILL.md files and
        # linked references remain the single source of truth.
        command.extend(["-c", 'project_doc_fallback_filenames=["CLAUDE.md"]'])
        skill_paths = [
            str(skill_dir)
            for skill_dir in sorted((SETTINGS.mcp_root / ".claude/skills").glob("*"))
            if (skill_dir / "SKILL.md").is_file()
        ]
        if skill_paths:
            skill_configs_toml = ", ".join(
                f"{{ path = {json.dumps(path, ensure_ascii=False)}, enabled = true }}" for path in skill_paths
            )
            command.extend(["-c", f"skills.config=[{skill_configs_toml}]"])
        claude_agent_dir = SETTINGS.mcp_root / ".claude/agents"
        prompt += f"""

Codex compatibility instructions for this MCP project:
1. Treat the project CLAUDE.md as mandatory repository instructions.
2. Claude-format skills under {SETTINGS.mcp_root / '.claude/skills'} are registered as Codex skills.
   Read the relevant SKILL.md and only the referenced material needed for the current task.
3. Files under {claude_agent_dir} are role specifications, not automatically spawned Claude agents.
   Before locking or announcing a best/final fitting round, read
   {claude_agent_dir / 'best-round-verifier.md'} and perform its read-only audit exactly.
   Do not declare the round final if that audit returns FAIL.
"""
    command.append(prompt)
    return command


def _claude_mcp_config(task_id: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            "galaxy_morphology_mcp": {
                "type": "stdio",
                # modified by zl: keep Claude and Codex on the same
                # container-safe absolute stdio launcher path.
                "command": "/bin/bash",
                "args": ["-lc", _mcp_command(task_id)],
            }
        }
    }


def _claude_command(task_id: str, request: dict[str, Any]) -> list[str]:
    # Claude runs from the MCP project root so it automatically discovers
    # CLAUDE.md plus project-local .claude/agents and .claude/skills.
    command = [
        SETTINGS.claude_bin,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "user,project,local",
        "--add-dir",
        request["galaxy_dir"],
        "--mcp-config",
        json.dumps(_claude_mcp_config(task_id), ensure_ascii=False),
        "--strict-mcp-config",
        "--allowedTools",
        "WaitForMcpServers,Read,Write,Edit,Glob,Grep,Bash,mcp__galaxy_morphology_mcp__*",
        "--no-session-persistence",
    ]
    fit_profile = _request_model_settings(request).get("fit_agent") or {}
    selected_model = str(fit_profile.get("model_name") or SETTINGS.claude_model).strip()
    if selected_model:
        command.extend(["--model", selected_model])
    if SETTINGS.claude_max_budget_usd:
        command.extend(["--max-budget-usd", SETTINGS.claude_max_budget_usd])
    command.append(_build_prompt(task_id, request))
    return command


def _agent_command(task_id: str, request: dict[str, Any]) -> list[str]:
    if SETTINGS.agent_backend == "codex":
        return _codex_command(task_id, request)
    if SETTINGS.agent_backend == "claude":
        return _claude_command(task_id, request)
    raise ValueError(f"Unsupported AGENT_BACKEND: {SETTINGS.agent_backend!r}; expected 'codex' or 'claude'")


def _agent_cwd(request: dict[str, Any]) -> str:
    if SETTINGS.agent_backend == "claude" or (
        SETTINGS.agent_backend == "codex" and SETTINGS.codex_load_claude_project
    ):
        return str(SETTINGS.mcp_root)
    return request["galaxy_dir"]


# modified by zl: pass opaque per-task bizInfo to Agent/MCP subprocesses.
def _workflow_env(request: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    profiles = _request_model_settings(request)
    fit_profile = profiles.get("fit_agent") or {}
    mcp_profile = profiles.get("mcp_analysis") or {}
    if fit_profile.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = str(fit_profile["base_url"])
    if fit_profile.get("api_key"):
        env["ANTHROPIC_AUTH_TOKEN"] = str(fit_profile["api_key"])
        env["ANTHROPIC_API_KEY"] = str(fit_profile["api_key"])
    for key, value in _mcp_model_env(mcp_profile).items():
        if value:
            env[key] = value
    if request.get("bizInfo") is not None:
        biz_info_json = json.dumps(
            request["bizInfo"], ensure_ascii=False, separators=(",", ":")
        )
        env["BIZ_INFO_JSON"] = biz_info_json
        # modified by zl: attach task accounting metadata to every Claude request.
        spend_metadata = json.dumps(
            # HTTP header values must remain ASCII-safe.  The nested JSON
            # string still round-trips to the original Unicode instruction.
            {"bizInfo": biz_info_json}, ensure_ascii=True, separators=(",", ":")
        )
        task_header = f"x-litellm-spend-logs-metadata: {spend_metadata}"
        existing_headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
        env["ANTHROPIC_CUSTOM_HEADERS"] = (
            f"{existing_headers}\n{task_header}" if existing_headers else task_header
        )
    else:
        env.pop("BIZ_INFO_JSON", None)
    return env


def _direct_fit_command(task_id: str, request: dict[str, Any]) -> list[str]:
    data_type = request.get("data_type", "single_band")
    config_file = request["lyric_file"] if data_type == "multi_band" else request["feedme"]
    python_bin = SETTINGS.direct_galfits_python if data_type == "multi_band" else SETTINGS.direct_fit_python
    command = [
        python_bin,
        str(SETTINGS.service_root / "direct_fit_runner.py"),
        "--mcp-root",
        str(SETTINGS.mcp_root),
        "--data-type",
        data_type,
        "--fitting-mode",
        str(request.get("fitting_mode") or "Image Fitting"),
        "--config-file",
        config_file,
        "--options-json",
        json.dumps(request.get("options") or ["-o"]),
        "--extra-args-json",
        json.dumps(request.get("extra_args") or []),
    ]
    if request.get("workplace"):
        command.extend(["--workplace", str(request["workplace"])])
    return command


def _workflow_executor(request: dict[str, Any]) -> str:
    return "direct" if request.get("fit_type") == "human" else SETTINGS.agent_backend


# modified by zl: an agent request that finishes without any fitting round is a business failure.
def _workflow_business_state(
    return_code: int | None,
    fit_round_count: int,
    requested_rounds: int,
) -> str:
    if return_code != 0 or (requested_rounds > 0 and fit_round_count < 1):
        return "failed"
    if requested_rounds and fit_round_count >= requested_rounds:
        return "completed"
    return "complete_less_than_max_round"


def _workflow_command(task_id: str, request: dict[str, Any]) -> list[str]:
    if _workflow_executor(request) == "direct":
        return _direct_fit_command(task_id, request)
    return _agent_command(task_id, request)


def _workflow_cwd(request: dict[str, Any]) -> str:
    if _workflow_executor(request) == "direct":
        return request["galaxy_dir"]
    return _agent_cwd(request)


async def _record_codex_stream(task_id: str, stream: asyncio.StreamReader, stream_name: str) -> list[str]:
    last_messages: list[str] = []
    while True:
        raw = await stream.readline()
        if not raw:
            break
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            payload = json.loads(text)
            event_type = f"codex.{payload.get('type', 'event')}"
            if payload.get("type") in {"item.completed", "item.failed"}:
                item = payload.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    last_messages.append(str(item["text"]))
                    last_messages[:] = last_messages[-5:]
        except json.JSONDecodeError:
            payload = {"text": text}
            event_type = f"codex.{stream_name}"
        _append_event(task_id, event_type, payload)
    return last_messages


async def _record_claude_stream(task_id: str, stream: asyncio.StreamReader, stream_name: str) -> list[str]:
    last_messages: list[str] = []
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            payload = json.loads(line)
            if payload.get("type") == "system" and payload.get("subtype") == "thinking_tokens":
                continue
            event_type = f"claude.{payload.get('type', 'event')}"
            if payload.get("type") == "assistant":
                content = (payload.get("message") or {}).get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        last_messages.append(str(block["text"]))
            elif payload.get("type") == "result" and payload.get("result"):
                last_messages.append(str(payload["result"]))
            last_messages[:] = last_messages[-5:]
        except json.JSONDecodeError:
            payload = {"text": line}
            event_type = f"claude.{stream_name}"
        _append_event(task_id, event_type, payload)
    return last_messages


async def _record_direct_fit_stream(
    task_id: str, stream: asyncio.StreamReader, stream_name: str
) -> list[str]:
    last_messages: list[str] = []
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            payload = json.loads(line)
            event_type = str(payload.get("type") or "direct_fit.event")
            if event_type == "direct_fit.result":
                result = payload.get("result") or {}
                last_messages.append(json.dumps(result, ensure_ascii=False, default=str))
            elif event_type == "direct_fit.failed":
                last_messages.append(str(payload.get("error") or "Direct fitting failed"))
            last_messages[:] = last_messages[-5:]
        except json.JSONDecodeError:
            payload = {"text": line}
            event_type = f"direct_fit.{stream_name}"
        _append_event(task_id, event_type, payload)
    return last_messages


async def _record_workflow_stream(
    task_id: str, stream: asyncio.StreamReader, stream_name: str, executor: str
) -> list[str]:
    if executor == "direct":
        return await _record_direct_fit_stream(task_id, stream, stream_name)
    if executor == "claude":
        return await _record_claude_stream(task_id, stream, stream_name)
    return await _record_codex_stream(task_id, stream, stream_name)


async def _finish_workflow_streams(
    task_id: str,
    stdout_task: asyncio.Task[list[str]],
    stderr_task: asyncio.Task[list[str]],
) -> tuple[list[str], list[str]]:
    try:
        return await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=SETTINGS.stream_drain_timeout_sec,
        )
    except asyncio.TimeoutError:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        _append_event(
            task_id,
            "workflow.stream_drain_timeout",
            {"timeout_sec": SETTINGS.stream_drain_timeout_sec},
        )
        return [], []


async def _run_workflow(task_id: str, request: dict[str, Any]) -> None:
    galaxy_dir = request["galaxy_dir"]
    lock = GALAXY_LOCKS.setdefault(galaxy_dir, asyncio.Lock())
    async with WORKER_SEMAPHORE, lock:
        async def fail_before_start(message: str) -> None:
            result = {
                "error": message,
                "exit_code": None,
                "fit_round_count": _count_events(task_id, "fit_round_finished"),
                "requested_rounds": int(request.get("max_rounds") or 0),
                "business_state": "failed",
            }
            _set_task(task_id, "failed", result, finished_at=now(), pid=None)
            _append_event(task_id, "workflow.failed", result)
            try:
                await _callback_local_platform_workflow_finished(task_id, request, result)
            except Exception:
                LOGGER.exception("failed to send pre-start failure callback: task_id=%s", task_id)

        task = _task_row(task_id)
        if task is None or task["state"] == "cancelled":
            return
        workflow_file = _workflow_file_for_request(request)
        if request.get("fit_type") == "human":
            workflow_file = SETTINGS.mcp_root / "src/mcp_server.py"
        if not workflow_file.is_file() or not SETTINGS.mcp_root.is_dir():
            message = "Fixed galaxy morphology MCP configuration is invalid"
            await fail_before_start(message)
            return
        try:
            command = _workflow_command(task_id, request)
        except ValueError as exc:
            await fail_before_start(str(exc))
            return
        executor = _workflow_executor(request)
        workflow_cwd = _workflow_cwd(request)
        _set_task(task_id, "starting", started_at=now())
        _append_event(
            task_id,
            "workflow.starting",
            {
                "executor": executor,
                "agent_backend": SETTINGS.agent_backend if executor != "direct" else None,
                "command": command[:-1] if executor != "direct" else command,
                "cwd": workflow_cwd,
                "galaxy_dir": galaxy_dir,
            },
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workflow_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SETTINGS.agent_stream_limit_bytes,
                env=_workflow_env(request),
            )
        except OSError as exc:
            await fail_before_start(str(exc))
            return
        RUNNING_PROCESSES[task_id] = process
        _set_task(task_id, "running", pid=process.pid)
        _append_event(task_id, "workflow.started", {"pid": process.pid})
        stdout_task = asyncio.create_task(
            _record_workflow_stream(task_id, process.stdout, "stdout", executor)
        )
        stderr_task = asyncio.create_task(
            _record_workflow_stream(task_id, process.stderr, "stderr", executor)
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=SETTINGS.agent_timeout_sec)
            messages, _ = await _finish_workflow_streams(task_id, stdout_task, stderr_task)
            task = _task_row(task_id)
            if task and task["state"] == "cancelled":
                _append_event(task_id, "workflow.cancelled", {"pid": process.pid})
                return
            if executor == "direct" and process.returncode == 0 and messages:
                try:
                    direct_result = json.loads(messages[-1])
                except json.JSONDecodeError:
                    direct_result = {}
                if direct_result.get("status") == "success":
                    round_status_file = direct_result.get("round_status_file")
                    archive_dir = str(Path(round_status_file).parent) if round_status_file else ""
                    raw_outputs = direct_result.get("outputs") or []
                    if isinstance(raw_outputs, dict):
                        raw_outputs = list(raw_outputs.values())
                    artifacts = [
                        str(path)
                        for path in (
                            list(raw_outputs)
                            + [
                                direct_result.get("input_param_file"),
                                direct_result.get("output_param_file"),
                                direct_result.get("optimized_fits_file"),
                                direct_result.get("image_file"),
                                direct_result.get("summary_file"),
                                direct_result.get("console_log_file"),
                                round_status_file,
                            ]
                        )
                        if path
                    ]
                    await _record_task_fit_event(
                        task_id,
                        {
                            "event": "fit_round_finished",
                            "fitter": "galfits" if request.get("data_type") == "multi_band" else "galfit",
                            "status": "success",
                            "round_id": Path(archive_dir).name if archive_dir else f"{task_id}-direct",
                            "archive_dir": archive_dir,
                            "round_status_file": round_status_file or "",
                            "artifacts": list(dict.fromkeys(artifacts)),
                            "fit_statistics": direct_result.get("fit_statistics") or {},
                        },
                    )
            fit_round_count = _count_events(task_id, "fit_round_finished")
            requested_rounds = int(request.get("max_rounds") or 0)
            business_state = _workflow_business_state(
                process.returncode,
                fit_round_count,
                requested_rounds,
            )
            missing_fit_round = requested_rounds > 0 and fit_round_count < 1
            result = {
                "exit_code": process.returncode,
                "final_messages": messages,
                "fit_round_count": fit_round_count,
                "requested_rounds": requested_rounds,
                "business_state": business_state,
            }
            if missing_fit_round:
                result["error"] = (
                    f"{executor.capitalize()} completed without a successful "
                    "fit_round_finished event"
                )
            state = "failed" if business_state == "failed" else "succeeded"
            _set_task(task_id, state, result, finished_at=now(), pid=None)
            _append_event(task_id, f"workflow.{state}", result)
            latest_task = _task_row(task_id)
            await _callback_local_platform_workflow_finished(task_id, latest_task["request"] if latest_task else request, result)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()
            await _finish_workflow_streams(task_id, stdout_task, stderr_task)
            result = {
                "error": (
                    f"{executor.capitalize()} timed out "
                    f"after {SETTINGS.agent_timeout_sec} seconds"
                ),
                "exit_code": None,
                "fit_round_count": _count_events(task_id, "fit_round_finished"),
                "requested_rounds": int(request.get("max_rounds") or 0),
                "business_state": "failed",
            }
            _set_task(task_id, "failed", result, finished_at=now(), pid=None)
            _append_event(task_id, "workflow.failed", result)
            latest_task = _task_row(task_id)
            await _callback_local_platform_workflow_finished(task_id, latest_task["request"] if latest_task else request, result)
        except Exception as exc:
            LOGGER.exception("workflow task failed unexpectedly: task_id=%s", task_id)
            latest_task = _task_row(task_id)
            if latest_task and latest_task["state"] == "cancelled":
                _append_event(
                    task_id,
                    "workflow.cancelled",
                    {"pid": process.pid, "cleanup_error": str(exc)},
                )
            else:
                result = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "exit_code": process.returncode,
                    "fit_round_count": _count_events(task_id, "fit_round_finished"),
                    "requested_rounds": int(request.get("max_rounds") or 0),
                    "business_state": "failed",
                }
                _set_task(task_id, "failed", result, finished_at=now(), pid=None)
                _append_event(task_id, "workflow.failed", result)
                latest_task = _task_row(task_id)
                await _callback_local_platform_workflow_finished(
                    task_id,
                    latest_task["request"] if latest_task else request,
                    result,
                )
        finally:
            RUNNING_PROCESSES.pop(task_id, None)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_deployment()
    init_database()
    recover_interrupted_tasks()
    await asyncio.to_thread(cleanup_staging_once)
    resumed_tasks = resume_queued_tasks()
    cleanup_task = asyncio.create_task(_stage_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        for task in resumed_tasks:
            task.cancel()
        if resumed_tasks:
            await asyncio.gather(*resumed_tasks, return_exceptions=True)


app = FastAPI(title="Agent Communication Service", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    started = datetime.now(timezone.utc)
    raw_body = await request.body()
    request_payload = {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "client": request.client.host if request.client else None,
        "body": _decode_body_for_log(raw_body),
    }
    LOGGER.info("http.request %s", json.dumps(request_payload, ensure_ascii=False, default=str))
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        LOGGER.exception(
            "http.response_error %s",
            json.dumps({**request_payload, "error": str(exc), "elapsed_ms": elapsed_ms}, ensure_ascii=False, default=str),
        )
        raise
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    LOGGER.info(
        "http.response %s",
        json.dumps(
            {
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
            ensure_ascii=False,
        ),
    )
    return response


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "agent_backend": SETTINGS.agent_backend,
        "agent_bin": SETTINGS.claude_bin if SETTINGS.agent_backend == "claude" else SETTINGS.codex_bin,
        "database": str(SETTINGS.database_file),
        "mcp_transport": "stdio",
        "mcp_root": str(SETTINGS.mcp_root),
        "log_file": str(SETTINGS.log_file),
        "stage_root": str(SETTINGS.local_stage_root),
        "stage_cleanup": {
            "enabled": SETTINGS.stage_cleanup_enabled,
            "retention_days": SETTINGS.stage_retention_days,
            "interval_hours": SETTINGS.stage_cleanup_interval_hours,
        },
    }


# Legacy hand-off endpoints used by request_agent/wait_for_agent_response.
@app.post("/api/fit-events")
async def receive_fit_event(body: FitRoundEventIn) -> dict[str, Any]:
    event = body.model_dump()
    event["received_at"] = now()
    FIT_EVENTS.append(event)
    return {"status": "success", "event_id": len(FIT_EVENTS) - 1}


@app.get("/api/fit-events")
async def list_fit_events(after: int = 0) -> dict[str, Any]:
    after = max(0, after)
    return {"status": "success", "next_offset": len(FIT_EVENTS), "events": FIT_EVENTS[after:]}


@app.post("/api/agent/requests")
async def create_request(body: AgentRequestIn) -> dict[str, Any]:
    request_id = body.request_id or uuid4().hex
    async with CONDITION:
        existing = REQUESTS.get(request_id)
        if existing:
            if existing["request"] != body.request:
                raise HTTPException(status_code=409, detail="request_id already exists with different content")
            return {"status": "success", "request_id": request_id, "state": existing["state"]}
        REQUESTS[request_id] = {"request_id": request_id, "request": body.request, "state": "pending", "created_at": now(), "updated_at": now()}
        CONDITION.notify_all()
    return {"status": "success", "request_id": request_id, "state": "pending"}


@app.get("/api/agent/requests/next")
async def claim_next_request(wait_sec: int = 30) -> dict[str, Any]:
    wait_sec = max(0, min(wait_sec, 60))
    async with CONDITION:
        def next_pending() -> dict[str, Any] | None:
            return next((item for item in REQUESTS.values() if item["state"] == "pending"), None)
        item = next_pending()
        if item is None and wait_sec:
            try:
                await asyncio.wait_for(CONDITION.wait_for(lambda: next_pending() is not None), wait_sec)
            except asyncio.TimeoutError:
                return {"status": "empty"}
            item = next_pending()
        if item is None:
            return {"status": "empty"}
        item["state"] = "running"
        item["claimed_at"] = item["updated_at"] = now()
        return {"status": "success", "request": item}


@app.post("/api/agent/requests/{request_id}/response")
async def complete_request(request_id: str, body: AgentResponseIn) -> dict[str, Any]:
    async with CONDITION:
        item = REQUESTS.get(request_id)
        if item is None:
            raise HTTPException(status_code=404, detail="request not found")
        if item["state"] in {"success", "failure", "needs_action"}:
            raise HTTPException(status_code=409, detail="request already completed")
        item["state"], item["result"], item["completed_at"], item["updated_at"] = body.status, body.result, now(), now()
        CONDITION.notify_all()
        return {"status": "success", "request_id": request_id, "state": item["state"]}


@app.get("/api/agent/requests/{request_id}")
async def get_request(request_id: str) -> dict[str, Any]:
    item = REQUESTS.get(request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="request not found")
    return {"status": "success", "request": item}


@app.get("/api/agent/requests/{request_id}/wait")
async def wait_for_request(request_id: str, timeout_sec: int = 300) -> dict[str, Any]:
    timeout_sec = max(1, min(timeout_sec, 600))
    async with CONDITION:
        item = REQUESTS.get(request_id)
        if item is None:
            raise HTTPException(status_code=404, detail="request not found")
        terminal = {"success", "failure", "needs_action"}
        if item["state"] not in terminal:
            try:
                await asyncio.wait_for(CONDITION.wait_for(lambda: REQUESTS[request_id]["state"] in terminal), timeout_sec)
            except asyncio.TimeoutError:
                return {"status": "timeout", "request_id": request_id, "state": REQUESTS[request_id]["state"]}
        return {"status": "success", "request_id": request_id, "state": item["state"], "result": item.get("result", {})}


@app.post("/api/galfit-workflows")
async def submit_galfit_workflow(body: GalfitWorkflowIn) -> dict[str, Any]:
    request = _validate_workflow_request(body)
    if body.idempotency_key:
        with _connect() as conn:
            row = conn.execute("SELECT task_id, state FROM galfit_tasks WHERE idempotency_key = ?", (body.idempotency_key,)).fetchone()
        if row:
            return {"status": "success", "task_id": row["task_id"], "state": row["state"], "reused": True}
    task_id = uuid4().hex
    created_at = now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO galfit_tasks(task_id, idempotency_key, state, request_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, body.idempotency_key, "queued", json.dumps(request, ensure_ascii=False), created_at, created_at),
        )
    _append_event(task_id, "workflow.queued", {"request": request})
    asyncio.create_task(_run_workflow(task_id, request))
    return {"status": "success", "task_id": task_id, "state": "queued", "events_url": f"/api/galfit-workflows/{task_id}/events"}


@app.post("/api/local-fit-requests")
async def submit_local_fit_request(body: LocalFitRequestIn) -> dict[str, Any]:
    idempotency_key = body.idempotency_key
    if idempotency_key:
        with _connect() as conn:
            row = conn.execute("SELECT task_id, state FROM galfit_tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if row and row["state"] not in {"failed", "cancelled"}:
            if body.legacy_human_request:
                return {"status": "success", "task_id": row["task_id"]}
            return {"status": "success", "task_id": row["task_id"], "state": row["state"], "reused": True}
    request = _stage_local_fit_request(body)
    task_id = uuid4().hex
    created_at = now()
    with _connect() as conn:
        if idempotency_key:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_id, state FROM galfit_tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row and row["state"] not in {"failed", "cancelled"}:
                if body.legacy_human_request:
                    return {"status": "success", "task_id": row["task_id"]}
                return {"status": "success", "task_id": row["task_id"], "state": row["state"], "reused": True}
            if row:
                archived_key = f"{idempotency_key}:archived:{row['task_id']}"
                conn.execute("UPDATE galfit_tasks SET idempotency_key = ? WHERE task_id = ?", (archived_key, row["task_id"]))
        conn.execute(
            "INSERT INTO galfit_tasks(task_id, idempotency_key, state, request_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, idempotency_key, "queued", json.dumps(request, ensure_ascii=False), created_at, created_at),
        )
    _append_event(
        task_id,
        "local_platform.staged",
        {
            "config_file": request.get("feedme") or request.get("lyric_file"),
            "feedme": request.get("feedme"),
            "lyric_file": request.get("lyric_file"),
            "galaxy_dir": request["galaxy_dir"],
            "local_platform": request["local_platform"],
        },
    )
    _append_event(task_id, "workflow.queued", {"request": request})
    asyncio.create_task(_run_workflow(task_id, request))
    if body.legacy_human_request:
        return {"status": "success", "task_id": task_id}
    response = {
        "status": "success",
        "task_id": task_id,
        "state": "queued",
        "staged_config_file": request.get("feedme") or request.get("lyric_file"),
        "staged_galaxy_dir": request["galaxy_dir"],
        "events_url": f"/api/workflows/{task_id}/events",
        "stream_url": f"/api/workflows/{task_id}/events/stream",
    }
    if request.get("feedme"):
        response["staged_feedme"] = request["feedme"]
    if request.get("lyric_file"):
        response["staged_lyric_file"] = request["lyric_file"]
    return response


@app.get("/api/workflows/{task_id}")
@app.get("/api/galfit-workflows/{task_id}")
async def get_galfit_workflow(task_id: str) -> dict[str, Any]:
    task = _task_row(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"status": "success", "task": task}


@app.get("/api/workflows/{task_id}/events")
@app.get("/api/galfit-workflows/{task_id}/events")
async def get_galfit_workflow_events(task_id: str, after: int = 0) -> dict[str, Any]:
    if _task_row(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    events = _events_after(task_id, after)
    return {"status": "success", "task_id": task_id, "next_offset": events[-1]["sequence"] if events else after, "events": events}


@app.get("/api/workflows/{task_id}/events/stream")
@app.get("/api/galfit-workflows/{task_id}/events/stream")
async def stream_galfit_workflow_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
    if _task_row(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    async def generate() -> AsyncIterator[str]:
        cursor = after
        while True:
            if await request.is_disconnected():
                return
            events = _events_after(task_id, cursor)
            for event in events:
                cursor = event["sequence"]
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            task = _task_row(task_id)
            if task and task["state"] in {"succeeded", "failed", "cancelled"} and not events:
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/workflows/{task_id}/fit-events")
@app.post("/api/galfit-workflows/{task_id}/fit-events")
async def receive_task_fit_event(task_id: str, body: FitRoundEventIn) -> dict[str, Any]:
    return await _record_task_fit_event(task_id, body.model_dump())


async def _record_task_fit_event(task_id: str, event: dict[str, Any]) -> dict[str, Any]:
    task = _task_row(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    round_index = _count_events(task_id, "fit_round_finished") + 1
    event = FitRoundEventIn.model_validate(event).model_dump()
    event["round_index"] = round_index
    sequence = _append_event(task_id, "fit_round_finished", event)
    if task["request"].get("local_platform"):
        local = task["request"]["local_platform"]
        if local.get("legacy_human_request"):
            await _ensure_platform_node_for_round(task_id, task["request"], round_index)
            _copy_round_artifacts_to_platform(task_id, task["request"], event, round_index)
        else:
            await _callback_local_platform(task_id, task["request"], event, round_index)
    return {"status": "success", "task_id": task_id, "sequence": sequence}


@app.post("/api/workflows/{task_id}/cancel")
@app.post("/api/galfit-workflows/{task_id}/cancel")
async def cancel_galfit_workflow(task_id: str) -> dict[str, Any]:
    task = _task_row(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["state"] in {"succeeded", "failed", "cancelled"}:
        return {"status": "success", "task_id": task_id, "state": task["state"]}
    _set_task(
        task_id,
        "cancelled",
        {"message": "cancelled by client"},
        finished_at=now(),
        pid=None,
    )
    process = RUNNING_PROCESSES.get(task_id)
    if process and process.returncode is None:
        process.terminate()
    _append_event(task_id, "workflow.cancellation_requested", {})
    return {"status": "success", "task_id": task_id, "state": "cancelled"}
