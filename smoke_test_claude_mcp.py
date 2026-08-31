"""Minimal live check that Claude can connect to and call the morphology MCP."""

from __future__ import annotations

import json
import subprocess
import sys

import main


TASK_ID = "claude-mcp-smoke"
FITS_FILE = (
    "/mnt/data/galaxy_decomposition_evaluation/code/agent/test_data/test_oss/"
    "single_band_test/cosmos_38/3801/input/cosmos_38_drz.fits"
)
TARGET_TOOL = "mcp__galaxy_morphology_mcp__re_arcsec2pix"


# modified by zl: exercise the same Claude stdio MCP configuration without running a fit.
def run_smoke_test() -> int:
    prompt = f"""This is a read-only MCP connectivity smoke test.
First call WaitForMcpServers for galaxy_morphology_mcp.
If it connects, call {TARGET_TOOL} exactly once with re_as=0.4 and
fits_file_list=[{json.dumps(FITS_FILE)}]. Do not use Bash, Read, or any other tool.
After the tool result, answer exactly MCP_SMOKE_PASS. If connection or the tool call
fails, answer MCP_SMOKE_FAIL followed by the error.
"""
    command = [
        main.SETTINGS.claude_bin,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--mcp-config",
        json.dumps(main._claude_mcp_config(TASK_ID), ensure_ascii=False),
        "--strict-mcp-config",
        "--allowedTools",
        f"WaitForMcpServers,{TARGET_TOOL}",
        "--no-session-persistence",
        prompt,
    ]
    process = subprocess.Popen(
        command,
        cwd=main.SETTINGS.mcp_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=main.os.environ.copy(),
    )
    saw_connected = False
    saw_tool_call = False
    saw_tool_result = False
    wait_tool_ids: set[str] = set()
    target_tool_ids: set[str] = set()
    assert process.stdout is not None
    for line in process.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            print(json.dumps({"mcp_servers": event.get("mcp_servers")}, ensure_ascii=False))
        for content in event.get("message", {}).get("content", []):
            content_type = content.get("type")
            if content_type == "tool_use":
                tool_name = content.get("name")
                tool_id = content.get("id")
                if tool_name == "WaitForMcpServers" and tool_id:
                    wait_tool_ids.add(tool_id)
                    print(json.dumps({"wait_call": content}, ensure_ascii=False))
                elif tool_name == TARGET_TOOL and tool_id:
                    target_tool_ids.add(tool_id)
                    saw_tool_call = True
                    print(json.dumps({"mcp_tool_call": content}, ensure_ascii=False))
            elif content_type == "tool_result":
                tool_id = content.get("tool_use_id")
                if tool_id in wait_tool_ids:
                    result_text = json.dumps(content, ensure_ascii=False)
                    saw_connected = (
                        not content.get("is_error", False)
                        and ('"ready": true' in result_text or "ready: true" in result_text)
                        and "galaxy_morphology_mcp" in result_text
                    )
                    print(json.dumps({"wait_result": content}, ensure_ascii=False))
                elif tool_id in target_tool_ids:
                    saw_tool_result = not content.get("is_error", False)
                    print(json.dumps({"mcp_tool_result": content}, ensure_ascii=False))
        if event_type == "result":
            print(json.dumps({
                "claude_result": event.get("result"),
                "is_error": event.get("is_error"),
                "subtype": event.get("subtype"),
            }, ensure_ascii=False))
    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if stderr:
        print(stderr, file=sys.stderr, end="")
    passed = return_code == 0 and saw_connected and saw_tool_call and saw_tool_result
    print(json.dumps({
        "passed": passed,
        "return_code": return_code,
        "saw_connected": saw_connected,
        "saw_tool_call": saw_tool_call,
        "saw_tool_result": saw_tool_result,
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run_smoke_test())
