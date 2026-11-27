import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import direct_fit_runner
import main
from fastapi import HTTPException


class AgentBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = {
            "data_type": "single_band",
            "fit_type": "human",
            "galaxy_dir": "/tmp/example/input",
            "feedme": "/tmp/example/input/example.feedme",
            "options": ["-o"],
            "max_rounds": 1,
            "instructions": "",
        }

    def test_codex_backend_command(self) -> None:
        with (
            patch.object(main.SETTINGS, "agent_backend", "codex"),
            patch.object(main.SETTINGS, "codex_load_claude_project", True),
        ):
            command = main._agent_command("task-1", self.request)
            self.assertEqual(command[0], main.SETTINGS.codex_bin)
            self.assertEqual(command[1], "exec")
            self.assertEqual(command[command.index("-C") + 1], str(main.SETTINGS.mcp_root))
            self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', command)
            self.assertTrue(any(value.startswith("skills.config=") for value in command))
            self.assertIn("best-round-verifier.md", command[-1])
            self.assertEqual(main._agent_cwd(self.request), str(main.SETTINGS.mcp_root))

    def test_backend_biz_info_alias_is_preserved(self) -> None:
        biz_info = {
            "env": "sg",
            "bizType": "a1",
            "bizId": "session_88",
            "userId": "user_123",
            "randomId": "fdsafd",
            "instruction": "自动完成 bulge + disk 拟合",
        }
        body = main.LocalFitRequestIn.model_validate(
            {
                "owner": "owner",
                "project_name": "project",
                "galaxy_id": "galaxy",
                "node_id": "node",
                "bizInfo": biz_info,
                "current_metadata": {"input_dir": "/tmp/input", "output_dir": "/tmp/output"},
            }
        )
        self.assertEqual(body.biz_info, biz_info)
        request = {**self.request, "bizInfo": biz_info}
        workflow_env = main._workflow_env(request)
        self.assertEqual(json.loads(workflow_env["BIZ_INFO_JSON"]), biz_info)
        header_name, header_value = workflow_env["ANTHROPIC_CUSTOM_HEADERS"].split(": ", 1)
        self.assertEqual(header_name, "x-litellm-spend-logs-metadata")
        self.assertTrue(header_value.isascii())
        self.assertEqual(json.loads(json.loads(header_value)["bizInfo"]), biz_info)
        self.assertEqual(
            json.loads(main._codex_mcp_env("task-biz-info", request)["BIZ_INFO_JSON"]),
            biz_info,
        )

    def test_biz_info_header_preserves_existing_claude_headers(self) -> None:
        request = {**self.request, "bizInfo": {"bizId": "task-1"}}
        with patch.dict(
            main.os.environ,
            {"ANTHROPIC_CUSTOM_HEADERS": "x-existing-header: existing-value"},
        ):
            custom_headers = main._workflow_env(request)["ANTHROPIC_CUSTOM_HEADERS"]
        self.assertTrue(custom_headers.startswith("x-existing-header: existing-value\n"))
        self.assertIn("x-litellm-spend-logs-metadata: ", custom_headers)

    def test_mcp_model_env_maps_openai_profile_directly(self) -> None:
        env = main._mcp_model_env(
            {
                "api_format": "openai",
                "base_url": "https://gateway.example/v1",
                "api_key": "secret",
                "model_name": "vision-model",
            }
        )
        self.assertEqual(env["OPENAI_BASE_URL"], "https://gateway.example/v1")
        self.assertEqual(env["OPENAI_API_KEY"], "secret")
        self.assertEqual(env["OPENAI_MODEL"], "vision-model")
        self.assertEqual(env["MODEL_API_FORMAT"], "openai")

    def test_mcp_model_env_normalizes_gemini_native_profile(self) -> None:
        for base_url in (
            "https://generativelanguage.googleapis.com",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ):
            with self.subTest(base_url=base_url):
                env = main._mcp_model_env(
                    {
                        "api_format": "gemini",
                        "base_url": base_url,
                        "api_key": "gemini-secret",
                        "model_name": "gemini-test",
                    }
                )
                self.assertEqual(
                    env["OPENAI_BASE_URL"],
                    "https://generativelanguage.googleapis.com/v1beta/openai",
                )
                self.assertEqual(env["MODEL_API_FORMAT"], "gemini")

    def test_mcp_model_env_maps_vertex_service_account(self) -> None:
        credential = {
            "project_id": "astro-project",
            "client_email": "agent@astro-project.iam.gserviceaccount.com",
        }
        with patch.object(
            main,
            "service_account_access_token",
            return_value=("short-lived-token", credential),
        ):
            env = main._mcp_model_env(
                {
                    "api_format": "vertex",
                    "base_url": "https://us-central1-aiplatform.googleapis.com",
                    "api_key": "{encrypted service account json}",
                    "model_name": "gemini-2.5-flash",
                }
            )
        self.assertEqual(env["OPENAI_API_KEY"], "short-lived-token")
        self.assertEqual(env["OPENAI_MODEL"], "google/gemini-2.5-flash")
        self.assertEqual(env["MODEL_API_FORMAT"], "vertex")
        self.assertEqual(
            env["OPENAI_BASE_URL"],
            "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/astro-project/locations/us-central1/endpoints/openapi",
        )

    # modified by zl: verify transport configuration stays outside MCP fitting APIs.
    def test_agent_prompt_keeps_transport_out_of_mcp_tool_arguments(self) -> None:
        prompt = main._build_ai_prompt("task-transport", {**self.request, "fit_type": "ai"})
        self.assertIn("fit-event delivery is configured by the service environment", prompt)
        self.assertIn("Do not pass callback or communication parameters", prompt)
        self.assertNotIn("callback_url exactly as supplied", prompt)

    # modified by zl: verify readiness instructions match each agent's MCP lifecycle.
    def test_ai_prompts_use_backend_specific_mcp_readiness(self) -> None:
        with patch.object(main.SETTINGS, "agent_backend", "claude"):
            claude_prompt = main._build_ai_prompt(
                "task-single", {**self.request, "fit_type": "ai"}
            )
        self.assertIn("call WaitForMcpServers", claude_prompt)
        self.assertIn("until galaxy_morphology_mcp is connected", claude_prompt)

        with patch.object(main.SETTINGS, "agent_backend", "codex"):
            codex_prompt = main._build_ai_prompt(
                "task-multi",
                {
                    **self.request,
                    "fit_type": "ai",
                    "data_type": "multi_band",
                    "lyric_file": "/tmp/example/input/example.lyric",
                    "fitting_mode": "Image Fitting",
                    "extra_args": [],
                },
            )
        self.assertNotIn("WaitForMcpServers", codex_prompt)
        self.assertIn("Call its MCP tools directly", codex_prompt)
        self.assertIn("no separate MCP readiness tool is required", codex_prompt)
        self.assertIn("Never fall back to Bash, direct Python imports", codex_prompt)

    def test_claude_backend_command_and_mcp_config(self) -> None:
        with (
            patch.object(main.SETTINGS, "agent_backend", "claude"),
            patch.object(main.SETTINGS, "claude_model", ""),
            patch.object(main.SETTINGS, "claude_max_budget_usd", ""),
        ):
            command = main._agent_command("task-2", self.request)
            self.assertEqual(command[0], main.SETTINGS.claude_bin)
            self.assertIn("stream-json", command)
            config = json.loads(command[command.index("--mcp-config") + 1])
            server = config["mcpServers"]["galaxy_morphology_mcp"]
            self.assertEqual(server["command"], "/bin/bash")
            allowed_tools = command[command.index("--allowedTools") + 1].split(",")
            self.assertIn("WaitForMcpServers", allowed_tools)
            self.assertEqual(main._agent_cwd(self.request), str(main.SETTINGS.mcp_root))

    # modified by zl: verify Claude receives enough time for the heavy stdio MCP startup.
    def test_claude_mcp_timeout_configuration(self) -> None:
        self.assertEqual(main.os.environ.get("MCP_TIMEOUT"), "120000")
        self.assertEqual(main.os.environ.get("MCP_CONNECT_TIMEOUT_MS"), "120000")

    def test_unsupported_backend(self) -> None:
        with patch.object(main.SETTINGS, "agent_backend", "other"):
            with self.assertRaisesRegex(ValueError, "Unsupported AGENT_BACKEND"):
                main._agent_command("task-3", self.request)

    def test_human_workflow_uses_direct_runner(self) -> None:
        command = main._workflow_command("task-human", self.request)
        self.assertEqual(main._workflow_executor(self.request), "direct")
        self.assertEqual(command[0], main.SETTINGS.direct_fit_python)
        self.assertTrue(command[1].endswith("direct_fit_runner.py"))
        self.assertNotIn("codex", command)
        self.assertNotIn("claude", command)
        self.assertNotIn("--callback-url", command)

    def test_single_band_human_step_is_translated_to_imax(self) -> None:
        self.assertEqual(
            main._single_band_human_options(["--step", "2000"]),
            ["-imax", "2000"],
        )
        self.assertEqual(
            main._single_band_human_options("--step 2000"),
            ["-imax", "2000"],
        )
        with self.assertRaises(HTTPException):
            main._single_band_human_options(["--step", "0"])

    def test_legacy_human_request_is_derived_from_oss_config_path(self) -> None:
        body = main.LocalFitRequestIn.model_validate(
            {
                "fit_type": "human",
                "data_type": "single_band",
                "node_id": "229",
                "fitting_mode": "Image Fitting",
                "lyric_file": "/owner/project/galaxy/229/input/galfit.feedme",
                "workplace": None,
                "output_path": "/owner/project/galaxy/229/output/",
                "args": ["--step", "100"],
                "callback_url": "http://backend/nodes/callback",
            }
        )
        self.assertTrue(body.legacy_human_request)
        self.assertEqual(body.owner, "owner")
        self.assertEqual(body.project_name, "project")
        self.assertEqual(body.galaxy_id, "galaxy")
        self.assertEqual(body.current_metadata["input_dir_file_list"], [body.lyric_file])
        self.assertEqual(body.platform_callback_url, body.callback_url)

    def test_legacy_multiband_arg_files_are_added_and_localized(self) -> None:
        body = main.LocalFitRequestIn.model_validate(
            {
                "fit_type": "human",
                "data_type": "multi_band",
                "node_id": "2",
                "fitting_mode": "SED Fitting",
                "lyric_file": "/owner/project/galaxy/2/input/2.lyric",
                "output_path": "/owner/project/galaxy/2/output/",
                "args": [
                    "--readsummary",
                    "/owner/project/galaxy/2/input/result.gssummary",
                    "--priorpath",
                    "/owner/project/galaxy/2/input/2.prior",
                ],
                "callback_url": "http://backend/nodes/callback",
            }
        )
        self.assertEqual(
            body.current_metadata["input_dir_file_list"],
            [
                "/owner/project/galaxy/2/input/2.lyric",
                "/owner/project/galaxy/2/input/result.gssummary",
                "/owner/project/galaxy/2/input/2.prior",
            ],
        )
        localized = main._localize_multiband_file_args(
            body.args,
            [
                Path("/tmp/stage/2.lyric"),
                Path("/tmp/stage/result.gssummary"),
                Path("/tmp/stage/2.prior"),
            ],
        )
        self.assertEqual(localized[1], "/tmp/stage/result.gssummary")
        self.assertEqual(localized[3], "/tmp/stage/2.prior")

    def test_legacy_feedme_constraint_is_fetched_from_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            feedme = input_dir / "galfit.feedme"
            feedme.write_text("G) galaxy.cons\n", encoding="utf-8")

            def fetch(source, dst_dir, backend, platform_input):
                destination = dst_dir / Path(source).name
                destination.write_text("constraint", encoding="utf-8")
                return destination

            with patch.object(main, "_fetch_input_file", side_effect=fetch) as mocked:
                staged = main._stage_feedme_constraint(
                    feedme, [feedme], "/owner/project/galaxy/229/input/", "oss"
                )
            self.assertEqual(staged.name, "galaxy.cons")
            self.assertEqual(
                mocked.call_args.args[0],
                "/owner/project/galaxy/229/input/galaxy.cons",
            )

    def test_legacy_human_final_callback_has_minimal_contract(self) -> None:
        async def run_in_place(function, *args):
            return function(*args)

        request = {
            "local_platform": {
                "legacy_human_request": True,
                "platform_callback_url": "http://backend/nodes/callback",
            }
        }
        with (
            patch.object(main, "_post_json", return_value={"status": "success"}) as post,
            patch.object(main.asyncio, "to_thread", side_effect=run_in_place),
            patch.object(main, "_append_event"),
        ):
            asyncio.run(
                main._callback_local_platform_workflow_finished(
                    "task-legacy",
                    request,
                    {"exit_code": 0, "business_state": "completed", "final_messages": []},
                )
            )
        self.assertEqual(
            post.call_args.args[1],
            {"task_id": "task-legacy", "status": "success", "message": ""},
        )

    def test_human_multiband_uses_galfits_python(self) -> None:
        request = {
            **self.request,
            "data_type": "multi_band",
            "lyric_file": "/tmp/example/input/example.lyric",
        }
        command = main._workflow_command("task-human-multi", request)
        self.assertEqual(command[0], main.SETTINGS.direct_galfits_python)
        self.assertEqual(main._workflow_executor(request), "direct")

    def test_direct_multiband_absolutizes_staged_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "example.lyric"
            config.write_text(
                "Ia1) [./fits_files/image.fits, 0]\n"
                "Ia2) f115w\n"
                "Ia3) [./fits_files/sigma.fits, 0]\n"
                "Ia15) 0\n",
                encoding="utf-8",
            )
            direct_fit_runner._absolutize_multiband_paths(str(config))
            content = config.read_text(encoding="utf-8")
            self.assertIn(str(root / "fits_files/image.fits"), content)
            self.assertIn(str(root / "fits_files/sigma.fits"), content)
            self.assertIn("\nIa2) f115w\n", content)
            self.assertIn("Ia15) 0", content)
            self.assertEqual(len(content.splitlines()), 4)

    def test_ai_workflow_uses_selected_agent(self) -> None:
        request = {**self.request, "fit_type": "ai"}
        with patch.object(main.SETTINGS, "agent_backend", "codex"):
            self.assertEqual(main._workflow_executor(request), "codex")
            self.assertEqual(main._workflow_command("task-ai", request)[1], "exec")

    # modified by zl: keep local single- and multi-band workspaces at the node root.
    def test_local_platform_stages_configs_at_node_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=main.SETTINGS.service_root) as tmp:
            root = Path(tmp)
            source_galaxy = root / "source" / "galaxy"
            (source_galaxy / "raw").mkdir(parents=True)
            stage_root = root / "stage"

            def stage(data_type: str, node_id: str, config_name: str):
                source_node = source_galaxy / node_id
                source_input = source_node / "input"
                source_output = source_node / "output"
                source_input.mkdir(parents=True)
                source_output.mkdir()
                config = source_input / config_name
                config.write_text("# config\n", encoding="utf-8")
                body = main.LocalFitRequestIn.model_validate(
                    {
                        "owner": "owner",
                        "project_name": "project",
                        "galaxy_id": "galaxy",
                        "node_id": node_id,
                        "fit_type": "AI",
                        "fit_max_round": 2,
                        "fit_instruction": "固定 bulge 的 Sérsic index 为 n=4。",
                        "data_type": data_type,
                        "bizInfo": {
                            "fit_task_id": "TASK-PLATFORM-V4",
                            "fit_task_target_id": f"TARGET-{node_id}",
                        },
                        "current_metadata": {
                            "round_index": 1,
                            "input_dir": str(source_input),
                            "output_dir": str(source_output),
                            "input_dir_file_list": [str(config)],
                        },
                    }
                )
                with (
                    patch.object(main.SETTINGS, "local_stage_root", stage_root),
                    patch.object(main, "_select_file_backend", return_value="local"),
                    patch.object(
                        main,
                        "_rewrite_staged_feedme_paths",
                        return_value={"rewritten_paths": {}, "copied_raw_files": []},
                    ),
                    patch.object(
                        main,
                        "_rewrite_staged_lyric_paths",
                        return_value={"rewritten_paths": {}, "copied_raw_files": []},
                    ),
                    patch.object(main, "_validate_feedme_input_paths", return_value={}),
                ):
                    return main._stage_local_fit_request(body)

            single = stage("single_band", "node-single", "initial.feedme")
            multi = stage("multi_band", "node-multi", "initial.lyric")
            for request, config_key, node_id in (
                (single, "feedme", "node-single"),
                (multi, "lyric_file", "node-multi"),
            ):
                expected_root = stage_root / "owner" / "project" / "galaxy" / node_id
                self.assertEqual(Path(request[config_key]).parent, expected_root)
                self.assertEqual(request["galaxy_dir"], str(expected_root))
                self.assertEqual(request["local_platform"]["stage_work_dir"], str(expected_root))
                self.assertEqual(request["local_platform"]["stage_input_dir"], str(expected_root))
                self.assertTrue((expected_root / "output").is_dir())
                self.assertFalse((expected_root / "input").exists())
                self.assertIn("平台拟合要求", request["instructions"])
                self.assertIn("固定 bulge 的 Sérsic index 为 n=4", request["instructions"])
                self.assertEqual(request["bizInfo"]["fit_task_id"], "TASK-PLATFORM-V4")

    # modified by zl: prevent zero-round agent exits from being reported as successful workflows.
    def test_zero_fit_round_is_business_failure(self) -> None:
        self.assertEqual(main._workflow_business_state(0, 0, 2), "failed")
        self.assertEqual(main._workflow_business_state(0, 1, 2), "complete_less_than_max_round")
        self.assertEqual(main._workflow_business_state(0, 2, 2), "completed")

    def test_claude_stream_collects_assistant_and_result(self) -> None:
        async def run() -> list[str]:
            stream = asyncio.StreamReader()
            stream.feed_data(
                b'{"type":"system","subtype":"thinking_tokens","estimated_tokens":1038}\n'
                b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
                b'{"type":"result","result":"done"}\n'
            )
            stream.feed_eof()
            with patch.object(main, "_append_event") as append_event:
                messages = await main._record_claude_stream("task-4", stream, "stdout")
            self.assertEqual(append_event.call_count, 2)
            self.assertNotIn("thinking_tokens", str(append_event.call_args_list))
            return messages

        self.assertEqual(asyncio.run(run()), ["hello", "done"])

    def test_direct_stream_collects_result(self) -> None:
        async def run() -> list[str]:
            stream = asyncio.StreamReader()
            stream.feed_data(
                b'{"type":"direct_fit.result","result":{"status":"success","message":"done"}}\n'
            )
            stream.feed_eof()
            with patch.object(main, "_append_event"):
                return await main._record_direct_fit_stream("task-5", stream, "stdout")

        messages = asyncio.run(run())
        self.assertEqual(len(messages), 1)
        self.assertEqual(json.loads(messages[0])["status"], "success")

    def test_stream_drain_timeout_cancels_stuck_readers(self) -> None:
        async def wait_forever() -> list[str]:
            await asyncio.Event().wait()
            return []

        async def run():
            stdout_task = asyncio.create_task(wait_forever())
            stderr_task = asyncio.create_task(wait_forever())
            with (
                patch.object(main.SETTINGS, "stream_drain_timeout_sec", 0.01),
                patch.object(main, "_append_event") as append_event,
            ):
                result = await main._finish_workflow_streams(
                    "task-stuck", stdout_task, stderr_task
                )
            self.assertTrue(stdout_task.cancelled())
            self.assertTrue(stderr_task.cancelled())
            append_event.assert_called_once_with(
                "task-stuck",
                "workflow.stream_drain_timeout",
                {"timeout_sec": 0.01},
            )
            return result

        self.assertEqual(asyncio.run(run()), ([], []))


if __name__ == "__main__":
    unittest.main()
