import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import main
from fastapi import HTTPException


class OssBackendTest(unittest.TestCase):
    @staticmethod
    def _callback_request() -> dict:
        return {
            "local_platform": {
                "owner": "owner",
                "project_name": "project",
                "galaxy_id": "galaxy",
                "data_type": "single_band",
                "initial_node_id": "node-1",
                "current_node_id": "node-1",
                "fit_max_round": 2,
                "platform_callback_url": "http://backend/callback",
                "file_backend": "oss",
                "round_nodes": {
                    "1": {
                        "node_id": "node-1",
                        "node_dir": "/owner/project/galaxy/node-1",
                        "input_dir": "/owner/project/galaxy/node-1/input",
                        "output_dir": "/owner/project/galaxy/node-1/output",
                    }
                },
            }
        }

    def test_round_callback_matches_platform_contract(self):
        async def run_in_place(function, *args):
            return function(*args)

        request = self._callback_request()
        copied = {
            "node_id": "node-1",
            "platform_node_dir": "/owner/project/galaxy/node-1",
            "input_dir": "/owner/project/galaxy/node-1/input",
            "output_dir": "/owner/project/galaxy/node-1/output",
            "copied_files": [],
            "failed_files": [],
        }
        response = {
            "status": "success",
            "next_node_id": "node-2",
            "next_input_dir": "/owner/project/galaxy/node-2/input",
            "next_output_dir": "/owner/project/galaxy/node-2/output",
        }
        with (
            patch.object(main, "_copy_round_artifacts_to_platform", return_value=copied),
            patch.object(main, "_post_json", return_value=response) as post,
            patch.object(main.asyncio, "to_thread", side_effect=run_in_place),
            patch.object(main, "_append_event"),
            patch.object(main, "_update_task_request"),
        ):
            asyncio.run(
                main._callback_local_platform(
                    "task", request, {"status": "success", "fit_statistics": {}}, 1
                )
            )

        payload = post.call_args.args[1]
        self.assertEqual(payload["data_type"], "single_band")
        self.assertIsNone(payload["message"])
        self.assertEqual(payload["event"], "fit_round_finished")
        self.assertEqual(request["local_platform"]["current_node_id"], "node-2")

    def test_round_callback_requires_complete_next_node_response(self):
        async def run_in_place(function, *args):
            return function(*args)

        request = self._callback_request()
        copied = {
            "node_id": "node-1",
            "platform_node_dir": "/owner/project/galaxy/node-1",
            "input_dir": "/owner/project/galaxy/node-1/input",
            "output_dir": "/owner/project/galaxy/node-1/output",
            "copied_files": [],
            "failed_files": [],
        }
        with (
            patch.object(main, "_copy_round_artifacts_to_platform", return_value=copied),
            patch.object(main, "_post_json", return_value={"status": "success"}),
            patch.object(main.asyncio, "to_thread", side_effect=run_in_place),
            patch.object(main, "_append_event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "missing fields"):
                asyncio.run(
                    main._callback_local_platform(
                        "task", request, {"status": "success", "fit_statistics": {}}, 1
                    )
                )

    def test_upload_binary_file_as_zip_multipart(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "result.fits"
            raw = b"\x00\xffFITS"
            source.write_bytes(raw)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"status":"success"}'

            with patch.object(main, "urlopen", return_value=response) as mocked_urlopen:
                main._upload_oss_file(source, "/owner/project/galaxy/node/output/result.fits")

            request = mocked_urlopen.call_args.args[0]
            content_type = request.headers["Content-type"]
            boundary = content_type.split("boundary=", 1)[1]
            zip_start = request.data.index(b"PK\x03\x04")
            zip_end = request.data.rindex(f"\r\n--{boundary}--".encode("utf-8"))
            with zipfile.ZipFile(io.BytesIO(request.data[zip_start:zip_end])) as archive:
                self.assertEqual(archive.read("result.fits"), raw)
            self.assertIn(b"/owner/project/galaxy/node/output/", request.data)
            self.assertEqual(request.get_method(), "POST")

    def test_round_artifacts_use_oss_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            output = archive / "image_fit.png"
            config = archive / "round.con"
            output.write_bytes(b"png")
            config.write_text("constraint", encoding="utf-8")
            request = {
                "local_platform": {
                    "file_backend": "oss",
                    "current_node_id": "183",
                    "platform_node_dir": "/owner/project/6978/183",
                    "platform_input_dir": "/owner/project/6978/183/input",
                    "platform_output_dir": "/owner/project/6978/183/output",
                    "round_nodes": {},
                }
            }

            with (
                patch.object(main, "_store_platform_file") as store,
                patch.object(main, "_append_event"),
                patch.object(main, "_update_task_request"),
            ):
                result = main._copy_round_artifacts_to_platform(
                    "task", request, {"archive_dir": str(archive)}, 1
                )

            store.assert_called_once_with(
                output.resolve(), "/owner/project/6978/183/output/image_fit.png", "oss"
            )
            self.assertEqual(
                result["copied_files"], ["/owner/project/6978/183/output/image_fit.png"]
            )
            self.assertEqual(result["failed_files"][0]["action"], "skipped_initial_input")

    def test_one_failed_upload_does_not_abort_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "result.png"
            artifact.write_bytes(b"png")
            request = {
                "local_platform": {
                    "file_backend": "oss",
                    "current_node_id": "183",
                    "platform_node_dir": "/owner/project/6978/183",
                    "platform_input_dir": "/owner/project/6978/183/input",
                    "platform_output_dir": "/owner/project/6978/183/output",
                    "round_nodes": {},
                }
            }

            with (
                patch.object(
                    main,
                    "_store_platform_file",
                    side_effect=HTTPException(status_code=502, detail="upload failed"),
                ),
                patch.object(main, "_append_event"),
                patch.object(main, "_update_task_request"),
            ):
                result = main._copy_round_artifacts_to_platform(
                    "task", request, {"artifacts": [str(artifact)]}, 1
                )

            self.assertEqual(result["copied_files"], [])
            self.assertEqual(result["failed_files"][0]["action"], "not_uploaded")
            self.assertEqual(
                result["failed_files"][0]["target"],
                "/owner/project/6978/183/output/result.png",
            )

    def test_single_band_filters_and_renames_round_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            for name in ("galfit_comparison.png", "galfit.03", "subcomps.fits", "fit.log"):
                (archive / name).write_bytes(b"data")
            request = self._callback_request()

            with (
                patch.object(main, "_store_platform_file") as store,
                patch.object(main, "_append_event"),
                patch.object(main, "_update_task_request"),
            ):
                result = main._copy_round_artifacts_to_platform(
                    "task", request, {"archive_dir": str(archive)}, 1
                )

            targets = [call.args[1] for call in store.call_args_list]
            self.assertEqual(
                targets,
                [
                    "/owner/project/galaxy/node-1/output/galfit_comparisonimage_fit.png",
                    "/owner/project/galaxy/node-1/output/fit.log",
                ],
            )
            self.assertEqual(result["copied_files"], targets)

    def test_multi_band_keeps_only_renamed_all_bands_image_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            for name in ("all_bands_comparison.png", "obj_image_fit.png", "run.log"):
                (archive / name).write_bytes(b"data")
            request = self._callback_request()
            request["local_platform"]["data_type"] = "multi_band"

            with (
                patch.object(main, "_store_platform_file") as store,
                patch.object(main, "_append_event"),
                patch.object(main, "_update_task_request"),
            ):
                result = main._copy_round_artifacts_to_platform(
                    "task", request, {"archive_dir": str(archive)}, 1
                )

            targets = [call.args[1] for call in store.call_args_list]
            self.assertEqual(
                targets,
                [
                    "/owner/project/galaxy/node-1/output/all_bands_comparisonimage_fit.png",
                    "/owner/project/galaxy/node-1/output/run.log",
                ],
            )
            self.assertEqual(result["copied_files"], targets)

    def test_final_copy_uploads_late_best_round_comparison_to_its_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_dir = Path(tmp) / "input"
            archive_dir = stage_dir / "archives" / "round-2"
            archive_dir.mkdir(parents=True)
            comparison = archive_dir / "best_round_comparison.md"
            comparison.write_text("comparison", encoding="utf-8")
            request = {
                "galaxy_dir": str(stage_dir),
                "local_platform": {
                    "file_backend": "oss",
                    "last_completed_round_index": 2,
                    "round_nodes": {
                        "2": {"output_dir": "/owner/project/galaxy/node-2/output"}
                    },
                },
            }
            row = {
                "payload_json": json.dumps(
                    {"round_index": 2, "archive_dir": str(archive_dir)}
                )
            }
            connection = MagicMock()
            connection.__enter__.return_value.execute.return_value.fetchall.return_value = [row]

            with (
                patch.object(main, "_connect", return_value=connection),
                patch.object(main, "_store_platform_file") as store,
                patch.object(main, "_append_event"),
            ):
                result = main._copy_final_agent_artifacts_to_platform("task", request)

            store.assert_called_once_with(
                comparison.resolve(),
                "/owner/project/galaxy/node-2/output/best_round_comparison.md",
                "oss",
            )
            self.assertEqual(
                result["final_artifacts"],
                ["/owner/project/galaxy/node-2/output/best_round_comparison.md"],
            )


if __name__ == "__main__":
    unittest.main()
