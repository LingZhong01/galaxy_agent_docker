#!/usr/bin/env python3
"""Run exactly one GALFIT/GalfitS call without starting an AI agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-root", required=True)
    parser.add_argument("--data-type", choices=("single_band", "multi_band"), required=True)
    parser.add_argument("--fitting-mode", default="Image Fitting")
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--callback-url")
    parser.add_argument("--options-json", default='["-o"]')
    parser.add_argument("--extra-args-json", default="[]")
    parser.add_argument("--workplace")
    return parser.parse_args()


def _emit(event_type: str, **payload: Any) -> None:
    print(json.dumps({"type": event_type, **payload}, ensure_ascii=False, default=str), flush=True)


def _load_env_file(path: Path) -> None:
    """Load MCP settings even when the GalfitS env lacks python-dotenv."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    else:
        load_dotenv(path)


_LYRIC_DATA_PATH_RE = re.compile(
    r"^(\s*I[A-Za-z](?:1|3|4|6)\)\s*\[\s*)([^,\]]+)(.*)$"
)


def _absolutize_multiband_paths(config_file: str) -> None:
    """Keep staged lyric paths valid after GalfitS copies it into output/."""
    path = Path(config_file).resolve()
    config_dir = path.parent
    output: list[str] = []
    changed = False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(newline)] if newline else line
        match = _LYRIC_DATA_PATH_RE.match(body)
        if not match:
            output.append(line)
            continue
        prefix, raw_value, suffix = match.groups()
        value = raw_value.strip().strip('"').strip("'")
        if not value or os.path.isabs(value):
            output.append(line)
            continue
        output.append(f"{prefix}{(config_dir / value).resolve()}{suffix}{newline}")
        changed = True
    if changed:
        path.write_text("".join(output), encoding="utf-8")


# modified by zl: inject event delivery through the environment, not fitting tool parameters.
async def _run(args: argparse.Namespace) -> dict[str, Any]:
    mcp_root = Path(args.mcp_root).resolve()
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-galaxy-mcp")
    sys.path.insert(0, str(mcp_root / "src"))

    _load_env_file(mcp_root / ".env")
    if args.callback_url:
        os.environ["FIT_ROUND_EVENT_URL"] = args.callback_url
    if args.data_type == "single_band":
        from tools.run_galfit import run_galfit

        return await run_galfit(
            config_file=args.config_file,
            options=json.loads(args.options_json),
        )

    _absolutize_multiband_paths(args.config_file)

    from tools.run_galfits import (
        run_galfits_image_fitting,
        run_galfits_image_sed_fitting,
        run_galfits_sed_fitting,
    )

    extra_args = json.loads(args.extra_args_json)
    mode = args.fitting_mode.strip().lower().replace("_", " ").replace("-", " ")
    if mode == "image fitting":
        return await run_galfits_image_fitting(
            config_file=args.config_file,
            extra_args=extra_args,
        )
    if mode == "sed fitting":
        if not args.workplace:
            raise ValueError("workplace is required for multi-band SED Fitting")
        return await run_galfits_sed_fitting(
            config_file=args.config_file,
            image_fitting_workplace=args.workplace,
            extra_args=extra_args,
        )
    if mode in {"image sed fitting", "imagesed fitting"}:
        return await run_galfits_image_sed_fitting(
            config_file=args.config_file,
            extra_args=extra_args,
        )
    raise ValueError(f"Unsupported multi-band fitting_mode: {args.fitting_mode!r}")


def main() -> int:
    args = _arguments()
    _emit(
        "direct_fit.started",
        data_type=args.data_type,
        fitting_mode=args.fitting_mode,
        config_file=args.config_file,
    )
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        _emit("direct_fit.failed", error=str(exc), traceback=traceback.format_exc())
        return 1
    _emit("direct_fit.result", result=result)
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
