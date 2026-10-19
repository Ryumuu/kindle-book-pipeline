#!/usr/bin/env python3
"""Deterministic unit tests for the ebook-to-kindle Skill."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


class PerformanceReviewTests(unittest.TestCase):
    def test_scanned_pdf_review_ranks_bottleneck_and_requires_quality_gated_experiment(self) -> None:
        from ebook_kindle.performance import review_report

        review = review_report(
            {
                "route": "docling-scanned-pdf-ocrmac",
                "details": {
                    "elapsed_seconds": 187.645,
                    "workspace": {
                        "stage_timings_seconds": {
                            "master_materialization": 180.230,
                            "master_validation": 3.076,
                            "final_conversion_and_validation": 2.648,
                        }
                    },
                },
            }
        )
        self.assertEqual(review["status"], "REVIEW_REQUIRED")
        self.assertEqual(review["primary_bottleneck"]["stage"], "master_materialization")
        self.assertEqual(review["candidate_experiments"][0]["id"], "page-level-ocr-routing")
        self.assertEqual(review["adoption_gate"]["quality_regressions_allowed"], 0)
        self.assertIn("compare_timing_and_quality_before_next_book", review["agent_closeout_required"])

    def test_review_exposes_nested_pdf_time_blocks_without_double_ranking_totals(self) -> None:
        from ebook_kindle.performance import build_performance_review

        review = build_performance_review(
            route="docling-digital-pdf",
            elapsed_seconds=20,
            workspace={"stage_timings_seconds": {"preflight": 1, "total_before_publication": 19}},
            route_details={
                "pdf_stage_timings_seconds": {"input_probe": 0.5, "docling_backend": 12},
                "pdf_backend_stage_timings_seconds": {"model_and_pipeline_setup": 3, "total": 12},
            },
        )
        self.assertEqual(review["primary_bottleneck"]["stage"], "preflight")
        self.assertEqual(review["pdf_stage_timings_seconds"]["docling_backend"], 12.0)
        self.assertEqual(review["pdf_backend_stage_timings_seconds"]["model_and_pipeline_setup"], 3.0)


class CoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from ebook_kindle import core

        cls.core = core

    def test_supported_inputs_and_local_only_boundary(self) -> None:
        for suffix in (".docx", ".pdf", ".html", ".htm", ".txt", ".md", ".markdown", ".epub"):
            self.assertTrue(self.core.is_supported_input(Path("book" + suffix)))
        self.assertFalse(self.core.is_supported_input(Path("book.mobi")))
        self.assertTrue(self.core.looks_like_url("https://example.invalid/book.pdf"))
        self.assertFalse(self.core.looks_like_url("/tmp/book.pdf"))

    def test_format_parser_defaults_to_azw3_and_normalizes_mobi6(self) -> None:
        self.assertEqual(self.core.parse_formats(None), ("azw3",))
        self.assertEqual(self.core.parse_formats("epub,mobi6,AZW3"), ("epub", "mobi", "azw3"))
        with self.assertRaises(self.core.SkillError) as caught:
            self.core.parse_formats("pdf")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_OUTPUT")

    def test_kindle_conversion_options_zero_margins_and_build_real_navigation(self) -> None:
        from ebook_kindle import pipeline

        options = pipeline.kindle_master_options(Path("/private/pw2.css"), "Fixture title")
        value = lambda key: options[options.index(key) + 1]
        for side in ("top", "right", "bottom", "left"):
            self.assertEqual(value(f"--margin-{side}"), "0")
        self.assertEqual(value("--filter-css"), "font-family,font-size,line-height")
        self.assertIn("--epub-inline-toc", options)
        self.assertIn("--use-auto-toc", options)
        self.assertIn("--level1-toc", options)
        self.assertIn("--level2-toc", options)
        self.assertIn("--start-reading-at", options)
        self.assertEqual(value("--max-toc-links"), "1000")

    def test_job_directory_and_runtime_directories_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            job = self.core.make_private_job_dir(Path(outer))
            mode = stat.S_IMODE(job.stat().st_mode)
            self.assertEqual(mode, 0o700)
            self.assertNotEqual(job.name, "job")

    def test_job_environment_redirects_home_and_all_known_state(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            job = self.core.make_private_job_dir(Path(outer))
            env = self.core.build_job_env(
                job,
                Path("/runtime"),
                base={
                    "HOME": "/Users/example",
                    "PATH": "/bin",
                    "OPENAI_API_KEY": "must-not-pass",
                    "AWS_SECRET_ACCESS_KEY": "must-not-pass",
                    "PYTHONPATH": "/untrusted",
                    "JAVA_TOOL_OPTIONS": "-javaagent:bad",
                    "HTTPS_PROXY": "http://private-proxy.invalid",
                    "UV_INDEX_URL": "https://private-index.invalid/simple",
                    "CALIBRE_CONFIG_DIRECTORY": "/untrusted-calibre",
                },
            )
            self.assertEqual(env["HOME"], str(job / "home"))
            self.assertEqual(env["CFFIXED_USER_HOME"], str(job / "home"))
            self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            self.assertEqual(env["CALIBRE_CONFIG_DIRECTORY"], str(job / "calibre-config"))
            self.assertEqual(env["CALIBRE_CACHE_DIRECTORY"], str(job / "calibre-cache"))
            self.assertEqual(env["TMPDIR"], str(job / "tmp"))
            self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            self.assertEqual(env["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
            for key in (
                "OPENAI_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                "PYTHONPATH",
                "JAVA_TOOL_OPTIONS",
                "HTTPS_PROXY",
                "UV_INDEX_URL",
            ):
                self.assertNotIn(key, env)

    def test_calibre_user_state_signature_detects_content_and_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            home = Path(outer) / "home"
            home.mkdir()
            absent = self.core.snapshot_calibre_user_state(home)
            preferences = home / "Library" / "Preferences" / "calibre"
            preferences.mkdir(parents=True)
            config = preferences / "global.py.json"
            config.write_bytes(b"fixture")
            present = self.core.snapshot_calibre_user_state(home)
            self.assertNotEqual(absent, present)
            config.chmod(0o600)
            metadata_changed = self.core.snapshot_calibre_user_state(home)
            self.assertNotEqual(present, metadata_changed)

    def test_explicit_metadata_is_safe_and_reaches_calibre_options(self) -> None:
        from ebook_kindle import pipeline

        request = pipeline.ConversionRequest(
            input_value="fixture.pdf",
            output_dir=Path("/private/output"),
            formats=("azw3",),
            runtime_root=Path("/private/runtime"),
            title=" WSET3   归普三级资料 ",
            authors="归普教育资料汇编",
            language="ZH",
        )
        metadata = pipeline._conversion_metadata(request, Path("fixture.pdf"))
        options = pipeline.kindle_master_options(
            Path("/private/pw2.css"),
            str(metadata["title"]),
            metadata["authors"],
            metadata["language"],
        )
        self.assertEqual(metadata["title"], "WSET3 归普三级资料")
        self.assertEqual(metadata["language"], "zh")
        self.assertEqual(options[options.index("--authors") + 1], "归普教育资料汇编")
        self.assertEqual(options[options.index("--language") + 1], "zh")

        invalid = pipeline.ConversionRequest(
            input_value="fixture.pdf",
            output_dir=Path("/private/output"),
            formats=("azw3",),
            runtime_root=Path("/private/runtime"),
            language="not_a_language",
        )
        with self.assertRaises(self.core.SkillError):
            pipeline._conversion_metadata(invalid, Path("fixture.pdf"))

    def test_runtime_root_rejects_symlink_components(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            actual = root / "actual"
            actual.mkdir()
            link = root / "runtime-link"
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(self.core.SkillError) as caught:
                runtime.guard_runtime_root(link)
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")

    def test_runtime_root_rejects_symlinked_top_level_entries(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            outside = Path(outer) / "outside"
            outside.mkdir()
            for name in ("core", "pdf", "jobs"):
                link = root / name
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(self.core.SkillError):
                    runtime.guard_runtime_root(root)
                link.unlink()

    def test_nested_model_symlink_cannot_satisfy_runtime_contract(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            models = root / "models"
            outside = root / "outside"
            models.mkdir()
            outside.mkdir()
            (outside / "model.bin").write_bytes(b"same-hash-content")
            (models / "nested").symlink_to(outside, target_is_directory=True)
            self.assertFalse(runtime.contained_regular_file(models / "nested" / "model.bin", models))

    def test_runtime_capacity_scan_fails_closed_on_metadata_error(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            (root / "broken").write_bytes(b"fixture")
            with mock.patch.object(runtime.os, "scandir", side_effect=OSError("synthetic scandir failure")):
                with self.assertRaises(self.core.SkillError) as caught:
                    runtime.tree_bytes(root)
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")

    def test_version_matching_rejects_longer_numeric_versions(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory() as outer:
            env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            cwd = Path(outer)
            self.assertFalse(runtime._version_check(["/bin/echo", "19.13.0"], "9.13.0", env=env, cwd=cwd)["ok"])
            self.assertFalse(runtime._version_check(["/bin/echo", "15.3.0"], "5.3.0", env=env, cwd=cwd)["ok"])
            self.assertFalse(runtime._version_check(["/bin/echo", "9.13.0dev"], "9.13.0", env=env, cwd=cwd)["ok"])
            self.assertFalse(runtime._version_check(["/bin/echo", "5.3.0-foo"], "5.3.0", env=env, cwd=cwd)["ok"])
            self.assertTrue(runtime._version_check(["/bin/echo", "v5.3.0"], "5.3.0", env=env, cwd=cwd)["ok"])

    def test_doctor_cleanup_failure_cannot_report_hygiene_pass(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            jobs = Path(outer) / "jobs"
            job = jobs / "job-current"
            job.mkdir(parents=True)
            with mock.patch("ebook_kindle.runtime.shutil.rmtree", side_effect=OSError("synthetic cleanup failure")):
                result = runtime._doctor_job_hygiene(job, jobs, 0)
            self.assertFalse(result["ok"])
            self.assertEqual(result["remaining_entries"], 1)

    def test_runtime_lock_is_atomic_and_kernel_released(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            with runtime.runtime_operation_lock(root):
                with self.assertRaises(self.core.SkillError) as busy:
                    with runtime.runtime_operation_lock(root):
                        pass
                self.assertEqual(busy.exception.code, "RUNTIME_BUSY")
            lock = root / runtime.RUNTIME_LOCK_NAME
            self.assertTrue(lock.is_file())
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            with runtime.runtime_operation_lock(root):
                self.assertTrue(lock.exists())
            self.assertTrue(lock.exists())

    def test_runtime_lock_close_error_cannot_rewrite_committed_truth(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            real_close = runtime.os.close

            def close_then_report_error(descriptor: int) -> None:
                real_close(descriptor)
                raise OSError("synthetic close report")

            with mock.patch.object(runtime.os, "close", side_effect=close_then_report_error):
                with runtime.runtime_operation_lock(root):
                    pass
            with runtime.runtime_operation_lock(root):
                pass

    def test_runtime_lock_rejects_a_hardlink_without_changing_the_victim(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            root = base / "ebook-to-kindle"
            root.mkdir()
            victim = base / "user-file"
            victim.write_bytes(b"")
            victim.chmod(0o644)
            os.link(victim, root / runtime.RUNTIME_LOCK_NAME)
            with self.assertRaises(self.core.SkillError) as caught:
                with runtime.runtime_operation_lock(root):
                    pass
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)

    def test_conversion_rejects_any_failed_jobs_hygiene_check(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime_root = base / "ebook-to-kindle"
            runtime_root.mkdir()
            source = base / "book.txt"
            source.write_text("正文内容足够长，用于验证临时目录卫生检查必须失败关闭。", encoding="utf-8")
            request = pipeline.ConversionRequest(
                input_value=str(source),
                output_dir=base / "out",
                formats=("azw3",),
                runtime_root=runtime_root,
            )
            verified = {
                "core_verified": True,
                "pdf_verified": False,
                "deep_checks": {"jobs_hygiene": {"ok": False, "preexisting_entries": 0, "remaining_entries": 1}},
            }
            with mock.patch.object(pipeline, "verify_runtime", return_value=verified), mock.patch.object(
                pipeline, "snapshot_calibre_user_state", return_value={"unchanged": True}
            ):
                with self.assertRaises(self.core.SkillError) as caught:
                    pipeline._convert_locked(request, SKILL_ROOT)
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")

    def test_conversion_rejects_runtime_output_before_creating_a_job(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime_root = base / "ebook-to-kindle"
            jobs = runtime_root / "jobs"
            jobs.mkdir(parents=True)
            source = base / "book.txt"
            source.write_text("正文内容足够长，用于验证运行时输出边界。", encoding="utf-8")
            request = pipeline.ConversionRequest(
                input_value=str(source),
                output_dir=jobs,
                formats=("azw3",),
                runtime_root=runtime_root,
                report_path=Path("runtime.json"),
                overwrite=True,
            )
            verified = {
                "core_verified": True,
                "pdf_verified": False,
                "deep_checks": {"jobs_hygiene": {"ok": True, "preexisting_entries": 0, "remaining_entries": 0}},
            }
            with mock.patch.object(pipeline, "verify_runtime", return_value=verified), mock.patch.object(
                pipeline, "snapshot_calibre_user_state", return_value={"unchanged": True}
            ), mock.patch.object(pipeline, "make_private_job_dir") as make_job:
                with self.assertRaises(self.core.SkillError) as caught:
                    pipeline._convert_locked(request, SKILL_ROOT)
            self.assertEqual(caught.exception.code, "UNSUPPORTED_OUTPUT")
            make_job.assert_not_called()

    def test_conversion_setup_cleanup_failure_overrides_the_original_error(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime_root = base / "ebook-to-kindle"
            jobs = runtime_root / "jobs"
            jobs.mkdir(parents=True)
            source = base / "book.txt"
            source.write_text("正文内容足够长，用于验证初始化失败后必须严格清理。", encoding="utf-8")
            request = pipeline.ConversionRequest(
                input_value=str(source),
                output_dir=base / "out",
                formats=("azw3",),
                runtime_root=runtime_root,
            )
            verified = {
                "core_verified": True,
                "pdf_verified": False,
                "deep_checks": {"jobs_hygiene": {"ok": True, "preexisting_entries": 0, "remaining_entries": 0}},
            }
            with mock.patch.object(pipeline, "verify_runtime", return_value=verified), mock.patch.object(
                pipeline, "snapshot_calibre_user_state", return_value={"unchanged": True}
            ), mock.patch.object(pipeline, "build_job_env", side_effect=OSError("synthetic setup failure")), mock.patch.object(
                pipeline.shutil, "rmtree", side_effect=OSError("synthetic cleanup failure")
            ):
                with self.assertRaises(self.core.SkillError) as caught:
                    pipeline._convert_locked(request, SKILL_ROOT)
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            self.assertEqual(len(list(jobs.iterdir())), 1)

    def test_postcommit_snapshot_failure_cannot_reverse_a_persisted_pass(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime_root = base / "ebook-to-kindle"
            (runtime_root / "jobs").mkdir(parents=True)
            source = base / "book.txt"
            source_text = "正文内容足够长，用于证明发布完成后的外部状态变化不能反转已持久化的成功真值。"
            source.write_text(source_text, encoding="utf-8")
            output = base / "out"
            output.mkdir()
            request = pipeline.ConversionRequest(
                input_value=str(source),
                output_dir=output,
                formats=("epub",),
                runtime_root=runtime_root,
            )
            verified = {
                "core_verified": True,
                "pdf_verified": False,
                "deep_checks": {"jobs_hygiene": {"ok": True, "preexisting_entries": 0, "remaining_entries": 0}},
            }
            state = {"unchanged": "same"}

            def fake_build(**kwargs: object) -> tuple:
                job = Path(kwargs["job"])
                master = job / "master.epub"
                master.write_bytes(b"synthetic-master")
                return master, "direct-text", {"source_resources": {}}, source_text

            def fake_calibre(
                paths: object,
                input_file: Path,
                destination: Path,
                env: object,
                cwd: Path,
                timeout: int,
                extra: object = (),
            ) -> None:
                destination.write_text(source_text, encoding="utf-8")

            def fake_extract_master_baseline(master: Path, destination: Path) -> str:
                destination.write_text(source_text, encoding="utf-8")
                destination.chmod(0o600)
                return source_text

            def fake_publish(
                staged: object,
                report_path: Path,
                report: dict,
                overwrite: bool,
                before_commit: object,
                directory_anchor: object = None,
            ) -> dict:
                before_commit()
                return report

            snapshots = [
                state,
                state,
                state,
                state,
                self.core.SkillError("ISOLATION_FAILED", "postcommit synthetic change"),
            ]
            with mock.patch.object(pipeline, "verify_runtime", return_value=verified), mock.patch.object(
                pipeline, "snapshot_calibre_user_state", side_effect=snapshots
            ) as snapshot, mock.patch.object(pipeline, "_build_master_epub", side_effect=fake_build), mock.patch.object(
                pipeline, "_epubcheck", return_value={"fatal": 0, "error": 0, "warning": 0}
            ), mock.patch.object(
                pipeline,
                "inspect_epub_structure",
                return_value={
                    "content_image_resources": 0,
                    "toc_entries": 1,
                    "valid_ncx_toc_entries": 1,
                    "noteref_count": 0,
                    "resolved_noteref_count": 0,
                    "bidirectional_note_count": 0,
                    "footnote_count": 0,
                    "bodymatter_landmarks": 1,
                },
            ), mock.patch.object(
                pipeline, "validate_image_retention", return_value={"retained_content_images": 0}
            ), mock.patch.object(
                pipeline,
                "_validate_readability",
                return_value={
                    "cjk_spacing_anomalies": 0,
                    "suspicious_cjk_block_breaks": 0,
                    "prose_hard_breaks": 0,
                    "inline_typography_declarations": 0,
                    "unsafe_font_size_declarations": 0,
                    "font_family_declarations": 0,
                    "unsafe_line_height_declarations": 0,
                },
            ), mock.patch.object(
                pipeline, "_extract_master_baseline", side_effect=fake_extract_master_baseline
            ), mock.patch.object(pipeline, "_calibre_convert", side_effect=fake_calibre), mock.patch.object(
                pipeline, "_publish_transaction", side_effect=fake_publish
            ):
                result = pipeline._convert_locked(request, SKILL_ROOT)
            self.assertEqual(result["status"], "PASS")
            # The fourth snapshot is still pre-commit (inside before_commit).
            # A fifth post-commit probe would raise and must never be called.
            self.assertEqual(snapshot.call_count, 4)
            self.assertEqual(list((runtime_root / "jobs").iterdir()), [])

    def test_doctor_env_setup_failure_still_removes_its_job(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            jobs = root / "jobs"
            jobs.mkdir(parents=True)
            fake_status = {
                "core_ready": True,
                "pdf_ready": False,
                "persistent_bytes": 0,
                "runtime_schema": 1,
                "runtime_version": "0.1.0",
                "components": {},
            }
            with mock.patch.object(runtime, "readiness", return_value=fake_status), mock.patch.object(
                runtime, "load_manifest", return_value={"schema_version": 1, "runtime_version": "0.1.0", "components": {}}
            ), mock.patch.object(runtime, "snapshot_calibre_user_state", return_value={"state": "same"}), mock.patch.object(
                runtime, "build_job_env", side_effect=OSError("synthetic env failure")
            ):
                with self.assertRaises(OSError):
                    runtime.verify_runtime(root, SKILL_ROOT)
            self.assertEqual(list(jobs.iterdir()), [])

    def test_non_pdf_runtime_verification_skips_pdf_hashes(self) -> None:
        from ebook_kindle import runtime

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            (root / "jobs").mkdir(parents=True)
            status = {
                "core_ready": True,
                "pdf_ready": True,
                "persistent_bytes": 10,
                "runtime_name": runtime.RUNTIME_NAME,
                "runtime_schema": runtime.RUNTIME_SCHEMA_VERSION,
                "runtime_version": runtime.RUNTIME_VERSION,
                "components": {"core": runtime.CORE_COMPONENT, "pdf": {}},
            }
            manifest = {
                "schema_version": runtime.RUNTIME_SCHEMA_VERSION,
                "runtime_version": runtime.RUNTIME_VERSION,
                "runtime_name": runtime.RUNTIME_NAME,
                "components": {"core": runtime.CORE_COMPONENT, "pdf": {}},
            }
            with mock.patch.object(runtime, "readiness", return_value=status), mock.patch.object(
                runtime, "load_manifest", return_value=manifest
            ), mock.patch.object(
                runtime, "snapshot_calibre_user_state", return_value={"state": "same"}
            ), mock.patch.object(
                runtime, "_version_check", return_value={"ok": True, "return_code": 0}
            ), mock.patch.object(runtime, "sha256_file") as sha:
                verified = runtime.verify_runtime(root, SKILL_ROOT, require_pdf=False)
            self.assertTrue(verified["core_verified"])
            self.assertTrue(verified["deep_ready"])
            self.assertEqual(verified["verification_scope"], "core")
            sha.assert_not_called()

    def test_conversion_commands_use_a_real_network_deny_sandbox(self) -> None:
        command = self.core.offline_subprocess_command(
            [sys.executable, "-c", "import socket; socket.socket().bind(('127.0.0.1', 0))"]
        )
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_report_paths_are_relative_and_secrets_are_not_serialized(self) -> None:
        report = self.core.safe_report(
            status="PASS",
            input_path=Path("/Users/example/secret/book.txt"),
            output_dir=Path("/Users/example/out"),
            outputs=[Path("/Users/example/out/book.azw3")],
            route="calibre-direct",
            details={"token": "must-not-appear", "pages": 2},
        )
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("/Users/example", encoded)
        self.assertNotIn("must-not-appear", encoded)
        self.assertEqual(report["input"], "book.txt")
        self.assertEqual(report["outputs"], ["book.azw3"])

    def test_public_json_write_preserves_existing_directory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "shared"
            output.mkdir(mode=0o755)
            output.chmod(0o755)
            self.core.atomic_write_json(output / "report.json", {"status": "PASS"}, private_parent=False)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)


class WorkspaceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from ebook_kindle import workspace

        cls.workspace = workspace

    def test_workspace_is_private_content_addressed_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "book-workspace"
            store = self.workspace.BookWorkspace.open(root)
            with store.locked():
                reference = store.put_bytes(b"private authored fixture")
                store.write_node("master", "a" * 64, {"master": reference})
                loaded = store.read_node("master", "a" * 64)
                self.assertEqual(store.read_object(loaded["master"]).read_bytes(), b"private authored fixture")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "workspace.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.object_path(reference["sha256"]).stat().st_mode), 0o600)
            status = self.workspace.inspect_workspace(root)
            self.assertEqual(status["workspace_name"], "ebook-to-kindle-book-workspace")
            self.assertEqual(status["node_counts"]["master"], 1)
            self.assertNotIn(str(root), json.dumps(status))

    def test_workspace_keys_are_canonical_and_exactly_invalidated(self) -> None:
        base = {"source": "1" * 64, "recipe": {"css": "2" * 64, "calibre": "9.13.0"}}
        first = self.workspace.cache_key(base)
        reordered = self.workspace.cache_key({"recipe": {"calibre": "9.13.0", "css": "2" * 64}, "source": "1" * 64})
        changed = self.workspace.cache_key({"source": "1" * 64, "recipe": {"css": "3" * 64, "calibre": "9.13.0"}})
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_workspace_corruption_fails_closed_instead_of_becoming_a_miss(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            store = self.workspace.BookWorkspace.open(Path(outer) / "book-workspace")
            with store.locked():
                reference = store.put_bytes(b"trusted")
                store.object_path(reference["sha256"]).write_bytes(b"tampered")
                with self.assertRaises(SkillError) as caught:
                    store.read_object(reference)
            self.assertEqual(caught.exception.code, "WORKSPACE_INVALID")

    def test_workspace_node_evidence_tampering_fails_closed(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            store = self.workspace.BookWorkspace.open(Path(outer) / "book-workspace")
            key = "b" * 64
            with store.locked():
                store.write_node("master", key, {"route": "trusted"})
                node_path = store.nodes / "master" / f"{key}.json"
                payload = json.loads(node_path.read_text(encoding="utf-8"))
                payload["route"] = "tampered"
                node_path.write_text(json.dumps(payload), encoding="utf-8")
                node_path.chmod(0o600)
                with self.assertRaises(SkillError) as caught:
                    store.read_node("master", key)
            self.assertEqual(caught.exception.code, "WORKSPACE_INVALID")
            with self.assertRaises(SkillError) as inspect_caught:
                self.workspace.inspect_workspace(store.root)
            self.assertEqual(inspect_caught.exception.code, "WORKSPACE_INVALID")

    def test_workspace_tree_roundtrip_rejects_links_and_preserves_bytes(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            source = base / "semantic"
            (source / "media").mkdir(parents=True)
            (source / "document.html").write_text("<html><body>正文</body></html>", encoding="utf-8")
            (source / "media" / "image.bin").write_bytes(b"image")
            store = self.workspace.BookWorkspace.open(base / "book-workspace")
            with store.locked():
                tree = store.put_tree(source)
                restored = base / "restored"
                store.restore_tree(tree, restored)
                self.assertEqual((restored / "document.html").read_text(encoding="utf-8"), "<html><body>正文</body></html>")
                self.assertEqual((restored / "media" / "image.bin").read_bytes(), b"image")
                (source / "bad-link").symlink_to(source / "document.html")
                with self.assertRaises(SkillError) as caught:
                    store.put_tree(source)
            self.assertEqual(caught.exception.code, "WORKSPACE_INVALID")

    def test_workspace_overlap_symlink_and_wrong_identity_are_rejected(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            input_path = base / "book.epub"
            input_path.write_bytes(b"book")
            root = base / "workspace"
            root.mkdir()
            (root / "foreign.txt").write_text("not ours", encoding="utf-8")
            with self.assertRaises(SkillError) as foreign:
                self.workspace.BookWorkspace.open(root)
            self.assertEqual(foreign.exception.code, "WORKSPACE_INVALID")
            root.rename(base / "foreign")
            link = base / "workspace-link"
            link.symlink_to(base / "foreign", target_is_directory=True)
            with self.assertRaises(SkillError) as symlink:
                self.workspace.BookWorkspace.open(link)
            self.assertEqual(symlink.exception.code, "WORKSPACE_INVALID")
            safe = self.workspace.BookWorkspace.open(base / "safe")
            with self.assertRaises(SkillError) as overlap:
                safe.validate_boundaries(input_path=base / "safe" / "inside.epub", output_dir=base / "out", runtime_root=base / "runtime", skill_root=SKILL_ROOT)
            self.assertEqual(overlap.exception.code, "WORKSPACE_INVALID")

    def test_workspace_purge_requires_exact_identity_and_confirmation(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "book-workspace"
            store = self.workspace.BookWorkspace.open(root)
            with store.locked():
                store.put_bytes(b"private")
            with self.assertRaises(SkillError):
                self.workspace.purge_workspace(root, "WRONG")
            self.assertTrue(root.exists())
            result = self.workspace.purge_workspace(root, "PURGE_EBOOK_WORKSPACE")
            self.assertEqual(result["status"], "PURGED")
            self.assertFalse(root.exists())

    def test_master_recipe_change_reuses_semantic_but_invalidates_master_and_final(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            copied_skill = base / "skill"
            needed = set((*pipeline.SEMANTIC_RECIPE_FILES, *pipeline.MASTER_RECIPE_FILES, *pipeline.FINAL_RECIPE_FILES))
            for relative in needed:
                source = SKILL_ROOT / relative
                target = copied_skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            runtime = base / "runtime"
            runtime.mkdir()
            (runtime / "runtime.json").write_text(
                json.dumps(
                    {
                        "runtime_name": "ebook-to-kindle",
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "components": {"core": {"calibre": {"version": "9.13.0"}}, "pdf": {"docling": "2.119.0"}},
                    }
                ),
                encoding="utf-8",
            )
            request = pipeline.ConversionRequest(
                input_value=str(SKILL_ROOT / "evals" / "fixtures" / "fixture-digital-layout.pdf"),
                output_dir=base / "out",
                formats=("azw3",),
                runtime_root=runtime,
            )
            before = pipeline._workspace_cache_plan(
                input_path=Path(request.input_value), request=request, runtime_root=runtime, skill_root=copied_skill
            )
            css = copied_skill / "assets" / "pw2.css"
            css.write_text(css.read_text(encoding="utf-8") + "\nbody { widows: 2; }\n", encoding="utf-8")
            after = pipeline._workspace_cache_plan(
                input_path=Path(request.input_value), request=request, runtime_root=runtime, skill_root=copied_skill
            )
            self.assertEqual(before["semantic_key"], after["semantic_key"])
            self.assertNotEqual(before["master_key"], after["master_key"])
            self.assertNotEqual(before["final_keys"]["azw3"], after["final_keys"]["azw3"])

    def test_external_resource_and_title_changes_invalidate_master_and_final(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            copied_skill = base / "skill"
            needed = set((*pipeline.SEMANTIC_RECIPE_FILES, *pipeline.MASTER_RECIPE_FILES, *pipeline.FINAL_RECIPE_FILES))
            for relative in needed:
                source = SKILL_ROOT / relative
                target = copied_skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            runtime = base / "runtime"
            runtime.mkdir()
            (runtime / "runtime.json").write_text(
                json.dumps(
                    {
                        "runtime_name": "ebook-to-kindle",
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "components": {"core": {"calibre": {"version": "9.13.0"}}},
                    }
                ),
                encoding="utf-8",
            )
            image = base / "image.png"
            image.write_bytes(b"first-image")
            source = base / "first-title.html"
            source.write_text('<html><body><p>Enough authored source content for a cache plan.</p><img src="image.png"></body></html>', encoding="utf-8")
            request = pipeline.ConversionRequest(
                input_value=str(source), output_dir=base / "out", formats=("azw3",), runtime_root=runtime
            )
            before = pipeline._workspace_cache_plan(
                input_path=source, request=request, runtime_root=runtime, skill_root=copied_skill
            )
            image.write_bytes(b"second-image")
            resource_changed = pipeline._workspace_cache_plan(
                input_path=source, request=request, runtime_root=runtime, skill_root=copied_skill
            )
            self.assertNotEqual(before["master_key"], resource_changed["master_key"])
            renamed = base / "second-title.html"
            renamed.write_bytes(source.read_bytes())
            title_changed = pipeline._workspace_cache_plan(
                input_path=renamed, request=request, runtime_root=runtime, skill_root=copied_skill
            )
            self.assertNotEqual(resource_changed["master_key"], title_changed["master_key"])

    def test_exact_epub_hit_skips_runtime_tools_and_preserves_identical_bytes(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "runtime"
            runtime.mkdir()
            (runtime / "runtime.json").write_text(
                json.dumps(
                    {
                        "runtime_name": "ebook-to-kindle",
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "components": {"core": {"calibre": {"version": "9.13.0"}}},
                    }
                ),
                encoding="utf-8",
            )
            source = base / "book.txt"
            source.write_text("用于精确复用测试的正文内容足够长，不允许任何转换工具在命中后启动。", encoding="utf-8")
            output = base / "out"
            output.mkdir()
            request = pipeline.ConversionRequest(
                input_value=str(source),
                output_dir=output,
                formats=("epub",),
                runtime_root=runtime,
                workspace_root=base / "workspace",
            )
            store = self.workspace.BookWorkspace.open(base / "workspace")
            with store.locked():
                plan = pipeline._workspace_cache_plan(
                    input_path=source, request=request, runtime_root=runtime, skill_root=SKILL_ROOT
                )
                artifact = base / "cached.epub"
                with zipfile.ZipFile(artifact, "w") as archive:
                    archive.writestr("mimetype", "application/epub+zip")
                    archive.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"></package>')
                reference = store.put_file(artifact)
                quality = {"sha256": reference["sha256"], "bytes": reference["bytes"]}
                store.write_node(
                    "final",
                    plan["final_keys"]["epub"],
                    {
                        "contract": pipeline.CACHE_CONTRACT_VERSION,
                        "source_sha256": plan["source_sha256"],
                        "master_key": plan["master_key"],
                        "format": "epub",
                        "route": "fixture-exact",
                        "route_details": {"source_resources": {}},
                        "epubcheck": {"fatal": 0, "error": 0, "warning": 0},
                        "epub_structure": {"toc_entries": 1},
                        "quality": quality,
                        "artifact": reference,
                        "paperwhite2_manual_check_required": True,
                    },
                )
                with mock.patch.object(pipeline, "verify_runtime", side_effect=AssertionError("runtime must be skipped")), mock.patch.object(
                    pipeline, "_calibre_convert", side_effect=AssertionError("Calibre must be skipped")
                ), mock.patch.object(pipeline, "_run", side_effect=AssertionError("subprocess must be skipped")):
                    result = pipeline._convert_with_workspace_locked(request, SKILL_ROOT, store)
            self.assertEqual(result["details"]["workspace"]["hit_level"], "exact")
            self.assertEqual(result["details"]["runtime_verification"], "skipped-exact-hit")
            self.assertEqual((output / "book.epub").read_bytes(), artifact.read_bytes())

    def test_pdf_runtime_scope_is_full_for_stateless_and_cold_only(self) -> None:
        from ebook_kindle import pipeline

        pdf = Path("book.pdf")
        self.assertTrue(pipeline._requires_pdf_runtime(pdf, "disabled"))
        self.assertTrue(pipeline._requires_pdf_runtime(pdf, "cold"))
        self.assertFalse(pipeline._requires_pdf_runtime(pdf, "semantic"))
        self.assertFalse(pipeline._requires_pdf_runtime(pdf, "master"))
        self.assertFalse(pipeline._requires_pdf_runtime(pdf, "exact"))
        self.assertFalse(pipeline._requires_pdf_runtime(Path("book.epub"), "cold"))


class PdfAndHtmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from ebook_kindle import normalize
        from ebook_kindle import pdf

        cls.normalize = normalize
        cls.pdf = pdf

    def test_pdf_probe_policy(self) -> None:
        digital = {"sampled_pages": 3, "readable_pages": 3, "text_characters": 900}
        scan = {"sampled_pages": 3, "readable_pages": 0, "text_characters": 2}
        mixed = {"sampled_pages": 3, "readable_pages": 2, "text_characters": 900}
        self.assertEqual(self.pdf.classify_probe(digital), "digital")
        self.assertEqual(self.pdf.classify_probe(scan), "scan")
        self.assertEqual(self.pdf.classify_probe(mixed), "scan")
        cover_and_blank_pages = {
            "page_count": 298,
            "sampled_pages": 298,
            "readable_pages": 291,
            "selectable_pages": 295,
            "text_characters": 152003,
            "blank_pages": [116, 241],
            "image_only_pages": [1],
        }
        interior_image_page = {**cover_and_blank_pages, "image_only_pages": [120]}
        one_page_scan = {
            "page_count": 1,
            "sampled_pages": 1,
            "readable_pages": 0,
            "selectable_pages": 0,
            "text_characters": 0,
            "blank_pages": [],
            "image_only_pages": [1],
        }
        self.assertEqual(self.pdf.classify_probe(cover_and_blank_pages), "digital")
        self.assertEqual(self.pdf.classify_probe(interior_image_page), "scan")
        self.assertEqual(self.pdf.classify_probe(one_page_scan), "scan")
        forced = {"page_count": 12, "sampled_pages": 5, "readable_pages": 5, "text_characters": 5000}
        self.assertEqual(self.pdf.choose_pdf_mode(forced, "scan"), "scan")

    def test_pdf_note_fallback_recovers_authored_text_but_not_a_source_gap(self) -> None:
        spec = importlib.util.spec_from_file_location("ebook_pdf_backend_fallback", SCRIPTS / "pdf_backend.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        page_texts = [
            "正文引用 [1]、[612] 和源文件缺失定义的 [750]。",
            "\n".join(f"[{number}] authored note {number}" for number in range(1, 11)),
            "\n".join(f"[{number}] authored note {number}" for number in range(300, 310)),
            "\n".join(f"[{number}] authored note {number}" for number in range(610, 620)),
        ]
        recovered, expected = module._note_profile_from_page_texts(page_texts)
        self.assertIn(1, recovered)
        self.assertIn(612, recovered)
        self.assertIn("[612]", recovered[612])
        self.assertNotIn("[613]", recovered[612])
        self.assertNotIn(750, recovered)
        self.assertEqual(expected, {1: 1, 612: 1, 750: 1})

    def test_missing_outline_run_anchors_before_the_next_known_section(self) -> None:
        spec = importlib.util.spec_from_file_location("ebook_pdf_backend_test", SCRIPTS / "pdf_backend.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        items = [
            {"title": "第十二章", "page": None, "page_resolution": "unresolved"},
            {"title": "建设我们的未来", "page": None, "page_resolution": "unresolved"},
        ]
        module._assign_missing_outline_pages(items, [None, None], previous_page=185, next_page=189)
        self.assertEqual([item["page"] for item in items], [188, 188])
        self.assertEqual(
            [item["page_resolution"] for item in items],
            ["neighbor-next-page", "neighbor-next-page"],
        )

    def test_heading_normalization_and_furniture_restore_are_conservative(self) -> None:
        source = "<html><body><h3>第一章</h3><p>正文</p></body></html>"
        normalized = self.normalize.normalize_heading_levels(source)
        self.assertIn("<h1>第一章</h1>", normalized)
        furniture = ["序言", "12", "重复", "重复", "正文"]
        restored = self.normalize.select_unique_furniture(furniture, body_text="正文 第一章")
        self.assertEqual(restored, ["序言"])

    def test_visible_html_text_excludes_title_script_and_style(self) -> None:
        source = (
            "<html><head><title>VERY-LONG-FILENAME-THAT-MUST-NOT-COUNT</title>"
            "<style>fake words</style></head><body><script>more fake words</script>正文</body></html>"
        )
        self.assertEqual(self.normalize.html_text(source), "正文")

    def test_empty_source_cannot_pass_with_a_long_filename(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import meaningful_character_count, validate_source_text

        with tempfile.TemporaryDirectory() as outer:
            source = Path(outer) / "this-is-a-very-long-title-that-used-to-pass.txt"
            source.write_text("", encoding="utf-8")
            with self.assertRaises(SkillError) as caught:
                validate_source_text(self.normalize.source_body_text(source))
            self.assertEqual(caught.exception.code, "CONTENT_GATE_FAILED")
        self.assertEqual(meaningful_character_count("a-very-long-generated-title", ("a-very-long-generated-title",)), 0)

    def test_gb18030_text_and_html_meta_are_decoded_without_replacement(self) -> None:
        chinese = "这是用于旧版中文编码转换验证的正文内容，必须完整解码且不能出现替换字符。"
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            text_path = root / "legacy.txt"
            text_path.write_bytes(chinese.encode("gb18030"))
            self.assertEqual(self.normalize.source_body_text(text_path), chinese)
            html_path = root / "legacy.html"
            html = f'<html><head><meta charset="gb18030"></head><body>{chinese}</body></html>'
            html_path.write_bytes(html.encode("gb18030"))
            self.assertEqual(self.normalize.source_body_text(html_path), chinese)

    def test_markdown_visible_text_removes_syntax_but_keeps_authored_words(self) -> None:
        value = (
            "---\ntitle: 不可见元数据\n---\n\n# 标题\n\n- [链接文字](https://example.invalid) 和 `代码内容`\n\n"
            "[引用文字][ref] 与 [快捷引用]\n\n"
            "[ref]: https://example.invalid/reference\n"
            "[快捷引用]: https://example.invalid/shortcut\n\n"
            "| 列一 | 列二 |\n| --- | --- |\n| 单元格甲 | 单元格乙 |\n\n"
            "<table><tr><td>原始HTML单元格</td></tr></table>"
            "\n\n[^1]: 可见脚注文字"
        )
        visible = self.normalize.markdown_visible_text(value)
        self.assertIn("标题", visible)
        self.assertIn("链接文字", visible)
        self.assertIn("代码内容", visible)
        self.assertIn("引用文字", visible)
        self.assertIn("快捷引用", visible)
        self.assertIn("单元格甲", visible)
        self.assertIn("原始HTML单元格", visible)
        self.assertIn("可见脚注文字", visible)
        self.assertNotIn("不可见元数据", visible)
        self.assertNotIn("https://", visible)
        self.assertNotIn("# ", visible)
        self.assertNotIn("<table>", visible)
        self.assertNotIn("---", visible)

    def test_html_resources_are_local_complete_and_css_aware(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_source_resources

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            (root / "one.png").write_bytes(b"one")
            (root / "two.png").write_bytes(b"two")
            (root / "style.css").write_text("body{background:url(two.png);filter:url(#local-filter)}", encoding="utf-8")
            html = root / "book.html"
            html.write_text(
                '<link rel="stylesheet" href="style.css">'
                '<img srcset="data:image/png;base64,AAAA 1x, one.png 2x, two.png 3x">',
                encoding="utf-8",
            )
            summary = inspect_source_resources(html)
            self.assertEqual(summary["image_resources"], 3)
            self.assertEqual(summary["inline_resources"], 1)
            self.assertEqual(summary["missing_resources"], 0)
            html.write_text('<img src="https://example.invalid/private.png">', encoding="utf-8")
            with self.assertRaises(SkillError) as remote:
                inspect_source_resources(html)
            self.assertEqual(remote.exception.details["remote_resources"], 1)
            html.write_text('<img src="missing.png">', encoding="utf-8")
            with self.assertRaises(SkillError) as missing:
                inspect_source_resources(html)
            self.assertEqual(missing.exception.details["missing_resources"], 1)

    def test_markdown_shortcut_and_raw_html_images_are_preflighted(self) -> None:
        from ebook_kindle.gates import inspect_source_resources

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            (root / "one.png").write_bytes(b"one")
            (root / "two.png").write_bytes(b"two")
            markdown = root / "book.md"
            markdown.write_text(
                "![shortcut]\n\n[shortcut]: one.png\n\n<img src=\"two.png\">",
                encoding="utf-8",
            )
            self.assertEqual(inspect_source_resources(markdown)["image_resources"], 2)

    def test_data_srcset_cannot_hide_a_remote_second_candidate(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_source_resources
        from ebook_kindle.normalize import html_resource_references

        html = (
            '<html><body><p>Enough authored text for source validation.</p>'
            '<img srcset="data:image/png;base64,AAAA, https://example.invalid/remote.png 2x"/>'
            "</body></html>"
        )
        references = html_resource_references(html)
        self.assertIn(("https://example.invalid/remote.png", "image"), references)
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "remote-srcset.html"
            path.write_text(html, encoding="utf-8")
            with self.assertRaises(SkillError) as caught:
                inspect_source_resources(path)
            self.assertEqual(caught.exception.code, "CONTENT_GATE_FAILED")
            self.assertEqual(caught.exception.details["remote_resources"], 1)

    def test_legacy_html_background_and_frame_resources_fail_closed(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_source_resources

        html = (
            '<html><body background="https://example.invalid/background.png">'
            '<p>Enough authored text for source validation.</p>'
            '<frame src="https://example.invalid/frame.xhtml"></frame>'
            "</body></html>"
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "legacy-remote.html"
            path.write_text(html, encoding="utf-8")
            with self.assertRaises(SkillError) as caught:
                inspect_source_resources(path)
            self.assertEqual(caught.exception.code, "CONTENT_GATE_FAILED")
            self.assertEqual(caught.exception.details["remote_resources"], 2)

    def test_docx_absolute_relationship_cannot_escape_package_root(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_source_resources

        relationships = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="/../../evil.png"/></Relationships>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "escape.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/_rels/document.xml.rels", relationships)
                archive.writestr("../../evil.png", b"not-package-local")
            with self.assertRaises(SkillError):
                inspect_source_resources(path)

    def test_source_gate_rejects_excess_replacement_characters(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import validate_source_text

        with self.assertRaises(SkillError) as caught:
            validate_source_text("正常正文" * 10 + "�" * 10)
        self.assertEqual(caught.exception.code, "CONTENT_GATE_FAILED")

    def test_direct_source_semantics_count_headings_and_linked_notes(self) -> None:
        from ebook_kindle.gates import inspect_source_semantics

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            html = root / "book.html"
            html.write_text(
                '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><h1>第一章</h1><h2>第二节</h2>'
                '<p>正文<a epub:type="noteref" href="#n1">[1]</a></p>'
                '<aside id="n1" epub:type="footnote">注释</aside></body></html>',
                encoding="utf-8",
            )
            markdown = root / "book.md"
            markdown.write_text("# 第一章\n\n## 第二节\n\n正文[^n]\n\n[^n]: 注释\n", encoding="utf-8")
            text = root / "book.txt"
            text.write_text("第一章\n正文足够长\n第二章\n更多正文内容\n", encoding="utf-8")
            html_metrics = inspect_source_semantics(html)
            markdown_metrics = inspect_source_semantics(markdown)
            text_metrics = inspect_source_semantics(text)
        self.assertEqual(html_metrics["outline_entries"], 2)
        self.assertEqual(html_metrics["synthesized_noterefs"], 1)
        self.assertEqual(html_metrics["synthesized_footnotes"], 1)
        self.assertEqual(markdown_metrics["outline_entries"], 2)
        self.assertEqual(markdown_metrics["synthesized_noterefs"], 1)
        self.assertEqual(markdown_metrics["synthesized_footnotes"], 1)
        self.assertEqual(text_metrics["outline_entries"], 2)

    def test_plain_text_chapters_are_materialized_as_semantic_html_headings(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            source = Path(outer) / "book.txt"
            destination = Path(outer) / "book.html"
            source.write_text("第一章\n第一段正文。\n\n第二章\n第二段正文。\n", encoding="utf-8")
            metrics = self.normalize.plain_text_to_semantic_html(source, destination, "测试书")
            html = destination.read_text(encoding="utf-8")
        self.assertEqual(metrics["heading_entries"], 2)
        self.assertEqual(html.count("<h1>"), 2)
        self.assertLess(html.index("第一章"), html.index("第一段正文"))
        self.assertLess(html.index("第一段正文"), html.index("第二章"))

    def test_blank_scan_html_title_cannot_satisfy_ocr_gate(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import validate_ocr_text

        body = self.normalize.html_text("<html><head><title>long generated filename title</title></head><body></body></html>")
        with self.assertRaises(SkillError) as caught:
            validate_ocr_text(body, 2)
        self.assertEqual(caught.exception.code, "OCR_QUALITY_FAILED")

    def test_recovered_page_end_content_stays_after_main_body_content(self) -> None:
        source = "<html><body><p>RIGHT-R3</p></body></html>"
        restored = self.normalize.inject_trailing_furniture(source, ["D-Z9", "END-DIGITAL-PDF"])
        self.assertLess(restored.index("RIGHT-R3"), restored.index("D-Z9"))
        self.assertLess(restored.index("D-Z9"), restored.index("END-DIGITAL-PDF"))
        self.assertLess(restored.index("END-DIGITAL-PDF"), restored.lower().index("</body>"))


class ContainerGateTests(unittest.TestCase):
    def _write_semantic_epub(
        self,
        path: Path,
        *,
        broken_toc: bool = False,
        broken_note: bool = False,
        toc_table: bool = False,
        body_printed_toc: bool = False,
        landmark: bool = True,
    ) -> None:
        container = (
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine toc="ncx"><itemref idref="chapter"/></spine></package>'
        )
        toc_target = "missing.xhtml#missing" if broken_toc else "chapter.xhtml#start"
        toc_body = (
            f'<table><tr><td><a href="{toc_target}">Chapter</a></td></tr></table>'
            if toc_table
            else f'<ol><li><a href="{toc_target}">Chapter</a></li></ol>'
        )
        landmark_nav = (
            '<nav epub:type="landmarks"><ol><li>'
            '<a epub:type="bodymatter" href="chapter.xhtml#start">正文</a>'
            '</li></ol></nav>'
            if landmark
            else ""
        )
        nav = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            f'<nav epub:type="toc">{toc_body}</nav>'
            f"{landmark_nav}</body></html>"
        )
        ncx = (
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="n1">'
            f'<navLabel><text>Chapter</text></navLabel><content src="{toc_target}"/>'
            '</navPoint></navMap></ncx>'
        )
        note_target = "#missing-note" if broken_note else "#note-1"
        printed_body = (
            '<table><tr><td>第三章 工程学的价值</td><td>033</td></tr>'
            '<tr><td>第四章 简化沟通</td><td>066</td></tr>'
            '<tr><td>第五章 精简为王</td><td>070</td></tr>'
            '<tr><td>第七章 继续前进</td><td>081</td></tr></table>'
            if body_printed_toc
            else ""
        )
        ordinary_data_table = (
            '<table><tr><th>普通数据</th><th>值</th></tr>'
            '<tr><td>甲</td><td>10</td></tr><tr><td>乙</td><td>20</td></tr><tr><td>丙</td><td>30</td></tr></table>'
        )
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<h1 id="start">Chapter</h1>'
            f'<p>Enough authored chapter text with a note <a id="ref-1" epub:type="noteref" href="{note_target}">[1]</a>.</p>'
            '<aside id="note-1" epub:type="footnote"><p>Note body <a href="#ref-1">返回正文</a></p></aside>'
            f"{printed_body}{ordinary_data_table}"
            '</body></html>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OPS/content.opf", opf)
            archive.writestr("OPS/nav.xhtml", nav)
            archive.writestr("OPS/toc.ncx", ncx)
            archive.writestr("OPS/chapter.xhtml", chapter)

    def _write_epub(self, path: Path, image_href: str) -> None:
        container = (
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/pkg/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
            '<item id="nav" href="../nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="chapter" href="../text/chapter.xhtml" media-type="application/xhtml+xml"/>'
            f'<item id="image" href="{image_href}" media-type="image/png"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>'
        )
        nav = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="text/chapter.xhtml">Chapter</a></nav></body></html>'
        chapter = '<html xmlns="http://www.w3.org/1999/xhtml"><body>Enough authored chapter text for validation.</body></html>'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OPS/pkg/content.opf", opf)
            archive.writestr("OPS/nav.xhtml", nav)
            archive.writestr("OPS/text/chapter.xhtml", chapter)
            archive.writestr("OPS/images/cover.png", b"image")

    def _write_cover_epub(self, path: Path, *, metadata_cover: bool, guide_cover: bool) -> None:
        container = (
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )
        metadata = '<metadata><meta name="cover" content="cover-image"/></metadata>' if metadata_cover else "<metadata/>"
        guide = '<guide><reference type="cover" href="cover.xhtml"/></guide>' if guide_cover else ""
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            f"{metadata}<manifest>"
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover-image" href="images/cover.png" media-type="image/png"/>'
            '<item id="content-image" href="images/content.png" media-type="image/png"/>'
            f'</manifest><spine><itemref idref="chapter"/></spine>{guide}</package>'
        )
        nav = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="chapter.xhtml">Chapter</a></nav></body></html>'
        cover = '<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="images/cover.png"/></body></html>'
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>Enough authored content for the guide cover classification fixture.</p>'
            '<img src="images/content.png"/></body></html>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OPS/content.opf", opf)
            archive.writestr("OPS/nav.xhtml", nav)
            archive.writestr("OPS/cover.xhtml", cover)
            archive.writestr("OPS/chapter.xhtml", chapter)
            archive.writestr("OPS/images/cover.png", b"cover")
            archive.writestr("OPS/images/content.png", b"content")

    def _write_remote_content_epub(self, path: Path, *, chapter: str, css: str = "", remote_property: bool = False) -> None:
        container = (
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )
        chapter_properties = ' properties="remote-resources"' if remote_property else ""
        css_item = '<item id="style" href="style.css" media-type="text/css"/>' if css else ""
        opf = (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            f'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"{chapter_properties}/>'
            f"{css_item}</manifest><spine><itemref idref=\"chapter\"/></spine></package>"
        )
        nav = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="chapter.xhtml">Chapter</a></nav></body></html>'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OPS/content.opf", opf)
            archive.writestr("OPS/nav.xhtml", nav)
            archive.writestr("OPS/chapter.xhtml", chapter)
            if css:
                archive.writestr("OPS/style.css", css)

    def test_master_baseline_uses_visible_epub_spine_without_a_calibre_subprocess(self) -> None:
        from ebook_kindle import pipeline

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            master = Path(outer) / "master.epub"
            baseline_path = Path(outer) / "baseline.txt"
            self._write_semantic_epub(master)
            baseline = pipeline._extract_master_baseline(master, baseline_path)
            self.assertIn("Enough authored chapter text", baseline)
            self.assertNotIn("ChapterChapter", baseline)
            self.assertEqual(baseline_path.read_text(encoding="utf-8"), baseline)
            self.assertEqual(stat.S_IMODE(baseline_path.stat().st_mode), 0o600)

    def test_chinese_hard_wraps_and_typography_are_normalized_fail_closed(self) -> None:
        from ebook_kindle import normalize

        safe_css = (
            "body{font-size:1em;line-height:1.5}"
            "h1{font-size:1.5em;line-height:1.25}"
            ".kindle-noteref{font-size:.75em;line-height:1.2;vertical-align:super}"
            ".kindle-toc-anchor{font-size:0;line-height:0}"
        )
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="style.css"/></head><body>'
            '<h1 id="chapter-anchor">一种全新的汽车公 司</h1>'
            '<p class="body">这是第一段在纸页末尾被</p><p class="body">错误截断的中文内容。</p>'
            '<p class="body">这是一段长度足够用于测试中文硬换<br/>行规范化的正文内容。</p>'
            '<h5 class="heading">你能从信息论的角度解释</h5><h5 class="heading">PayPal 的理念吗？</h5>'
            '<ul><li class="item">工程课程包括基础工</li><li class="item">程学。</li></ul>'
            '<ul><li><a href="#chapter-anchor">第一章：结构</a></li><li><a href="#chapter-anchor">第二章：排版</a></li></ul>'
            '<p class="body">中文 字 号 ，不应被纸面空格拆散。</p>'
            '<aside><p class="body">脚注结构不应被</p><p class="body">跨块合并。</p></aside>'
            '<pre>预 格 式</pre></body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "readability.epub"
            self._write_remote_content_epub(path, chapter=chapter, css=safe_css)
            changed = normalize.normalize_epub_readability(path)
            inspected = normalize.inspect_epub_readability(path)
            with zipfile.ZipFile(path) as archive:
                normalized = archive.read("OPS/chapter.xhtml").decode("utf-8")
        self.assertGreaterEqual(changed["cjk_paragraph_fragments_merged"], 1)
        self.assertEqual(changed["cjk_heading_fragments_merged"], 1)
        self.assertEqual(changed["cjk_list_fragments_merged"], 1)
        self.assertEqual(changed["cjk_hard_breaks_removed"], 1)
        self.assertGreater(changed["cjk_spacing_removed"], 0)
        self.assertIn("被错误截断", normalized)
        self.assertIn("中文字号，", normalized)
        self.assertIn('id="chapter-anchor"', normalized)
        self.assertIn("一种全新的汽车公司", normalized)
        self.assertIn("预 格 式", normalized)
        self.assertIn("脚注结构不应被", normalized)
        self.assertIn("跨块合并", normalized)
        self.assertEqual(normalized.count('href="#chapter-anchor"'), 2)
        for key, value in inspected.items():
            self.assertEqual(value, 0, (key, inspected))

    def test_readability_inspector_rejects_absolute_or_inline_typography(self) -> None:
        from ebook_kindle import normalize
        from ebook_kindle import pipeline
        from ebook_kindle.core import SkillError

        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="style.css"/></head>'
            '<body><p style="font-size:8pt">这是足够长的中文正文，用来验证异常字号能够被确定性识别。</p></body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "unsafe-typography.epub"
            self._write_remote_content_epub(
                path,
                chapter=chapter,
                css="p{font-family:serif;font-size:9pt;line-height:.8}",
            )
            metrics = normalize.inspect_epub_readability(path)
            with self.assertRaises(SkillError):
                pipeline._validate_readability(path, error_code="EPUB_INVALID")
            roundtrip_scope = pipeline._validate_readability(
                path,
                error_code="KINDLE_ROUNDTRIP_FAILED",
                typography=False,
            )
        self.assertGreater(metrics["unsafe_font_size_declarations"], 0)
        self.assertGreater(metrics["font_family_declarations"], 0)
        self.assertGreater(metrics["unsafe_line_height_declarations"], 0)
        self.assertGreater(metrics["inline_typography_declarations"], 0)
        self.assertEqual(roundtrip_scope["typography_contract_checked"], 0)
        self.assertNotIn("unsafe_font_size_declarations", roundtrip_scope)

    def test_readability_normalizer_drops_web_favicon_links(self) -> None:
        from ebook_kindle import normalize

        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<link href="cover.jpg" rel="icon" type="image/x-cover"/>'
            '<link rel="stylesheet" href="style.css"/></head>'
            '<body><p>Project Gutenberg chapters link the cover as a favicon.</p></body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "favicon.epub"
            self._write_remote_content_epub(path, chapter=chapter, css="body{font-size:1em}")
            changed = normalize.normalize_epub_readability(path)
            with zipfile.ZipFile(path) as archive:
                normalized = archive.read("OPS/chapter.xhtml").decode("utf-8")
        self.assertEqual(changed["icon_links_removed"], 1)
        self.assertNotIn('rel="icon"', normalized)
        self.assertIn('rel="stylesheet"', normalized)

    def test_bilingual_cascade_rechecks_the_preceding_cjk_boundary(self) -> None:
        from ebook_kindle import normalize

        safe_css = "body{font-size:1em;line-height:1.5}"
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="style.css"/></head><body>'
            '<p class="body">加热器：霜冻来临前点燃加热器</p>'
            '<p class="body">Heaters: use it before the frost hits.</p>'
            '<p class="body">风机：温暖的空气带到地面，避免温度降低至零度以下</p>'
            '</body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "bilingual-cascade.epub"
            self._write_remote_content_epub(path, chapter=chapter, css=safe_css)
            before = normalize.inspect_epub_readability(path)
            changed = normalize.normalize_epub_readability(path)
            after = normalize.inspect_epub_readability(path)
            with zipfile.ZipFile(path) as archive:
                normalized = archive.read("OPS/chapter.xhtml").decode("utf-8")
        self.assertEqual(before["suspicious_cjk_block_breaks"], 1)
        self.assertEqual(changed["cjk_paragraph_fragments_merged"], 2)
        self.assertIn("加热器：霜冻来临前点燃加热器 Heaters", normalized)
        self.assertIn("hits. 风机：温暖的空气", normalized)
        self.assertEqual(after["suspicious_cjk_block_breaks"], 0)

    def test_mobi_version_uses_the_file_version_field(self) -> None:
        from ebook_kindle.gates import mobi_version

        payload = bytearray(256)
        payload[60:68] = b"BOOKMOBI"
        payload[78:82] = struct.pack(">I", 128)
        payload[144:148] = b"MOBI"
        payload[164:168] = struct.pack(">I", 8)
        payload[180:184] = struct.pack(">I", 0xFFFFFFFF)
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "sample.azw3"
            path.write_bytes(payload)
            self.assertEqual(mobi_version(path), 8)

    def test_semantic_epub_counts_working_toc_landmark_and_bidirectional_note(self) -> None:
        from ebook_kindle.gates import inspect_epub_structure, validate_semantic_retention

        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "semantic.epub"
            self._write_semantic_epub(path)
            summary = inspect_epub_structure(path)
        self.assertEqual(summary["toc_entries"], 1)
        self.assertEqual(summary["nav_toc_entries"], 1)
        self.assertEqual(summary["ncx_toc_entries"], 1)
        self.assertEqual(summary["broken_navigation_targets"], 0)
        self.assertEqual(summary["toc_table_count"], 0)
        self.assertEqual(summary["body_printed_toc_table_count"], 0)
        self.assertEqual(summary["bodymatter_landmarks"], 1)
        self.assertEqual(summary["noteref_count"], 1)
        self.assertEqual(summary["resolved_noteref_count"], 1)
        self.assertEqual(summary["bidirectional_note_count"], 1)
        retained = validate_semantic_retention(
            {"outline_entries": 1, "synthesized_noterefs": 1, "synthesized_footnotes": 1},
            summary,
            require_ncx=True,
        )
        self.assertEqual(retained["required_toc_entries"], 1)
        self.assertEqual(retained["retained_bodymatter_landmarks"], 1)

    def test_start_location_is_materialized_and_required(self) -> None:
        from ebook_kindle import normalize
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_epub_structure, validate_semantic_retention

        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "missing-start.epub"
            self._write_semantic_epub(path, landmark=False)
            before = inspect_epub_structure(path)
            self.assertEqual(before["bodymatter_landmarks"], 0)
            with self.assertRaises(SkillError):
                validate_semantic_retention({"outline_entries": 1}, before)
            normalize.ensure_epub_start_location(path)
            after = inspect_epub_structure(path)
        self.assertGreaterEqual(after["bodymatter_landmarks"], 1)

    def test_semantic_retention_rejects_lost_ncx_or_bidirectional_note(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import validate_semantic_retention

        expected = {"outline_entries": 3, "synthesized_noterefs": 2, "synthesized_footnotes": 2}
        broken = {
            "toc_entries": 3,
            "valid_ncx_toc_entries": 0,
            "noteref_count": 2,
            "resolved_noteref_count": 2,
            "bidirectional_note_count": 1,
            "footnote_count": 2,
        }
        with self.assertRaises(SkillError) as caught:
            validate_semantic_retention(expected, broken, require_ncx=True)
        self.assertEqual(caught.exception.code, "EPUB_INVALID")

    def test_semantic_epub_rejects_broken_toc_note_and_table_navigation(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_epub_structure

        cases = (
            {"broken_toc": True},
            {"broken_note": True},
            {"toc_table": True},
            {"body_printed_toc": True},
        )
        with tempfile.TemporaryDirectory() as outer:
            for index, values in enumerate(cases):
                path = Path(outer) / f"broken-{index}.epub"
                self._write_semantic_epub(path, **values)
                with self.subTest(values=values):
                    with self.assertRaises(SkillError) as caught:
                        inspect_epub_structure(path)
                    self.assertEqual(caught.exception.code, "EPUB_INVALID")

    def test_pdf_semantic_enrichment_maps_outline_removes_printed_toc_and_links_notes(self) -> None:
        from ebook_kindle import pipeline
        from ebook_kindle.core import build_job_env
        from ebook_kindle.gates import inspect_epub_structure
        from ebook_kindle.runtime import default_runtime_root, runtime_paths

        runtime_root = default_runtime_root()
        paths = runtime_paths(runtime_root)
        pdf_python = paths["pdf_python"]
        if not pdf_python.is_file():
            self.skipTest("PDF runtime is not installed")
        references = " ".join(f"正文引用[{index}]" for index in range(1, 26))
        definitions = "<ol>" + "".join(
            f"<li>[{index}] 第 {index} 条注释内容。</li>" for index in range(1, 26)
        ) + "</ol>"
        html = (
            '<html><head><style>body{max-width:800px;margin:0 auto;padding:2rem;box-shadow:1px 1px}</style></head>'
            '<body><div class="page"><table><tr><td>第一章</td><td>第二节</td></tr></table>'
            '<h2>第一章</h2><p>' + references + '</p><h3>第二节</h3>'
            '<ol><li>列表章节正文</li></ol>' + definitions +
            '<table><tr><td>第三章 工程学的价值</td><td>033</td></tr>'
            '<tr><td>第四章 简化沟通</td><td>066</td></tr>'
            '<tr><td>第五章 精简为王</td><td>070</td></tr>'
            '<tr><td>第七章 继续前进</td><td>081</td></tr></table>'
            '<table><tr><th>普通数据</th><th>值</th></tr><tr><td>甲</td><td>10</td></tr>'
            '<tr><td>乙</td><td>20</td></tr><tr><td>丙</td><td>30</td></tr></table>'
            '<p>该页正文起点</p>'
            '</div></body></html>'
        )
        document = {
            "texts": [
                {"text": "第一章", "label": "section_header", "prov": [{"page_no": 2}]},
                {"text": "第二节", "label": "section_header", "prov": [{"page_no": 3}]},
                {"text": "列表章节正文", "label": "text", "prov": [{"page_no": 4}]},
                {"text": "该页正文起点", "label": "text", "prov": [{"page_no": 5}]},
            ]
        }
        outline = [
            {"title": "第一章", "level": 0, "page": 2},
            {"title": "第二节", "level": 1, "page": 3},
            {"title": "列表章节", "level": 0, "page": 4},
            {"title": "源文缺失的章节标题", "level": 0, "page": 5, "page_resolution": "interpolated"},
        ]
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            source = root / "source.html"
            docling = root / "document.json"
            outline_path = root / "outline.json"
            output = root / "normalized.html"
            report = root / "semantics.json"
            source.write_text(html, encoding="utf-8")
            docling.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [
                    str(pdf_python),
                    "-I",
                    str(SCRIPTS / "pdf_semantics.py"),
                    "--html",
                    str(source),
                    "--document",
                    str(docling),
                    "--outline",
                    str(outline_path),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            enriched = output.read_text(encoding="utf-8")
            metrics = json.loads(report.read_text(encoding="utf-8"))
            job = root / "job"
            job.mkdir(mode=0o700)
            env = build_job_env(job, runtime_root)
            master = root / "master.epub"
            pipeline._calibre_convert(
                paths,
                output,
                master,
                env,
                root,
                300,
                pipeline.kindle_master_options(SKILL_ROOT / "assets" / "pw2.css", "Semantic fixture"),
            )
            pipeline._epubcheck(paths, master, root / "epubcheck.json", env, root, 300)
            structure = inspect_epub_structure(master)
        self.assertEqual(enriched.lower().count("<table"), 1)
        self.assertIn("普通数据", enriched)
        self.assertNotIn("第三章 工程学的价值", enriched)
        self.assertNotIn("max-width:800px", enriched.replace(" ", "").lower())
        self.assertEqual(enriched.count("kindle-toc-level-1"), 3)
        self.assertEqual(enriched.count("kindle-toc-level-2"), 1)
        self.assertEqual(enriched.count('epub:type="noteref"'), 25)
        self.assertEqual(enriched.count('epub:type="footnote"'), 25)
        self.assertEqual(metrics["outline_entries"], 4)
        self.assertEqual(metrics["mapped_outline_entries"], 4)
        self.assertEqual(metrics["generated_outline_anchors"], 2)
        self.assertEqual(metrics["title_matched_outline_entries"], 3)
        self.assertEqual(metrics["page_start_outline_anchors"], 1)
        self.assertEqual(metrics["interpolated_outline_entries"], 1)
        self.assertEqual(metrics["printed_toc_tables_removed"], 2)
        self.assertEqual(metrics["remaining_printed_toc_tables"], 0)
        self.assertEqual(metrics["synthesized_noterefs"], 25)
        self.assertEqual(metrics["synthesized_footnotes"], 25)
        self.assertEqual(structure["toc_entries"], 4)
        self.assertEqual(structure["noteref_count"], 25)
        self.assertEqual(structure["bidirectional_note_count"], 25)

    def test_pdf_semantics_links_split_endnote_regions_and_leaves_no_raw_references(self) -> None:
        from ebook_kindle.runtime import default_runtime_root, runtime_paths

        paths = runtime_paths(default_runtime_root())
        pdf_python = paths["pdf_python"]
        if not pdf_python.is_file():
            self.skipTest("PDF runtime is not installed")
        early = (
            "<table><tr><td><ul><li>[1] isolated first note</li></ul></td></tr></table>"
            + "<p>note section separator</p>" * 6
            + "<ol>"
            + "".join(f"<li>[{number}] early note {number}</li>" for number in range(2, 67))
            + "</ol>"
        )
        packed = "<pre><code>" + " ".join(
            f"[{number}] packed note {number}" for number in range(67, 91)
        ) + "</code></pre>"
        later = "<ol>" + "".join(
            f"<li>[{number}] later note {number}</li>" for number in range(147, 221)
        ) + "</ol><p>[439] final note 439</p><p>[1041] final note 1041</p>"
        html = (
            "<html><body><span>直属引用[1][1]</span><h5>标题引用[12]</h5>"
            "<pre>预格式引用[37]</pre><div>直属文本引用[66]</div><p>段落引用[150]。</p>"
            "<aside class=\"callout\"><p>侧栏引用[ 439 ]</p></aside>"
            '<p><a href="#legacy"><span>[612]</span></a>和缺失源注释[750]</p>'
            + early
            + packed
            + later
            + "</body></html>"
        )
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            source = root / "source.html"
            document = root / "document.json"
            outline = root / "outline.json"
            output = root / "semantic.html"
            report = root / "semantics.json"
            fallback = root / "fallback.json"
            expected = root / "expected.json"
            source.write_text(html, encoding="utf-8")
            document.write_text('{"texts": []}', encoding="utf-8")
            outline.write_text("[]", encoding="utf-8")
            fallback.write_text(
                '{"1": "[1] isolated first note", "612": "[612] recovered PDF note"}',
                encoding="utf-8",
            )
            expected.write_text(
                '{"1":1,"12":1,"37":1,"66":1,"150":1,"439":1,"612":1,"750":1}',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(pdf_python),
                    "-I",
                    str(SCRIPTS / "pdf_semantics.py"),
                    "--html",
                    str(source),
                    "--document",
                    str(document),
                    "--outline",
                    str(outline),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                    "--fallback-definitions",
                    str(fallback),
                    "--expected-reference-counts",
                    str(expected),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            enriched = output.read_text(encoding="utf-8")
            metrics = json.loads(report.read_text(encoding="utf-8"))
        for number in (1, 12, 37, 66, 150, 439, 612, 750):
            self.assertIn(f'id="kindle-noteref-{number}-1"', enriched)
            self.assertIn(f'id="kindle-note-{number}"', enriched)
        self.assertNotIn('href="#legacy"', enriched)
        self.assertEqual(metrics["synthesized_noterefs"], 8)
        self.assertEqual(metrics["synthesized_footnotes"], 168)
        self.assertEqual(metrics["recovered_pdf_footnotes"], 1)
        self.assertEqual(metrics["placeholder_footnotes"], 1)
        self.assertEqual(metrics["source_body_noteref_expectations"], 8)
        self.assertEqual(metrics["source_reference_mismatches"], 0)
        self.assertEqual(metrics["deduplicated_noteref_markers"], 1)
        self.assertEqual(metrics["unresolved_noteref_markers"], 0)

    def test_pdf_semantic_gate_rejects_matching_raw_note_markers(self) -> None:
        from ebook_kindle import pipeline
        from ebook_kindle.core import SkillError

        metrics = {
            "outline_entries": 69,
            "mapped_outline_entries": 69,
            "remaining_printed_toc_tables": 0,
            "unresolved_noteref_markers": 1,
        }
        with self.assertRaises(SkillError) as caught:
            pipeline._validate_pdf_semantic_metrics(metrics)
        self.assertEqual(caught.exception.code, "EPUB_INVALID")
        self.assertEqual(caught.exception.details["unresolved_noteref_markers"], 1)
        metrics["unresolved_noteref_markers"] = 0
        metrics["source_reference_mismatches"] = 2
        with self.assertRaises(SkillError) as mismatch:
            pipeline._validate_pdf_semantic_metrics(metrics)
        self.assertEqual(mismatch.exception.code, "EPUB_INVALID")
        self.assertEqual(mismatch.exception.details["source_reference_mismatches"], 2)

    def test_ocr_quality_rejects_an_empty_page(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import docling_page_character_counts, validate_ocr_text

        document = {
            "texts": [
                {
                    "label": "text",
                    "text": "A" * 300,
                    "prov": [{"page_no": 1, "charspan": [0, 300]}],
                }
            ],
            "tables": [],
        }
        counts = docling_page_character_counts(document, 2)
        self.assertEqual(counts, [300, 0])
        with self.assertRaises(SkillError) as caught:
            validate_ocr_text("A" * 300, 2, counts)
        self.assertEqual(caught.exception.code, "OCR_QUALITY_FAILED")
        self.assertEqual(caught.exception.details["min_page_characters"], 0)
        self.assertEqual(caught.exception.details["page_coverage_ratio"], 0.5)

    def test_ocr_quality_allows_verified_blank_and_sparse_pages(self) -> None:
        from ebook_kindle.gates import validate_ocr_text

        result = validate_ocr_text(
            "A" * 45,
            3,
            [41, 0, 4],
            page_minimums=[15, 0, 4],
        )
        self.assertEqual(result["page_coverage_ratio"], 1.0)
        self.assertEqual(result["blank_page_exemptions"], 1)
        self.assertEqual(result["sparse_page_thresholds"], 1)

    def test_ocr_page_gate_counts_table_only_page_text(self) -> None:
        from ebook_kindle.gates import docling_page_character_counts, validate_ocr_text

        document = {
            "texts": [
                {"label": "text", "text": "A" * 20, "prov": [{"page_no": 1, "charspan": [0, 20]}]},
                {"label": "page_footer", "text": "ignored footer", "prov": [{"page_no": 2}]},
            ],
            "tables": [
                {
                    "prov": [{"page_no": 2}],
                    "data": {"table_cells": [{"text": "B" * 20}]},
                }
            ],
        }
        counts = docling_page_character_counts(document, 2)
        self.assertEqual(counts, [20, 20])
        result = validate_ocr_text("A" * 20 + "B" * 20, 2, counts)
        self.assertEqual(result["page_coverage_ratio"], 1.0)
        self.assertEqual(result["min_page_characters"], 20)

    def test_ocr_page_gate_excludes_furniture_and_ambiguous_cross_page_text(self) -> None:
        from ebook_kindle.gates import docling_page_character_counts

        document = {
            "texts": [
                {
                    "label": "text",
                    "content_layer": "furniture",
                    "text": "header long enough to fake coverage",
                    "prov": [{"page_no": 1}],
                },
                {
                    "label": "text",
                    "text": "ambiguous cross page item long enough to fake both pages",
                    "prov": [{"page_no": 1}, {"page_no": 2}],
                },
            ],
            "tables": [
                {
                    "label": "table",
                    "content_layer": "furniture",
                    "prov": [{"page_no": 2}],
                    "data": {"table_cells": [{"text": "furniture table long enough to fake coverage"}]},
                }
            ],
        }
        self.assertEqual(docling_page_character_counts(document, 2), [0, 0])

    def test_roundtrip_gate_rejects_reordered_content(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import compare_content

        sections = [f"section-{index:03d}-unique-content-marker" for index in range(120)]
        baseline = " ".join(sections)
        candidate = " ".join([*sections[60:], *sections[:60]])
        with self.assertRaises(SkillError) as caught:
            compare_content(baseline, candidate)
        self.assertEqual(caught.exception.code, "KINDLE_ROUNDTRIP_FAILED")
        self.assertGreater(caught.exception.details["marker_ratio"], 0.7)
        self.assertLess(caught.exception.details["ordered_marker_ratio"], 0.7)

    def test_roundtrip_gate_rejects_replacement_character_corruption(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import compare_content

        baseline = " ".join(f"stable-section-{index:03d}" for index in range(120))
        candidate = baseline + (" �" * 80)
        with self.assertRaises(SkillError) as caught:
            compare_content(baseline, candidate)
        self.assertEqual(caught.exception.code, "KINDLE_ROUNDTRIP_FAILED")
        self.assertGreater(caught.exception.details["roundtrip_replacement_ratio"], 0.01)

    def test_roundtrip_gate_rejects_whole_book_duplication(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import compare_content

        baseline = " ".join(f"stable-section-{index:03d}-unique-body" for index in range(120))
        candidate = baseline + " " + baseline
        with self.assertRaises(SkillError) as caught:
            compare_content(baseline, candidate)
        self.assertGreater(caught.exception.details["length_ratio"], 1.9)
        self.assertGreater(caught.exception.details["marker_occurrence_ratio"], 1.5)

    def test_roundtrip_gate_rejects_quarter_loss_hidden_by_duplicate_padding(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import compare_content

        sections = [f"section-{index:03d}-unique-content-marker" for index in range(120)]
        baseline = " ".join(sections)
        candidate = " ".join([*sections[:45], *sections[75:], *sections[:30]])
        with self.assertRaises(SkillError) as caught:
            compare_content(baseline, candidate)
        self.assertTrue(
            caught.exception.details["marker_ratio"] < 0.95
            or caught.exception.details["ordered_marker_ratio"] < 0.95
        )

    def test_epub_parent_relative_resources_resolve_and_remote_manifest_fails(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_epub_structure

        with tempfile.TemporaryDirectory() as outer:
            valid = Path(outer) / "valid.epub"
            self._write_epub(valid, "../images/cover.png")
            summary = inspect_epub_structure(valid)
            self.assertEqual(summary["image_resources"], 1)
            self.assertEqual(summary["remote_resources"], 0)
            remote = Path(outer) / "remote.epub"
            self._write_epub(remote, "https://example.invalid/cover.png")
            with self.assertRaises(SkillError) as caught:
                inspect_epub_structure(remote)
            self.assertEqual(caught.exception.code, "EPUB_INVALID")
            self.assertEqual(caught.exception.details["remote_resources"], 1)

    def test_epub_remote_resources_property_and_spine_image_fail_closed(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_epub_structure

        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>Enough authored chapter text for validation.</p>'
            '<img src="https://example.invalid/remote.png"/></body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "remote-spine.epub"
            self._write_remote_content_epub(path, chapter=chapter, remote_property=True)
            with self.assertRaises(SkillError) as caught:
                inspect_epub_structure(path)
            self.assertEqual(caught.exception.code, "EPUB_INVALID")
            self.assertGreaterEqual(caught.exception.details["remote_resources"], 1)

    def test_epub_remote_css_url_fails_closed(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import inspect_epub_structure

        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<link rel="stylesheet" href="style.css"/></head>'
            '<body><p>Enough authored chapter text for validation.</p></body></html>'
        )
        with tempfile.TemporaryDirectory() as outer:
            path = Path(outer) / "remote-css.epub"
            self._write_remote_content_epub(
                path,
                chapter=chapter,
                css='body { background-image: url("https://example.invalid/remote.png"); }',
            )
            with self.assertRaises(SkillError) as caught:
                inspect_epub_structure(path)
            self.assertEqual(caught.exception.code, "EPUB_INVALID")
            self.assertGreaterEqual(caught.exception.details["remote_resources"], 1)

    def test_source_to_master_image_count_cannot_shrink(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import validate_image_retention

        with self.assertRaises(SkillError):
            validate_image_retention({"image_resources": 2}, {"image_resources": 1})

    def test_generated_cover_cannot_compensate_for_a_lost_content_image(self) -> None:
        from ebook_kindle.core import SkillError
        from ebook_kindle.gates import validate_image_retention

        source = {"image_resources": 1, "content_image_resources": 1, "cover_image_resources": 0}
        cover_only = {"image_resources": 1, "content_image_resources": 0, "cover_image_resources": 1}
        with self.assertRaises(SkillError):
            validate_image_retention(source, cover_only)
        existing_epub = {"image_resources": 1, "content_image_resources": 0, "cover_image_resources": 1}
        output_without_cover = {"image_resources": 0, "content_image_resources": 0, "cover_image_resources": 0}
        with self.assertRaises(SkillError):
            validate_image_retention(existing_epub, output_without_cover)

    def test_epub2_guide_cover_page_and_metadata_cover_classify_the_same_image(self) -> None:
        from ebook_kindle.gates import inspect_epub_structure, validate_image_retention

        with tempfile.TemporaryDirectory() as outer:
            guide_source = Path(outer) / "guide.epub"
            metadata_roundtrip = Path(outer) / "metadata.epub"
            self._write_cover_epub(guide_source, metadata_cover=False, guide_cover=True)
            self._write_cover_epub(metadata_roundtrip, metadata_cover=True, guide_cover=False)
            source = inspect_epub_structure(guide_source)
            roundtrip = inspect_epub_structure(metadata_roundtrip)
            self.assertEqual(source["cover_image_resources"], 1)
            self.assertEqual(source["content_image_resources"], 1)
            self.assertEqual(roundtrip["cover_image_resources"], 1)
            self.assertEqual(roundtrip["content_image_resources"], 1)
            validate_image_retention(source, roundtrip)


class LocalizationTests(unittest.TestCase):
    @staticmethod
    def _write_note_epub(path: Path) -> None:
        container = (
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>'
        )
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="chapter"/><itemref idref="notes"/></spine>'
            '<guide><reference type="text" title="Start" href="chapter.xhtml#start"/></guide>'
            '</package>'
        )
        chapter = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body><p id="start">Enough authored body text '
            '<a epub:type="noteref" id="ref-1" href="notes.xhtml#note-1">1</a> and another reference '
            '<a epub:type="noteref" id="ref-2" href="notes.xhtml#note-2">2</a>.</p></body></html>'
        )
        notes = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<aside epub:type="footnote" id="note-1"><p>Same English note 2026. '
            '<a href="chapter.xhtml#ref-1">back</a></p></aside>'
            '<aside epub:type="footnote" id="note-2"><p>Same English note 2026. '
            '<a href="chapter.xhtml#ref-2">back</a></p></aside>'
            '</body></html>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OEBPS/content.opf", opf)
            archive.writestr("OEBPS/chapter.xhtml", chapter)
            archive.writestr("OEBPS/notes.xhtml", notes)

    def test_note_ledger_deduplicates_only_before_approval_and_apply_preserves_links(self) -> None:
        from ebook_kindle import localization
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            source = root / "source.epub"
            output = root / "localized.epub"
            self._write_note_epub(source)
            ledger = localization.extract_note_ledger(source)
            self.assertEqual(ledger["metrics"]["note_count"], 2)
            self.assertEqual(ledger["metrics"]["unique_source_count"], 1)
            self.assertEqual(ledger["metrics"]["duplicate_source_count"], 1)
            with self.assertRaises(SkillError) as approval:
                localization.translate_pending_groups(ledger, allow_cloud_translation=False)
            self.assertEqual(approval.exception.code, "CLOUD_TRANSLATION_NOT_APPROVED")

            def fake_spark(items: object) -> dict:
                values = list(items)
                self.assertEqual(len(values), 1)
                return {values[0]["group_key"]: "同一条中文注释，保留 2026 年。"}

            reviewed = localization.translate_pending_groups(
                ledger,
                allow_cloud_translation=True,
                runner=fake_spark,
            )
            self.assertEqual({note["status"] for note in reviewed["notes"]}, {"model-reviewed"})
            self.assertEqual(len({note["translation"] for note in reviewed["notes"]}), 1)
            with self.assertRaises(SkillError):
                localization.approve_translations(reviewed, "WRONG")
            approved = localization.approve_translations(reviewed, "APPROVE_NOTE_TRANSLATIONS")
            result = localization.apply_approved_translations(source, approved, output, overwrite=False)
            self.assertEqual(result["status"], "PASS")
            localized = localization.extract_note_ledger(output)
            self.assertEqual(
                {note["source_text"] for note in localized["notes"]},
                {"同一条中文注释，保留 2026 年。"},
            )
            from ebook_kindle.gates import inspect_epub_structure

            structure = inspect_epub_structure(output, require_toc=False)
            self.assertEqual(structure["noteref_count"], 2)
            self.assertEqual(structure["resolved_noteref_count"], 2)
            self.assertEqual(structure["bidirectional_note_count"], 2)

    def test_existing_per_id_approved_translations_are_never_fanned_out(self) -> None:
        from ebook_kindle import localization
        from ebook_kindle.workspace import cache_key

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            source = Path(outer) / "source.epub"
            self._write_note_epub(source)
            ledger = localization.extract_note_ledger(source)
            translations = ["第一条已经批准的 2026 年译文。", "第二条上下文不同的 2026 年批准译文。"]
            for note, translated in zip(ledger["notes"], translations):
                note["translation"] = translated
                note["translation_sha256"] = cache_key({"text": translated})
                note["status"] = "approved"
                note["approval_basis"] = "existing-gold"
            unchanged = localization.translate_pending_groups(
                ledger,
                allow_cloud_translation=True,
                runner=lambda items: self.fail("approved groups must never be sent"),
            )
            self.assertEqual([note["translation"] for note in unchanged["notes"]], translations)
            approved = localization.approve_translations(unchanged, "APPROVE_NOTE_TRANSLATIONS")
            self.assertEqual([note["translation"] for note in approved["notes"]], translations)

    def test_ledger_write_is_private_no_clobber_and_source_alias_is_rejected_by_cli(self) -> None:
        from ebook_kindle import localization
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            source = root / "source.epub"
            ledger_path = root / "notes.json"
            self._write_note_epub(source)
            ledger = localization.extract_note_ledger(source)
            localization.write_ledger(ledger_path, ledger, overwrite=False)
            self.assertEqual(stat.S_IMODE(ledger_path.stat().st_mode), 0o600)
            with self.assertRaises(SkillError) as collision:
                localization.write_ledger(ledger_path, ledger, overwrite=False)
            self.assertEqual(collision.exception.code, "OUTPUT_EXISTS")
            spec = importlib.util.spec_from_file_location("localize_notes_entry", SCRIPTS / "localize_notes.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = module.main(["extract", str(source), "--ledger", str(source), "--overwrite"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stream.getvalue())["error_code"], "UNSUPPORTED_OUTPUT")
            self.assertTrue(zipfile.is_zipfile(source))


class CliContractTests(unittest.TestCase):
    def test_thin_cli_script_exists(self) -> None:
        target = SCRIPTS / "ebook_to_kindle.py"
        self.assertTrue(target.is_file())
        spec = importlib.util.spec_from_file_location("ebook_to_kindle_entry", target)
        self.assertIsNotNone(spec)

    def test_convert_and_workspace_commands_have_explicit_reuse_controls(self) -> None:
        from ebook_kindle import cli

        converted = cli.build_parser().parse_args(
            ["convert", "book.pdf", "--workspace", "/private/tmp/book-workspace", "--reuse", "refresh"]
        )
        self.assertEqual(converted.workspace, Path("/private/tmp/book-workspace"))
        self.assertEqual(converted.reuse, "refresh")
        inspected = cli.build_parser().parse_args(["workspace", "inspect", "/private/tmp/book-workspace"])
        self.assertEqual(inspected.workspace_command, "inspect")
        purged = cli.build_parser().parse_args(
            ["workspace", "purge", "/private/tmp/book-workspace", "--confirm", "PURGE_EBOOK_WORKSPACE"]
        )
        self.assertEqual(purged.confirm, "PURGE_EBOOK_WORKSPACE")


class PublicationSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from ebook_kindle import pipeline

        cls.pipeline = pipeline

    def test_report_cannot_alias_input_output_or_escape_output_dir(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer).resolve()
            output = root / "out"
            output.mkdir()
            source = root / "book.docx"
            source.write_text("fixture", encoding="utf-8")
            with self.assertRaises(SkillError):
                self.pipeline.resolve_publication_paths(
                    input_path=source, output_dir=output, formats=("azw3",), report_override=Path("book.azw3")
                )
            with self.assertRaises(SkillError):
                self.pipeline.resolve_publication_paths(
                    input_path=source, output_dir=output, formats=("azw3",), report_override=root / "outside.json"
                )
            epub_input = output / "same.epub"
            epub_input.write_text("fixture", encoding="utf-8")
            with self.assertRaises(SkillError):
                self.pipeline.resolve_publication_paths(
                    input_path=epub_input, output_dir=output, formats=("epub",), report_override=None
                )
            case_alias_source = root / "book.docx"
            case_alias_source.write_text("fixture", encoding="utf-8")
            existing_output = output / "book.azw3"
            existing_output.write_bytes(b"existing-book")
            with self.assertRaises(SkillError) as case_error:
                self.pipeline.resolve_publication_paths(
                    input_path=case_alias_source,
                    output_dir=output,
                    formats=("azw3",),
                    report_override=Path("book.AZW3"),
                )
            self.assertEqual(case_error.exception.code, "UNSUPPORTED_OUTPUT")
            self.assertEqual(existing_output.read_bytes(), b"existing-book")
            self.assertEqual(case_alias_source.read_text(encoding="utf-8"), "fixture")

    def test_runtime_tree_and_protected_hardlinks_are_never_publication_targets(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "Ebook-To-Kindle"
            jobs = runtime / "jobs"
            jobs.mkdir(parents=True)
            manifest = runtime / "runtime.json"
            manifest.write_text("{}", encoding="utf-8")
            lock = runtime / ".operation.lock"
            lock.write_bytes(b"")

            for unsafe in (runtime, jobs, base / "ebook-to-kindle" / "jobs"):
                with self.assertRaises(SkillError) as caught:
                    self.pipeline.reject_runtime_publication_paths(unsafe, [unsafe / "book.azw3"], runtime)
                self.assertEqual(caught.exception.code, "UNSUPPORTED_OUTPUT")

            output = base / "out"
            output.mkdir()
            hardlink = output / "book.azw3"
            os.link(manifest, hardlink)
            with self.assertRaises(SkillError) as hardlink_error:
                self.pipeline.resolve_publication_paths(
                    input_path=base / "book.txt",
                    output_dir=output,
                    formats=("azw3",),
                    report_override=None,
                )
            self.assertEqual(hardlink_error.exception.code, "UNSUPPORTED_OUTPUT")
            self.assertEqual(manifest.read_text(encoding="utf-8"), "{}")

    def test_output_directory_swap_cannot_redirect_a_commit(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            output = base / "out"
            moved = base / "out-original"
            runtime = base / "runtime"
            output.mkdir()
            runtime.mkdir()
            runtime_manifest = runtime / "runtime.json"
            runtime_manifest.write_bytes(b"runtime-truth")
            source = base / "new-book"
            source.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"

            def swap_output_directory() -> None:
                output.rename(moved)
                output.symlink_to(runtime, target_is_directory=True)

            with self.assertRaises(SkillError) as caught:
                self.pipeline._publish_transaction(
                    {destination: source},
                    report,
                    {"status": "PASS"},
                    True,
                    before_commit=swap_output_directory,
                )
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            self.assertEqual(runtime_manifest.read_bytes(), b"runtime-truth")
            self.assertFalse((runtime / "book.azw3").exists())
            self.assertFalse((runtime / "book.json").exists())
            self.assertEqual(list(moved.iterdir()), [])

    def test_conversion_start_anchor_prevents_input_parent_from_becoming_output(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            source_parent = base / "source"
            output = base / "out"
            original_output = base / "out-original"
            source_parent.mkdir()
            output.mkdir()
            input_epub = source_parent / "book.epub"
            input_epub.write_bytes(b"original-input")
            staged = base / "new.epub"
            staged.write_bytes(b"new-output")
            anchor = self.pipeline._open_publication_anchor(output)
            try:
                output.rename(original_output)
                source_parent.rename(output)
                with self.assertRaises(SkillError) as caught:
                    self.pipeline._publish_transaction(
                        {output / "book.epub": staged},
                        output / "book.json",
                        {"status": "PASS"},
                        True,
                        directory_anchor=anchor,
                    )
                self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            finally:
                self.pipeline._close_publication_anchor(anchor)
            self.assertEqual((output / "book.epub").read_bytes(), b"original-input")
            self.assertEqual(list(original_output.iterdir()), [])

    def test_overwrite_leaf_type_swap_is_restored_and_never_published(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            output = base / "out"
            output.mkdir()
            staged = base / "new.azw3"
            staged.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"
            destination.write_bytes(b"old-book")
            report.write_text('{"status":"OLD"}', encoding="utf-8")
            real_replace = os.replace
            swapped = False

            def swap_leaf_before_backup(source: str, target: str, *args: object, **kwargs: object) -> None:
                nonlocal swapped
                if source == destination.name and ".rollback-" in target and not swapped:
                    swapped = True
                    directory_fd = int(kwargs["src_dir_fd"])
                    os.unlink(source, dir_fd=directory_fd)
                    os.mkdir(source, 0o700, dir_fd=directory_fd)
                real_replace(source, target, *args, **kwargs)

            with mock.patch.object(self.pipeline.os, "replace", side_effect=swap_leaf_before_backup):
                with self.assertRaises(SkillError) as caught:
                    self.pipeline._publish_transaction(
                        {destination: staged}, report, {"status": "PASS"}, True
                    )
            self.assertEqual(caught.exception.code, "UNSUPPORTED_OUTPUT")
            self.assertTrue(destination.is_dir())
            self.assertEqual(report.read_text(encoding="utf-8"), '{"status":"OLD"}')
            self.assertFalse(any(path.name.startswith(".book.azw3.stage-") for path in output.iterdir()))

    def test_multi_file_publish_rolls_back_and_preserves_directory_mode(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "shared"
            output.mkdir(mode=0o755)
            output.chmod(0o755)
            source_one = Path(outer) / "new-one"
            source_two = Path(outer) / "new-two"
            source_one.write_bytes(b"new-one")
            source_two.write_bytes(b"new-two")
            destination_one = output / "book.azw3"
            destination_two = output / "book.mobi"
            report = output / "book.json"
            destination_one.write_bytes(b"old-one")
            destination_two.write_bytes(b"old-two")
            report.write_bytes(b"old-report")
            real_replace = os.replace

            def flaky_replace(source: str, destination: str, *args: object, **kwargs: object) -> None:
                if destination == destination_two.name and ".stage-" in Path(source).name:
                    raise OSError("synthetic commit failure")
                real_replace(source, destination, *args, **kwargs)

            with mock.patch("ebook_kindle.pipeline.os.replace", side_effect=flaky_replace):
                with self.assertRaises(SkillError):
                    self.pipeline._publish_transaction(
                        {destination_one: source_one, destination_two: source_two},
                        report,
                        {"status": "PASS"},
                        True,
                    )
            self.assertEqual(destination_one.read_bytes(), b"old-one")
            self.assertEqual(destination_two.read_bytes(), b"old-two")
            self.assertEqual(report.read_bytes(), b"old-report")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
            self.assertEqual([path for path in output.iterdir() if path.name.startswith(".")], [])

    def test_restore_failure_preserves_the_only_old_copy_as_rollback(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "out"
            output.mkdir()
            source_one = Path(outer) / "new-one"
            source_two = Path(outer) / "new-two"
            source_one.write_bytes(b"new-one")
            source_two.write_bytes(b"new-two")
            destination_one = output / "book.azw3"
            destination_two = output / "book.mobi"
            report = output / "book.json"
            destination_one.write_bytes(b"old-one")
            destination_two.write_bytes(b"old-two")
            report.write_bytes(b"old-report")
            real_replace = os.replace

            def flaky_restore(source: str, destination: str, *args: object, **kwargs: object) -> None:
                source_path = Path(source)
                if destination == destination_two.name and ".stage-" in source_path.name:
                    raise OSError("synthetic commit failure")
                if destination == destination_one.name and ".rollback-" in source_path.name:
                    raise OSError("synthetic restore failure")
                real_replace(source, destination, *args, **kwargs)

            with mock.patch("ebook_kindle.pipeline.os.replace", side_effect=flaky_restore):
                with self.assertRaises(SkillError) as caught:
                    self.pipeline._publish_transaction(
                        {destination_one: source_one, destination_two: source_two},
                        report,
                        {"status": "PASS"},
                        True,
                    )
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            rollback_files = list(output.glob(".book.azw3.rollback-*"))
            self.assertEqual(len(rollback_files), 1)
            self.assertEqual(rollback_files[0].read_bytes(), b"old-one")
            self.assertEqual(destination_two.read_bytes(), b"old-two")
            self.assertEqual(report.read_bytes(), b"old-report")

    def test_backup_probe_failure_is_reported_and_other_backups_still_restore(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "out"
            output.mkdir()
            source = Path(outer) / "new-book"
            source.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"
            destination.write_bytes(b"old-book")
            report.write_bytes(b"old-report")
            real_replace = os.replace
            real_entry_stat = self.pipeline._entry_stat
            backup_probe_counts: dict[str, int] = {}

            def fail_commit(source_name: str, destination_name: str, *args: object, **kwargs: object) -> None:
                if destination_name == destination.name and ".stage-" in str(source_name):
                    raise OSError("synthetic commit failure")
                real_replace(source_name, destination_name, *args, **kwargs)

            def fail_second_book_backup_probe(directory_fd: int, name: str) -> object:
                if name.startswith(f".{destination.name}.rollback-"):
                    backup_probe_counts[name] = backup_probe_counts.get(name, 0) + 1
                    if backup_probe_counts[name] == 2:
                        raise PermissionError("synthetic rollback probe failure")
                return real_entry_stat(directory_fd, name)

            with mock.patch.object(self.pipeline.os, "replace", side_effect=fail_commit), mock.patch.object(
                self.pipeline, "_entry_stat", side_effect=fail_second_book_backup_probe
            ):
                with self.assertRaises(SkillError) as caught:
                    self.pipeline._publish_transaction(
                        {destination: source}, report, {"status": "PASS"}, True
                    )
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            rollback_files = list(output.glob(f".{destination.name}.rollback-*"))
            self.assertEqual(len(rollback_files), 1)
            self.assertEqual(rollback_files[0].read_bytes(), b"old-book")
            self.assertIn(rollback_files[0].name, caught.exception.details["rollback_pending"])
            self.assertFalse(destination.exists())
            self.assertEqual(report.read_bytes(), b"old-report")

    def test_post_commit_backup_cleanup_failure_is_explicit_pass_state(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "out"
            output.mkdir()
            source = Path(outer) / "new"
            source.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"
            destination.write_bytes(b"old-book")
            report.write_bytes(b"old-report")
            real_unlink = self.pipeline._unlink_at

            def flaky_unlink(directory_fd: int, name: str) -> None:
                if ".rollback-" in name:
                    raise OSError("synthetic post-commit cleanup failure")
                real_unlink(directory_fd, name)

            with mock.patch("ebook_kindle.pipeline._unlink_at", side_effect=flaky_unlink):
                published = self.pipeline._publish_transaction(
                    {destination: source}, report, {"status": "PASS"}, True
                )
            persisted = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            cleanup = published["details"]["publication_cleanup"]
            self.assertEqual(cleanup["status"], "pending")
            self.assertTrue(cleanup["pending_files"])
            self.assertEqual(destination.read_bytes(), b"new-book")
            self.assertEqual(persisted["status"], "PASS")
            self.assertTrue(list(output.glob(".*.rollback-*")))

    def test_post_commit_report_rewrite_failure_returns_the_persisted_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "out"
            output.mkdir()
            source = Path(outer) / "new"
            source.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"
            real_atomic_write = self.pipeline._atomic_json_entry

            def fail_final_report(directory_fd: int, destination_name: str, payload: object) -> None:
                if destination_name == report.name:
                    raise OSError("synthetic final report rewrite failure")
                real_atomic_write(directory_fd, destination_name, payload)

            with mock.patch("ebook_kindle.pipeline._atomic_json_entry", side_effect=fail_final_report):
                published = self.pipeline._publish_transaction(
                    {destination: source}, report, {"status": "PASS"}, False
                )
            persisted = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            self.assertEqual(persisted["details"]["publication_cleanup"]["status"], "pending")
            self.assertEqual(destination.read_bytes(), b"new-book")

    def test_clean_publication_persists_explicit_complete_cleanup_state(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            output = Path(outer) / "out"
            output.mkdir()
            source = Path(outer) / "book.azw3"
            source.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"
            published = self.pipeline._publish_transaction(
                {destination: source}, report, {"status": "PASS"}, False
            )
            persisted = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            self.assertEqual(
                persisted["details"]["publication_cleanup"],
                {"status": "complete", "pending_files": []},
            )

    def test_directory_and_symlink_collisions_are_rejected_before_publish(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer).resolve()
            output = root / "out"
            output.mkdir()
            source = root / "new-book"
            source.write_bytes(b"new")

            directory_collision = output / "book.azw3"
            directory_collision.mkdir()
            with self.assertRaises(SkillError) as directory_error:
                self.pipeline._publish_transaction(
                    {directory_collision: source}, output / "book.json", {"status": "PASS"}, True
                )
            self.assertEqual(directory_error.exception.code, "UNSUPPORTED_OUTPUT")
            self.assertTrue(directory_collision.is_dir())

            directory_collision.rmdir()
            outside = root / "outside.azw3"
            outside.write_bytes(b"outside")
            symlink_collision = output / "book.azw3"
            symlink_collision.symlink_to(outside)
            with self.assertRaises(SkillError) as symlink_error:
                self.pipeline.resolve_publication_paths(
                    input_path=root / "book.docx",
                    output_dir=output,
                    formats=("azw3",),
                    report_override=None,
                )
            self.assertEqual(symlink_error.exception.code, "UNSUPPORTED_OUTPUT")
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_cleanup_failure_leaves_existing_outputs_untouched_and_no_hidden_stage(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            output = root / "out"
            output.mkdir()
            job = root / "job"
            job.mkdir()
            (job / "sensitive-source.txt").write_text("private body", encoding="utf-8")
            staged = root / "new.azw3"
            staged.write_bytes(b"new")
            destination = output / "book.azw3"
            report = output / "book.json"
            destination.write_bytes(b"old")
            report.write_bytes(b"old-report")
            with mock.patch("ebook_kindle.pipeline.shutil.rmtree", side_effect=OSError("synthetic cleanup failure")):
                with self.assertRaises(SkillError) as caught:
                    self.pipeline._publish_transaction(
                        {destination: staged},
                        report,
                        {"status": "PASS"},
                        True,
                        before_commit=lambda: self.pipeline._remove_job_strict(job),
                    )
            self.assertEqual(caught.exception.code, "ISOLATION_FAILED")
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(report.read_bytes(), b"old-report")
            self.assertEqual([path for path in output.iterdir() if path.name.startswith(".")], [])
            (job / "sensitive-source.txt").unlink()
            job.rmdir()

    def test_no_overwrite_race_uses_atomic_no_clobber(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            output = root / "out"
            output.mkdir()
            staged = root / "new.azw3"
            staged.write_bytes(b"new-book")
            destination = output / "book.azw3"
            report = output / "book.json"

            def competing_commit() -> None:
                destination.write_bytes(b"competitor")

            with self.assertRaises(SkillError) as caught:
                self.pipeline._publish_transaction(
                    {destination: staged},
                    report,
                    {"status": "PASS"},
                    False,
                    before_commit=competing_commit,
                )
            self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
            self.assertEqual(destination.read_bytes(), b"competitor")
            self.assertFalse(report.exists())
            self.assertEqual([path for path in output.iterdir() if path.name.startswith(".")], [])


class SendToKindleTests(unittest.TestCase):
    def setUp(self) -> None:
        import send_to_kindle

        self.stk = send_to_kindle

    def test_node_script_values_are_not_rescanned_for_placeholders(self) -> None:
        placeholders = "__TITLE__ __FILE_PATH__ __DOMAIN__ __DEVICE__ __DELIVER__ __TIMEOUT__"
        title = placeholders + "'\"`${process.exit(1)}`\\\n"
        device = placeholders + "</script>"
        script = self.stk.build_node_script("/books/" + placeholders + ".epub", "amazon.com", device, True, 90, title)
        self.assertIn("const expectedTitle = " + json.dumps(title) + ";", script)
        self.assertIn("const targetDevice = " + json.dumps(device) + ";", script)
        self.assertIn('const domain = "amazon.com";', script)
        self.assertIn("parseInt(90, 10)", script)
        node = shutil.which("node")
        if node:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "push.js"
                path.write_text(script, encoding="utf-8")
                result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_kindle_only_formats_switch_to_sibling_epub_or_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book.azw3"
            book.write_bytes(b"azw3")
            missing = self.stk.send_to_kindle(book, method="email")
            self.assertEqual(missing["error"], "UNSUPPORTED_FORMAT")
            (Path(tmp) / "book.epub").write_bytes(b"epub")
            seen = {}

            def fake_run(cmd, **kwargs):
                seen["cmd"] = cmd
                return types.SimpleNamespace(stdout='{"status": "SUCCESS"}', stderr="", returncode=0)

            with mock.patch.object(self.stk.subprocess, "run", side_effect=fake_run):
                result = self.stk.send_to_kindle(book, method="email")
            self.assertEqual(result["status"], "SUCCESS")
            self.assertTrue(seen["cmd"][-1].endswith("book.epub"))

    def test_rejects_non_amazon_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "book.epub"
            book.write_bytes(b"epub")
            for domain in ("evil.example", "amazon.com/../x", "amazon.com'+alert(1)+'"):
                self.assertEqual(self.stk.send_to_kindle(book, domain=domain)["error"], "INVALID_DOMAIN")

    def test_generated_browser_script_against_fake_amazon_pages(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        harness = SKILL_ROOT / "evals" / "fixtures" / "send_to_kindle_mock_ego.js"
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for deliver in (True, False):
                path = Path(tmp) / ("push.js" if deliver else "upload-only.js")
                path.write_text(self.stk.build_node_script("/books/Dune.epub", "amazon.co.jp", "Paperwhite", deliver, 180, "Dune"), encoding="utf-8")
                paths.append(str(path))
            run = subprocess.run([node, str(harness), *paths], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        cases = {case["name"]: case for case in map(json.loads, run.stdout.splitlines())}

        def outcome(name):
            case = cases[name]
            self.assertEqual(case["finishes"], 0 if name == "task-creation-error" else 1, name)  # closed exactly once
            self.assertEqual(len(case["results"]), 1, name)
            return case, case["results"][0]

        case, res = outcome("happy")
        self.assertEqual(res["deliveryStatus"], "DELIVERY_REQUESTED")
        self.assertEqual(case["delivered"], [{"asin": "NEW", "devices": ["Paperwhite"]}])
        # Library not readable before upload: an old same-title copy must never be pushed.
        case, res = outcome("empty-baseline-old-only")
        self.assertEqual((res["status"], res["deliveryStatus"], case["delivered"]), ("SUCCESS", "CLOUD_UPLOADED", []))
        # Device list that only appears after the dialog opens is still found.
        case, res = outcome("async-device-list")
        self.assertEqual(res["deliveryStatus"], "DELIVERY_REQUESTED")
        # Failures after the upload keep the fact that the book is already in the library.
        case, res = outcome("post-upload-navigation-error")
        self.assertEqual((res["status"], res["deliveryStatus"], case["delivered"]), ("SUCCESS", "CLOUD_UPLOADED", []))
        case, res = outcome("post-delivery-cleanup-error")
        self.assertEqual(res["deliveryStatus"], "DELIVERY_REQUESTED")
        self.assertIn("cleanupWarning", res)
        for name in ("ambiguous-new-documents", "ambiguous-devices", "disabled-confirm"):
            case, res = outcome(name)
            self.assertEqual((res["deliveryStatus"], case["delivered"]), ("CLOUD_UPLOADED", []), name)
        # An older same-title copy that loads late, or sits below the newest pre-upload document, is never picked.
        for name in ("nonempty-incomplete-baseline-old-only", "same-count-different-asins-old-only",
                     "new-copy-below-newest-old-document", "unstable-nonempty-baseline", "device-list-never-loads"):
            case, res = outcome(name)
            self.assertEqual((res["status"], res["deliveryStatus"], case["delivered"]), ("SUCCESS", "CLOUD_UPLOADED", []), name)
        case, res = outcome("nonempty-incomplete-japanese-fallback")
        self.assertEqual((res["error"], case["delivered"]), ("UPLOAD_UNCONFIRMED", []))
        # Once the confirm click may have happened, the result never claims delivery was skipped.
        for name in ("dialog-stays-open", "confirm-click-response-lost", "confirm-click-response-null", "post-confirm-wait-error"):
            case, res = outcome(name)
            self.assertEqual((res["deliveryStatus"], res["device"]), ("DELIVERY_UNCONFIRMED", "Paperwhite"), name)
        # The page changing the device selection or the button between calls must stop the click.
        for name in ("device-selection-reset-between-rpcs", "extra-device-selected-between-rpcs", "confirm-disabled-between-rpcs"):
            case, res = outcome(name)
            self.assertEqual((res["deliveryStatus"], case["delivered"]), ("CLOUD_UPLOADED", []), name)
        # A dialog status that is unreadable (null, undefined, error) is unknown, never success.
        for name in ("dialog-status-null-open", "dialog-status-undefined-open", "dialog-status-throws-open"):
            self.assertEqual(outcome(name)[1]["deliveryStatus"], "DELIVERY_UNCONFIRMED", name)
        self.assertEqual(outcome("send-click-response-lost")[1]["error"], "UPLOAD_UNCONFIRMED")
        for name in cases:  # every scenario, asserted above or not, cleans up once and reports once
            outcome(name)
        self.assertEqual(outcome("empty-baseline-cloud-only")[1]["deliveryStatus"], "CLOUD_UPLOADED")
        self.assertEqual(outcome("login-required")[1]["error"], "NOT_LOGGED_IN")


class InstallSkillSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        target = SCRIPTS / "install_skill.py"
        spec = importlib.util.spec_from_file_location("ebook_to_kindle_installer", target)
        assert spec is not None and spec.loader is not None
        cls.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.installer)

    def test_installer_rejects_parent_and_leaf_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            home = Path(outer) / "home"
            real_agents = Path(outer) / "real-agents"
            (real_agents / "skills").mkdir(parents=True)
            home.mkdir()
            (home / ".agents").symlink_to(real_agents, target_is_directory=True)
            target = home / ".agents" / "skills" / "ebook-to-kindle"
            with mock.patch.object(self.installer, "ALLOWED_HOME", home), mock.patch.object(
                self.installer, "ALLOWED_PARENT", home / ".agents" / "skills"
            ):
                with self.assertRaises(RuntimeError):
                    self.installer.guarded_target(target)

            normal_parent = home / "normal" / "skills"
            normal_parent.mkdir(parents=True)
            victim = home / "victim"
            victim.mkdir()
            leaf = normal_parent / "ebook-to-kindle"
            leaf.symlink_to(victim, target_is_directory=True)
            with mock.patch.object(self.installer, "ALLOWED_HOME", home), mock.patch.object(
                self.installer, "ALLOWED_PARENT", normal_parent
            ):
                with self.assertRaises(RuntimeError):
                    self.installer.guarded_target(leaf)

    def test_replace_failure_restores_the_previous_skill(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            home = Path(outer) / "home"
            parent = home / ".agents" / "skills"
            target = parent / "ebook-to-kindle"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("---\nname: ebook-to-kindle\n---\nold copy\n", encoding="utf-8")
            real_replace = os.replace

            def fail_commit(source: str, destination: str) -> None:
                source_path = Path(source)
                if source_path.name.startswith(".ebook-to-kindle.installing-") and Path(destination) == target:
                    raise OSError("synthetic install commit failure")
                real_replace(source, destination)

            with mock.patch.object(self.installer, "ALLOWED_HOME", home), mock.patch.object(
                self.installer, "ALLOWED_PARENT", parent
            ), mock.patch.object(self.installer.os, "replace", side_effect=fail_commit):
                with self.assertRaises(OSError):
                    self.installer.install(target, True)
            self.assertIn("old copy", (target / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(
                {path.name for path in parent.iterdir() if path.name.startswith(".ebook-to-kindle")},
                {self.installer.INSTALL_LOCK_NAME},
            )

    def test_installer_lock_is_nonblocking_and_kernel_released(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            parent = Path(outer)
            with self.installer.installation_lock(parent):
                with self.assertRaises(self.installer.InstallError) as busy:
                    with self.installer.installation_lock(parent):
                        pass
                self.assertEqual(busy.exception.code, "INSTALL_BUSY")
            with self.installer.installation_lock(parent):
                pass

    def test_restore_failure_preserves_backup_and_reports_recovery_basename(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            home = Path(outer) / "home"
            parent = home / ".agents" / "skills"
            target = parent / "ebook-to-kindle"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("---\nname: ebook-to-kindle\n---\nold copy\n", encoding="utf-8")
            real_replace = os.replace

            def fail_commit_and_restore(source: str, destination: str) -> None:
                source_path = Path(source)
                if source_path.name.startswith(".ebook-to-kindle.installing-") and Path(destination) == target:
                    raise OSError("synthetic commit failure")
                if source_path.name.startswith(".ebook-to-kindle.backup-") and Path(destination) == target:
                    raise OSError("synthetic restore failure")
                real_replace(source, destination)

            with mock.patch.object(self.installer, "ALLOWED_HOME", home), mock.patch.object(
                self.installer, "ALLOWED_PARENT", parent
            ), mock.patch.object(self.installer.os, "replace", side_effect=fail_commit_and_restore):
                with self.assertRaises(self.installer.InstallError) as caught:
                    self.installer.install(target, True)
            self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
            backups = [path for path in parent.iterdir() if path.name.startswith(".ebook-to-kindle.backup-")]
            self.assertEqual(len(backups), 1)
            self.assertEqual(caught.exception.details["recovery_entries"], [backups[0].name])
            self.assertIn("old copy", (backups[0] / "SKILL.md").read_text(encoding="utf-8"))
            self.assertFalse(target.exists())

    def test_staging_cleanup_failure_does_not_skip_old_skill_restore(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            home = Path(outer) / "home"
            parent = home / ".agents" / "skills"
            target = parent / "ebook-to-kindle"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("---\nname: ebook-to-kindle\n---\nold copy\n", encoding="utf-8")
            real_replace = os.replace

            def fail_commit(source: str, destination: str) -> None:
                if Path(source).name.startswith(".ebook-to-kindle.installing-") and Path(destination) == target:
                    raise OSError("synthetic commit failure")
                real_replace(source, destination)

            def fail_staging_cleanup(path: Path) -> None:
                if path.name.startswith(".ebook-to-kindle.installing-"):
                    raise self.installer.InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [path.name]})
                self.installer._remove_staging_strict(path)

            with mock.patch.object(self.installer, "ALLOWED_HOME", home), mock.patch.object(
                self.installer, "ALLOWED_PARENT", parent
            ), mock.patch.object(self.installer.os, "replace", side_effect=fail_commit), mock.patch.object(
                self.installer, "_remove_staging_strict", side_effect=fail_staging_cleanup
            ):
                with self.assertRaises(self.installer.InstallError) as caught:
                    self.installer.install(target, True)
            self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
            self.assertIn("old copy", (target / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue(caught.exception.details["recovery_entries"][0].startswith(".ebook-to-kindle.installing-"))


class AcceptanceRunnerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        target = SKILL_ROOT / "evals" / "run_acceptance.py"
        spec = importlib.util.spec_from_file_location("ebook_to_kindle_acceptance", target)
        assert spec is not None and spec.loader is not None
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)
        verifier_target = SKILL_ROOT / "evals" / "verify_fixture_roundtrip.py"
        verifier_spec = importlib.util.spec_from_file_location("ebook_to_kindle_verifier", verifier_target)
        assert verifier_spec is not None and verifier_spec.loader is not None
        cls.verifier = importlib.util.module_from_spec(verifier_spec)
        verifier_spec.loader.exec_module(cls.verifier)

    def test_nonempty_acceptance_root_requires_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "results"
            root.mkdir()
            (root / "keep.txt").write_text("old", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.runner.prepare_output_root(root, False)
            self.assertEqual(self.runner.prepare_output_root(root, True), root.resolve())

    def test_acceptance_output_root_rejects_runtime_and_skill_source_before_mkdir(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "ebook-to-kindle"
            runtime.mkdir()
            unsafe_runtime_child = runtime / "acceptance"
            with self.assertRaises(RuntimeError):
                self.runner.prepare_output_root(unsafe_runtime_child, True, runtime)
            self.assertFalse(unsafe_runtime_child.exists())

            source_child = SKILL_ROOT / "evals" / "fixtures" / "must-not-create"
            with self.assertRaises(RuntimeError):
                self.runner.prepare_output_root(source_child, True, runtime)
            self.assertFalse(source_child.exists())

    def test_acceptance_case_symlink_cannot_escape_result_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "results"
            victim = Path(outer) / "victim"
            root.mkdir()
            victim.mkdir()
            (victim / "keep.txt").write_text("unchanged", encoding="utf-8")
            (root / "docx").symlink_to(victim, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                self.runner.prepare_case_dir(root.resolve(), "docx")
            self.assertEqual((victim / "keep.txt").read_text(encoding="utf-8"), "unchanged")

    def test_acceptance_root_swap_cannot_create_a_case_in_runtime(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            root = base / "results"
            moved = base / "results-original"
            runtime = base / "runtime"
            root.mkdir()
            runtime.mkdir()
            with self.runner.acceptance_root_anchor(root) as anchor:
                root.rename(moved)
                root.symlink_to(runtime, target_is_directory=True)
                with self.assertRaises(RuntimeError):
                    self.runner.prepare_case_dir(root, "digital-pdf", anchor)
            self.assertFalse((runtime / "digital-pdf").exists())
            self.assertEqual(list(moved.iterdir()), [])

    def test_acceptance_runtime_symlink_is_rejected_before_any_subprocess(self) -> None:
        from ebook_kindle.core import SkillError

        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            actual_parent = root / "actual"
            runtime = actual_parent / "ebook-to-kindle"
            runtime.mkdir(parents=True)
            linked_parent = root / "linked"
            linked_parent.symlink_to(actual_parent, target_is_directory=True)
            with self.assertRaises(SkillError):
                self.runner.prepare_runtime_argument(linked_parent / "ebook-to-kindle")

    def test_roundtrip_verifier_runtime_symlink_returns_stable_json(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer)
            actual = root / "actual-runtime"
            actual.mkdir()
            linked = root / "linked-runtime"
            linked.symlink_to(actual, target_is_directory=True)
            ebook = root / "fixture.azw3"
            ebook.write_bytes(b"fixture")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_ROOT / "evals" / "verify_fixture_roundtrip.py"),
                    "--runtime",
                    str(linked),
                    "--item",
                    "docx",
                    "azw3",
                    str(ebook),
                    "--output",
                    str(root / "report.json"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error_code"], "ISOLATION_FAILED")
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(root), result.stdout + result.stderr)

    def test_roundtrip_verifier_env_failure_strictly_removes_job(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            jobs = root / "jobs"
            jobs.mkdir(parents=True)
            args = types.SimpleNamespace(
                truth=SKILL_ROOT / "evals" / "ground-truth.json",
                item=[],
                overwrite=True,
            )
            with mock.patch.object(self.verifier, "verify_runtime", return_value={"core_verified": True}), mock.patch.object(
                self.verifier, "snapshot_calibre_user_state", return_value={"state": "same"}
            ), mock.patch.object(self.verifier, "build_job_env", side_effect=OSError("synthetic env failure")):
                output = Path(outer) / "report.json"
                with self.verifier.verification_output_anchor(output, [args.truth], True) as anchor:
                    with self.assertRaises(OSError):
                        self.verifier._verify_locked(args, output, root, anchor)
            self.assertEqual(list(jobs.iterdir()), [])

    def test_roundtrip_verifier_rejects_truth_hardlink_and_parent_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "ebook-to-kindle"
            runtime.mkdir()
            truth = base / "truth.json"
            truth.write_text(
                json.dumps({"fixture_version": "test", "docx": {"ordered_markers": ["A"]}}),
                encoding="utf-8",
            )
            ebook = base / "book.azw3"
            ebook.write_bytes(b"fixture")
            hardlink = base / "report.json"
            os.link(truth, hardlink)
            args = types.SimpleNamespace(
                runtime=runtime,
                truth=truth,
                item=[["docx", "azw3", str(ebook)]],
                output=hardlink,
                overwrite=True,
            )
            with self.assertRaises(self.verifier.SkillError) as alias:
                self.verifier.prepare_verification_paths(args)
            self.assertEqual(alias.exception.code, "UNSAFE_OUTPUT")

            real_parent = base / "real-parent"
            real_parent.mkdir()
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            args.output = linked_parent / "result.json"
            with self.assertRaises(self.verifier.SkillError) as symlink:
                self.verifier.prepare_verification_paths(args)
            self.assertEqual(symlink.exception.code, "UNSAFE_OUTPUT")

    def test_roundtrip_verifier_rejects_duplicate_normalized_item_keys(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "ebook-to-kindle"
            runtime.mkdir()
            truth = base / "truth.json"
            truth.write_text(
                json.dumps({"fixture_version": "test", "docx": {"ordered_markers": ["A"]}}),
                encoding="utf-8",
            )
            first = base / "first.azw3"
            second = base / "second.azw3"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            args = types.SimpleNamespace(
                runtime=runtime,
                truth=truth,
                item=[
                    ["docx", "azw3", str(first)],
                    ["DOCX", "AZW3", str(second)],
                ],
                output=base / "result.json",
                overwrite=False,
            )
            with self.assertRaises(self.verifier.SkillError) as caught:
                self.verifier.prepare_verification_paths(args)
            self.assertEqual(caught.exception.code, "DUPLICATE_ITEM")

    def test_roundtrip_verifier_rejects_casefold_runtime_tree_alias(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "Ebook-To-Kindle"
            runtime.mkdir()
            truth = base / "truth.json"
            truth.write_text(
                json.dumps({"fixture_version": "test", "docx": {"ordered_markers": ["A"]}}),
                encoding="utf-8",
            )
            ebook = base / "book.azw3"
            ebook.write_bytes(b"fixture")
            args = types.SimpleNamespace(
                runtime=runtime,
                truth=truth,
                item=[["docx", "azw3", str(ebook)]],
                output=base / "ebook-to-kindle" / "inside.json",
                overwrite=False,
            )
            with self.assertRaises(self.verifier.SkillError) as caught:
                self.verifier.prepare_verification_paths(args)
            self.assertEqual(caught.exception.code, "UNSAFE_OUTPUT")

    def test_roundtrip_verifier_anchor_blocks_parent_swap_and_no_clobber_race(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            parent = base / "reports"
            moved = base / "reports-original"
            runtime = base / "runtime"
            parent.mkdir()
            runtime.mkdir()
            truth = base / "truth.json"
            ebook = base / "book.azw3"
            runtime_manifest = runtime / "runtime.json"
            truth.write_text("{}", encoding="utf-8")
            ebook.write_bytes(b"ebook")
            runtime_manifest.write_bytes(b"runtime-truth")
            output = parent / "report.json"
            protected = [truth, ebook, runtime_manifest]

            with self.verifier.verification_output_anchor(output, protected, False) as anchor:
                output.write_bytes(b"racing-file")
                with self.assertRaises(self.verifier.SkillError) as collision:
                    self.verifier.publish_verification_report(anchor, output, {"status": "PASS"})
                self.assertEqual(collision.exception.code, "OUTPUT_EXISTS")
                self.assertEqual(output.read_bytes(), b"racing-file")
            output.unlink()

            with self.verifier.verification_output_anchor(output, protected, True) as anchor:
                parent.rename(moved)
                parent.symlink_to(runtime, target_is_directory=True)
                with self.assertRaises(self.verifier.SkillError) as swap:
                    self.verifier.publish_verification_report(anchor, output, {"status": "PASS"})
                self.assertEqual(swap.exception.code, "UNSAFE_OUTPUT")
            self.assertEqual(runtime_manifest.read_bytes(), b"runtime-truth")
            self.assertFalse((runtime / "report.json").exists())
            self.assertEqual(list(moved.iterdir()), [])

    def test_roundtrip_verifier_postcommit_cleanup_never_reverses_persisted_pass(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            parent = base / "reports"
            parent.mkdir()
            truth = base / "truth.json"
            ebook = base / "book.azw3"
            truth.write_text("{}", encoding="utf-8")
            ebook.write_bytes(b"ebook")
            output = parent / "report.json"

            with self.verifier.verification_output_anchor(output, [truth, ebook], False) as anchor:
                real_unlink = self.verifier.os.unlink

                def fail_stage_cleanup(path: str, *args: object, **kwargs: object) -> None:
                    if str(path).startswith(".report.json.stage-"):
                        raise OSError("synthetic cleanup failure")
                    real_unlink(path, *args, **kwargs)

                with mock.patch.object(self.verifier.os, "unlink", side_effect=fail_stage_cleanup):
                    published = self.verifier.publish_verification_report(anchor, output, {"status": "PASS"})
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            self.assertEqual(persisted["status"], "PASS")
            self.assertEqual(persisted["publication_cleanup"]["status"], "pending")
            self.assertTrue(persisted["publication_cleanup"]["pending_files"])
            for pending_name in persisted["publication_cleanup"]["pending_files"]:
                (parent / pending_name).unlink()

            # A post-commit stat failure for the now-absent final stage is
            # noexcept; disk and returned complete truth stay aligned.
            with self.verifier.verification_output_anchor(output, [truth, ebook], True) as anchor:
                real_entry_stat = self.verifier._entry_stat

                def fail_postcommit_stage_stat(directory_fd: int, name: str) -> object:
                    if name.startswith(".report.json.stage-"):
                        raise PermissionError("synthetic postcommit stat failure")
                    return real_entry_stat(directory_fd, name)

                with mock.patch.object(self.verifier, "_entry_stat", side_effect=fail_postcommit_stage_stat):
                    published = self.verifier.publish_verification_report(anchor, output, {"status": "PASS"})
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            self.assertEqual(persisted["publication_cleanup"]["status"], "complete")
            self.assertEqual(persisted["publication_cleanup"]["pending_files"], [])

            # Overwrite commits a conservative pending report first. A failure
            # while rewriting "complete" must return that exact disk truth.
            with self.verifier.verification_output_anchor(output, [truth, ebook], True) as anchor:
                real_replace = self.verifier.os.replace
                calls = 0

                def fail_final_replace(source: str, destination: str, *args: object, **kwargs: object) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("synthetic finalization failure")
                    real_replace(source, destination, *args, **kwargs)

                with mock.patch.object(self.verifier.os, "replace", side_effect=fail_final_replace):
                    published = self.verifier.publish_verification_report(anchor, output, {"status": "PASS"})
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(published, persisted)
            self.assertEqual(persisted["status"], "PASS")
            self.assertEqual(persisted["publication_cleanup"]["status"], "pending")

    def test_roundtrip_verifier_rejects_ambiguous_colon_keys(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            base = Path(outer)
            runtime = base / "ebook-to-kindle"
            runtime.mkdir()
            truth = base / "truth.json"
            truth.write_text(
                json.dumps({"fixture_version": "test", "docx": {"ordered_markers": ["A"]}}),
                encoding="utf-8",
            )
            ebook = base / "book.azw3"
            ebook.write_bytes(b"ebook")
            args = types.SimpleNamespace(
                runtime=runtime,
                truth=truth,
                item=[["docx:alias", "azw3", str(ebook)]],
                output=base / "report.json",
                overwrite=False,
            )
            with self.assertRaises(self.verifier.SkillError) as caught:
                self.verifier.prepare_verification_paths(args)
            self.assertEqual(caught.exception.code, "DUPLICATE_ITEM")


class UninstallSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        target = SCRIPTS / "uninstall.py"
        spec = importlib.util.spec_from_file_location("ebook_to_kindle_uninstall", target)
        assert spec is not None and spec.loader is not None
        cls.uninstall = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.uninstall)

    def test_guard_rejects_a_symlink_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            victim = root / "victim"
            victim.mkdir()
            link = root / "ebook-to-kindle"
            link.symlink_to(victim, target_is_directory=True)
            with mock.patch.object(self.uninstall.Path, "home", return_value=root):
                with self.assertRaises(RuntimeError):
                    self.uninstall.guarded(link, link)
            self.assertTrue(victim.is_dir())

    def test_uninstall_cli_rejects_unsafe_target_without_traceback_or_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            unsafe = Path(outer) / "unsafe-target"
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "uninstall.py"), "--skill-target", str(unsafe), "--keep-runtime"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(payload["error_code"], "UNSAFE_TARGET")
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(unsafe), result.stdout + result.stderr)

    def test_uninstall_io_failure_reports_partial_state_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            target = Path(outer) / "ebook-to-kindle"
            target.mkdir()
            (target / "SKILL.md").write_text("---\nname: ebook-to-kindle\n---\n", encoding="utf-8")
            child = target / "partially-removed"
            child.write_text("fixture", encoding="utf-8")

            def partial_failure(path: Path) -> None:
                (path / "partially-removed").unlink()
                raise OSError("synthetic failure")

            with mock.patch.object(self.uninstall, "remove_exact", side_effect=partial_failure):
                payload, code = self.uninstall.execute_removal([("skill", target)])
            self.assertEqual(code, 3)
            self.assertEqual(payload["error_code"], "UNINSTALL_FAILED")
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["partial_state"], "possible")
            self.assertNotIn("path", json.dumps(payload))
            self.assertTrue(target.exists())

    def test_uninstall_skill_identity_requires_exact_frontmatter_and_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            base = Path(outer)
            target = base / "ebook-to-kindle"
            target.mkdir()
            skill_file = target / "SKILL.md"
            skill_file.write_text("---\nname: unrelated\n---\nbody name: ebook-to-kindle\n", encoding="utf-8")
            self.assertFalse(self.uninstall.identity_ok("skill", target))
            result, code = self.uninstall.execute_removal([("skill", target)])
            self.assertEqual(code, 2)
            self.assertEqual(result["error_code"], "TARGET_IDENTITY_MISMATCH")
            self.assertTrue(target.exists())

            skill_file.unlink()
            real_skill = base / "real-skill.md"
            real_skill.write_text("---\nname: ebook-to-kindle\n---\n", encoding="utf-8")
            skill_file.symlink_to(real_skill)
            self.assertFalse(self.uninstall.identity_ok("skill", target))

    def test_uninstall_busy_runtime_performs_zero_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            runtime = root / "ebook-to-kindle"
            runtime.mkdir()
            (runtime / "runtime.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "runtime_name": "ebook-to-kindle",
                        "components": {},
                    }
                ),
                encoding="utf-8",
            )
            skill = root / "skill"
            skill.mkdir()
            with mock.patch.object(
                self.uninstall,
                "runtime_operation_lock",
                side_effect=self.uninstall.SkillError("RUNTIME_BUSY", "busy"),
            ), mock.patch.object(self.uninstall, "remove_exact") as remove:
                payload, code = self.uninstall.execute_removal_locked([("skill", skill)], runtime)
            self.assertEqual(code, 4)
            self.assertEqual(payload["error_code"], "RUNTIME_BUSY")
            self.assertFalse(payload["partial"])
            remove.assert_not_called()
            self.assertTrue(skill.exists())

    def test_uninstall_shares_the_installer_lock_and_performs_zero_deletions(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            parent = Path(outer) / "skills"
            skill = parent / "ebook-to-kindle"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: ebook-to-kindle\n---\n", encoding="utf-8")
            runtime = Path(outer) / "runtime-absent"
            with self.uninstall.installation_lock(parent):
                payload, code = self.uninstall.execute_removal_locked([("skill", skill)], runtime)
            self.assertEqual(code, 4)
            self.assertEqual(payload["error_code"], "INSTALL_BUSY")
            self.assertFalse(payload["partial"])
            self.assertTrue(skill.exists())

    def test_uninstall_runtime_identity_requires_name_components_and_known_entries(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            runtime = Path(outer) / "ebook-to-kindle"
            runtime.mkdir()
            manifest = runtime / "runtime.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "runtime_version": "0.1.0", "components": {}}),
                encoding="utf-8",
            )
            (runtime / "unrelated-user-data").write_text("keep", encoding="utf-8")
            self.assertFalse(self.uninstall.identity_ok("runtime", runtime))
            payload, code = self.uninstall.execute_removal([("runtime", runtime)])
            self.assertEqual(code, 2)
            self.assertEqual(payload["error_code"], "TARGET_IDENTITY_MISMATCH")
            self.assertTrue(runtime.exists())

            (runtime / "unrelated-user-data").unlink()
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "runtime_name": "ebook-to-kindle",
                        "components": {},
                        "rollback_shell": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(self.uninstall.identity_ok("runtime", runtime))


class BootstrapSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        target = SCRIPTS / "bootstrap_runtime.py"
        spec = importlib.util.spec_from_file_location("ebook_to_kindle_bootstrap", target)
        assert spec is not None and spec.loader is not None
        cls.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bootstrap)

    def test_install_root_requires_dedicated_identity_and_plan_shows_full_path(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            (root / "unrelated-user-file.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(self.bootstrap.SkillError):
                self.bootstrap._guard_install_root(root)
            root.joinpath("unrelated-user-file.txt").unlink()
            self.bootstrap._guard_install_root(root)
            self.assertEqual(self.bootstrap.plan("core", root)["runtime_path"], str(root))

    def test_install_environment_setup_failure_leaves_no_component_or_staging(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            with mock.patch.object(
                self.bootstrap, "_build_install_env", side_effect=OSError("synthetic environment failure")
            ):
                with self.assertRaises(OSError):
                    self.bootstrap.install_core(root)
            self.assertEqual(list(root.iterdir()), [])

            with mock.patch.object(
                self.bootstrap, "verify_runtime", return_value={"core_verified": True}
            ), mock.patch.object(self.bootstrap.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(
                self.bootstrap, "_build_install_env", side_effect=OSError("synthetic environment failure")
            ):
                with self.assertRaises(OSError):
                    self.bootstrap.install_pdf(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_all_install_failure_restores_new_root_including_doctor_jobs(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()

            def fake_core(target: Path) -> bool:
                (target / "core").mkdir()
                (target / "runtime.json").write_text(
                    json.dumps({"schema_version": 1, "runtime_version": "0.1.0", "components": {"core": {}}}),
                    encoding="utf-8",
                )
                return True

            def fake_verify(target: Path, skill_root: Path) -> dict:
                (target / "jobs").mkdir(exist_ok=True)
                return {"core_verified": True}

            with mock.patch.object(self.bootstrap, "install_core", side_effect=fake_core), mock.patch.object(
                self.bootstrap, "verify_runtime", side_effect=fake_verify
            ), mock.patch.object(
                self.bootstrap,
                "install_pdf",
                side_effect=self.bootstrap.SkillError("INSTALL_FAILED", "synthetic pdf failure"),
            ):
                with self.assertRaises(self.bootstrap.SkillError):
                    self.bootstrap._install_requested(root, "all", True, ())
            self.assertFalse(root.exists())

    def test_rollback_failure_preserves_manifest_and_reports_recovery_entry(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            core = root / "core"
            core.mkdir(parents=True)
            (core / "partial").write_text("fixture", encoding="utf-8")
            manifest = root / "runtime.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "runtime_version": "0.1.0", "components": {"core": {}}}),
                encoding="utf-8",
            )
            with mock.patch.object(self.bootstrap.shutil, "rmtree", side_effect=OSError("synthetic rollback failure")):
                with self.assertRaises(self.bootstrap.SkillError) as caught:
                    self.bootstrap._rollback_components(root, ["core"], False, ())
            self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
            self.assertEqual(caught.exception.details["recovery_entries"], ["core"])
            self.assertTrue(core.exists())
            self.assertTrue(manifest.exists())

    def test_rollback_strictly_handles_a_new_nonempty_jobs_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            core = root / "core"
            jobs = root / "jobs"
            core.mkdir(parents=True)
            jobs.mkdir()
            (jobs / "stale-doctor-job").mkdir()
            (root / ".operation.lock").write_bytes(b"")
            (root / "runtime.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "runtime_name": "ebook-to-kindle",
                        "components": {"core": {}},
                    }
                ),
                encoding="utf-8",
            )
            real_remove = self.bootstrap._remove_tree_strict

            def fail_jobs_cleanup(path: Path) -> None:
                if path.name == "jobs":
                    raise self.bootstrap.SkillError(
                        "ROLLBACK_INCOMPLETE", "synthetic jobs cleanup failure", {"recovery_entries": ["jobs"]}
                    )
                real_remove(path)

            with mock.patch.object(self.bootstrap, "_remove_tree_strict", side_effect=fail_jobs_cleanup):
                with self.assertRaises(self.bootstrap.SkillError) as caught:
                    self.bootstrap._rollback_components(root, ["core"], True, ())
            self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
            self.assertEqual(caught.exception.details["recovery_entries"], ["jobs"])
            self.assertFalse(core.exists())
            self.assertTrue(jobs.exists())
            manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["runtime_name"], "ebook-to-kindle")
            self.assertEqual(manifest["components"], {})
            self.assertTrue(manifest["rollback_shell"])

    def test_component_and_auxiliary_rollback_failures_are_all_reported(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            core = root / "core"
            jobs = root / "jobs"
            packages = root / "packages"
            core.mkdir(parents=True)
            jobs.mkdir()
            packages.mkdir()
            (core / "partial").write_bytes(b"core")
            (jobs / "stale-job").mkdir()
            (packages / "temporary-package").write_bytes(b"package")
            (root / "runtime.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "runtime_name": "ebook-to-kindle",
                        "components": {"core": {"fixture": True}},
                    }
                ),
                encoding="utf-8",
            )
            real_remove = self.bootstrap._remove_tree_strict

            def fail_core_and_jobs(path: Path) -> None:
                if path.name in {"core", "jobs"}:
                    raise self.bootstrap.SkillError(
                        "ROLLBACK_INCOMPLETE",
                        "synthetic combined rollback failure",
                        {"recovery_entries": [path.name]},
                    )
                real_remove(path)

            with mock.patch.object(self.bootstrap, "_remove_tree_strict", side_effect=fail_core_and_jobs):
                with self.assertRaises(self.bootstrap.SkillError) as caught:
                    self.bootstrap._rollback_components(root, ["core"], False, ())
            self.assertEqual(caught.exception.code, "ROLLBACK_INCOMPLETE")
            self.assertEqual(caught.exception.details["recovery_entries"], ["core", "jobs"])
            self.assertTrue(core.exists())
            self.assertTrue(jobs.exists())
            self.assertFalse(packages.exists())
            manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            self.assertIn("core", manifest["components"])
            self.assertTrue(manifest["rollback_shell"])

    def test_retryable_preexisting_identity_shell_survives_another_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            root.mkdir()
            manifest_path = root / "runtime.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_version": "0.1.0",
                        "runtime_name": "ebook-to-kindle",
                        "components": {},
                        "rollback_shell": True,
                    }
                ),
                encoding="utf-8",
            )
            self.bootstrap._rollback_components(root, [], False, ("runtime.json",))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["components"], {})
            self.assertTrue(manifest["rollback_shell"])
            self.assertEqual(manifest["runtime_name"], "ebook-to-kindle")

    def test_bootstrap_catch_all_returns_sanitized_json(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(self.bootstrap, "_main", side_effect=RuntimeError("/private/sensitive/path")), contextlib.redirect_stdout(stream):
            code = self.bootstrap.main()
        payload = json.loads(stream.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "INSTALL_FAILED")
        self.assertNotIn("/private/sensitive/path", stream.getvalue())

    def test_real_bootstrap_failure_keeps_only_an_identified_retryable_shell(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            stream = io.StringIO()
            argv = [
                "bootstrap_runtime.py",
                "--runtime",
                str(root),
                "--component",
                "all",
                "--accept-download",
            ]
            disk = types.SimpleNamespace(free=self.bootstrap.MIN_FREE_BYTES + 1)

            def fake_core(target: Path) -> bool:
                (target / "core").mkdir()
                (target / "runtime.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "runtime_version": "0.1.0",
                            "components": {"core": {"fixture": True}},
                        }
                    ),
                    encoding="utf-8",
                )
                return True

            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                self.bootstrap, "_platform_check"
            ), mock.patch.object(self.bootstrap.shutil, "disk_usage", return_value=disk), mock.patch.object(
                self.bootstrap, "install_core", side_effect=fake_core
            ), mock.patch.object(
                self.bootstrap, "verify_runtime", return_value={"core_verified": False, "pdf_verified": False, "deep_ready": False}
            ), contextlib.redirect_stdout(stream):
                code = self.bootstrap._main()
            self.assertEqual(code, 1)
            self.assertEqual({item.name for item in root.iterdir()}, {".operation.lock", "runtime.json"})
            manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["components"], {})
            self.assertTrue(manifest["rollback_shell"])
            self.assertEqual(manifest["runtime_name"], "ebook-to-kindle")
            self.bootstrap._guard_install_root(root)
            self.assertEqual(json.loads(stream.getvalue())["error_code"], "INSTALL_FAILED")

    def test_low_disk_failure_still_leaves_an_identified_retryable_shell(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as outer:
            root = Path(outer) / "ebook-to-kindle"
            stream = io.StringIO()
            argv = [
                "bootstrap_runtime.py",
                "--runtime",
                str(root),
                "--component",
                "core",
                "--accept-download",
            ]
            disk = types.SimpleNamespace(free=self.bootstrap.MIN_FREE_BYTES - 1)
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                self.bootstrap, "_platform_check"
            ), mock.patch.object(self.bootstrap.shutil, "disk_usage", return_value=disk), contextlib.redirect_stdout(stream):
                code = self.bootstrap._main()
            self.assertEqual(code, 1)
            self.assertEqual({item.name for item in root.iterdir()}, {".operation.lock", "runtime.json"})
            manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["runtime_name"], "ebook-to-kindle")
            self.assertEqual(manifest["components"], {})
            self.assertTrue(manifest["rollback_shell"])
            self.bootstrap._guard_install_root(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
