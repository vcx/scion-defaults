#!/usr/bin/env python3
"""Unit tests for scripts/agy-usage-enrich.py."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPT_PATH = Path(__file__).resolve().parent / "agy-usage-enrich.py"
SPEC = importlib.util.spec_from_file_location("agy_usage_enrich", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
agy_usage_enrich = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agy_usage_enrich)


class AgyUsageEnrichTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_exact_step_match_with_trailing_generic_tool_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            full_path = Path(tmp) / "transcript_full.jsonl"
            self._write_jsonl(
                full_path,
                [
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "content": "Inspect repository",
                    },
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-13T03:25:15Z",
                        "input_tokens": 4889,
                        "cache_read_tokens": 3705,
                        "output_tokens": 123,
                        "thinking": "Checking files...",
                        "tool_calls": [{"name": "run_command"}, {"name": "read_file"}],
                    },
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "command output 1",
                    },
                    {
                        "step_index": 3,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "file contents 2",
                    },
                    {
                        "step_index": 4,
                        "source": "SYSTEM",
                        "type": "SYSTEM_MESSAGE",
                        "status": "DONE",
                        "content": "background task notification",
                    },
                ],
            )

            hook = {
                "hook_event_name": "PostInvocation",
                "conversationId": "00000000-0000-0000-0000-000000000010",
                "transcriptPath": str(full_path),
                "modelName": "Gemini 3.8 Flash (Medium)",
                "invocationNum": 0,
                "initialNumSteps": 1,
            }
            out = agy_usage_enrich.enrich_payload(hook, retry_sleep_s=0.0)
            self.assertEqual(out["input_tokens"], 4889)
            self.assertEqual(out["output_tokens"], 123)
            self.assertEqual(out["cached_tokens"], 3705)
            self.assertEqual(out["modelName"], "Gemini 3.8 Flash (Medium)")
            self.assertNotIn("reasoning_tokens", out)
            self.assertNotIn("cache_write_tokens", out)

    def test_prefers_sibling_compact_transcript_when_full_exceeds_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            compact_path = Path(tmp) / "transcript.jsonl"
            full_path = Path(tmp) / "transcript_full.jsonl"

            model_row_compact = {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "input_tokens": 9100,
                "cache_read_tokens": 18200,
                "output_tokens": 340,
                "truncated_fields": ["content", "thinking"],
            }
            model_row_full = dict(model_row_compact)
            model_row_full.pop("truncated_fields")

            self._write_jsonl(
                compact_path,
                [
                    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                    model_row_compact,
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "truncated tool output",
                        "truncated_fields": ["content"],
                    },
                ],
            )
            # Trailing tool output in transcript_full.jsonl exceeds 4 MiB, pushing step_index=1 out of tail window.
            self._write_jsonl(
                full_path,
                [
                    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                    model_row_full,
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "X" * (agy_usage_enrich.TAIL_BYTES + 65536),
                    },
                ],
            )

            hook = {
                "hook_event_name": "PostInvocation",
                "transcriptPath": str(full_path),
                "initialNumSteps": 1,
            }
            out = agy_usage_enrich.enrich_payload(hook, retry_sleep_s=0.0)
            self.assertEqual(out["input_tokens"], 9100)
            self.assertEqual(out["output_tokens"], 340)
            self.assertEqual(out["cached_tokens"], 18200)

    def test_bounded_4mib_tail_read_discards_partial_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            filler_payload = "A" * 8192
            with transcript.open("w", encoding="utf-8") as fp:
                # Write >4.5 MiB so (size - 4 MiB) lands mid-line inside an early filler step.
                for idx in range(560):
                    fp.write(
                        json.dumps(
                            {
                                "step_index": idx,
                                "source": "MODEL",
                                "type": "GENERIC",
                                "status": "DONE",
                                "content": filler_payload,
                            }
                        )
                        + "\n"
                    )
                fp.write(
                    json.dumps(
                        {
                            "step_index": 560,
                            "source": "MODEL",
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "input_tokens": 15420,
                            "cache_read_tokens": 42000,
                            "output_tokens": 612,
                        }
                    )
                    + "\n"
                )

            self.assertGreater(transcript.stat().st_size, agy_usage_enrich.TAIL_BYTES + 200_000)
            hook = {
                "hook_event_name": "PostInvocation",
                "transcriptPath": str(transcript),
                "initialNumSteps": 560,
            }
            out = agy_usage_enrich.enrich_payload(hook, retry_sleep_s=0.0)
            self.assertEqual(out["input_tokens"], 15420)
            self.assertEqual(out["cached_tokens"], 42000)
            self.assertEqual(out["output_tokens"], 612)

    def test_system_inject_steps_offset_fallback_and_non_system_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            self._write_jsonl(
                transcript,
                [
                    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                    {"step_index": 1, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE"},
                    {"step_index": 2, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE"},
                    {
                        "step_index": 3,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "input_tokens": 2100,
                        "cache_read_tokens": 900,
                        "output_tokens": 88,
                    },
                ],
            )

            out = agy_usage_enrich.enrich_payload(
                {
                    "hook_event_name": "PostInvocation",
                    "transcriptPath": str(transcript),
                    "initialNumSteps": 1,
                },
                retry_sleep_s=0.0,
            )
            self.assertEqual(out["input_tokens"], 2100)
            self.assertEqual(out["cached_tokens"], 900)
            self.assertEqual(out["output_tokens"], 88)

            # Guard: if step 0 is USER_EXPLICIT (not SYSTEM), initialNumSteps=0 must NOT jump across it.
            blocked = agy_usage_enrich.enrich_payload(
                {
                    "hook_event_name": "PostInvocation",
                    "transcriptPath": str(transcript),
                    "initialNumSteps": 0,
                },
                retry_sleep_s=0.0,
            )
            self.assertNotIn("input_tokens", blocked)

    def test_never_double_counts_prior_step_when_target_step_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            self._write_jsonl(
                transcript,
                [
                    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "input_tokens": 5000,
                        "cache_read_tokens": 1000,
                        "output_tokens": 200,
                    },
                    {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE"},
                ],
            )

            hook = {
                "hook_event_name": "PostInvocation",
                "transcriptPath": str(transcript),
                "initialNumSteps": 3,
            }
            out = agy_usage_enrich.enrich_payload(hook, retry_sleep_s=0.0)
            self.assertEqual(out, hook)

    def test_single_retry_recovers_running_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            self._write_jsonl(
                transcript,
                [
                    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "RUNNING",
                    },
                ],
            )

            def complete_on_sleep(_delay: float) -> None:
                self._write_jsonl(
                    transcript,
                    [
                        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
                        {
                            "step_index": 1,
                            "source": "MODEL",
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "input_tokens": 777,
                            "cache_read_tokens": 333,
                            "output_tokens": 44,
                        },
                    ],
                )

            out = agy_usage_enrich.enrich_payload(
                {
                    "hook_event_name": "PostInvocation",
                    "transcriptPath": str(transcript),
                    "initialNumSteps": 1,
                },
                retry_sleep_s=0.030,
                sleep_fn=complete_on_sleep,
            )
            self.assertEqual(out["input_tokens"], 777)
            self.assertEqual(out["cached_tokens"], 333)
            self.assertEqual(out["output_tokens"], 44)

    def test_optional_thinking_and_cache_write_do_not_inflate_output_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            self._write_jsonl(
                transcript,
                [
                    {
                        "step_index": 0,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "input_tokens": 14181,
                        "cache_read_tokens": 8150,
                        "cache_creation_input_tokens": 1200,
                        "output_tokens": 92,
                        "thinking_tokens": 87,
                    }
                ],
            )

            out = agy_usage_enrich.enrich_payload(
                {
                    "hook_event_name": "PostInvocation",
                    "transcriptPath": str(transcript),
                    "initialNumSteps": 0,
                },
                retry_sleep_s=0.0,
            )
            # output_tokens in AGY already includes thinking tokens (92, NOT 92 + 87).
            self.assertEqual(out["input_tokens"], 14181)
            self.assertEqual(out["cached_tokens"], 8150)
            self.assertEqual(out["cache_write_tokens"], 1200)
            self.assertEqual(out["output_tokens"], 92)
            self.assertEqual(out["reasoning_tokens"], 87)

    def test_cli_fail_open_and_stdout_purity(self) -> None:
        cases = [
            b'{"hook_event_name":"PreInvocation","initialNumSteps":1}\n',
            b'{"hook_event_name":"PostToolUse","toolName":"run_command"}\n',
            b'{"hook_event_name":"PostInvocation","transcriptPath":"/nonexistent/transcript.jsonl","initialNumSteps":1}\n',
            b"not-json-at-all{{{",
        ]
        env = dict(os.environ)
        env.pop("DRY_RUN", None)
        env.pop("SCION_DEBUG_USAGE", None)

        for raw in cases:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH)],
                input=raw,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, raw)
            self.assertEqual(proc.stderr, b"")

    def test_dry_run_cli_and_env(self) -> None:
        proc_flag = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry-run"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_flag.returncode, 0)
        self.assertEqual(proc_flag.stderr, "")
        parsed_flag = json.loads(proc_flag.stdout)
        self.assertTrue(parsed_flag["dry_run"])
        self.assertEqual(parsed_flag["self_test"], "ok")
        self.assertEqual(parsed_flag["sample_enriched"]["cached_tokens"], 3705)

        env = dict(os.environ, DRY_RUN="1")
        proc_env = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc_env.returncode, 0)
        self.assertEqual(proc_env.stderr, "")
        parsed_env = json.loads(proc_env.stdout)
        self.assertTrue(parsed_env["dry_run"])
        self.assertEqual(parsed_env["self_test"], "ok")

    def test_concurrent_subagent_transcripts_are_stateless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent_dir = Path(tmp) / "parent"
            child_dir = Path(tmp) / "child"
            parent_dir.mkdir()
            child_dir.mkdir()
            parent_tx = parent_dir / "transcript.jsonl"
            child_tx = child_dir / "transcript.jsonl"

            self._write_jsonl(
                parent_tx,
                [
                    {
                        "step_index": 4,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "input_tokens": 10000,
                        "cache_read_tokens": 5000,
                        "output_tokens": 300,
                    }
                ],
            )
            self._write_jsonl(
                child_tx,
                [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "input_tokens": 2200,
                        "cache_read_tokens": 800,
                        "output_tokens": 95,
                    }
                ],
            )

            results: dict[str, dict] = {}

            def worker(label: str, tx: Path, step: int) -> None:
                time.sleep(0.002)
                results[label] = agy_usage_enrich.enrich_payload(
                    {
                        "hook_event_name": "PostInvocation",
                        "transcriptPath": str(tx),
                        "initialNumSteps": step,
                    },
                    retry_sleep_s=0.0,
                )

            t1 = threading.Thread(target=worker, args=("parent", parent_tx, 4))
            t2 = threading.Thread(target=worker, args=("child", child_tx, 1))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(results["parent"]["input_tokens"], 10000)
            self.assertEqual(results["parent"]["cached_tokens"], 5000)
            self.assertEqual(results["child"]["input_tokens"], 2200)
            self.assertEqual(results["child"]["cached_tokens"], 800)

    def test_exact_tail_newline_boundary_retains_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            prefix_line = json.dumps(
                {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"}
            ) + "\n"
            target_line = json.dumps(
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "input_tokens": 640,
                    "cache_read_tokens": 320,
                    "output_tokens": 45,
                }
            ) + "\n"
            transcript.write_text(prefix_line + target_line, encoding="utf-8")

            # Set max_bytes == exact byte length of target_line so offset lands right after prefix_line's '\n'.
            out = agy_usage_enrich.enrich_payload(
                {
                    "hook_event_name": "PostInvocation",
                    "transcriptPath": str(transcript),
                    "initialNumSteps": 1,
                },
                max_bytes=len(target_line.encode("utf-8")),
                retry_sleep_s=0.0,
            )
            self.assertEqual(out["input_tokens"], 640)
            self.assertEqual(out["cached_tokens"], 320)
            self.assertEqual(out["output_tokens"], 45)

    def test_telemetry_docs_links_and_ste100_compliance(self) -> None:
        import re

        repo_root = SCRIPT_PATH.parent.parent
        doc_path = repo_root / "docs" / "telemetry" / "antigravity-token-usage.md"
        readme_path = repo_root / "docs" / "telemetry" / "README.md"
        self.assertTrue(doc_path.is_file())
        self.assertIn("antigravity-token-usage.md", readme_path.read_text(encoding="utf-8"))

        md = doc_path.read_text(encoding="utf-8")
        lines = []
        in_fence = False
        for line in md.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                lines.append(line)
        prose = "\n".join(lines)

        self.assertIsNone(re.search(r"\b(we|our|ourselves)\b", prose, flags=re.IGNORECASE))
        self.assertEqual(
            re.findall(
                r"\b(?:\w+'(?:t|re|ve|ll|d|m)|(?:it|that|what|there|here|who|let)'s)\b",
                prose.replace("’", "'"),
                flags=re.IGNORECASE,
            ),
            [],
        )
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", prose):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            rel, _, _ = target.partition("#")
            self.assertTrue((doc_path.parent / rel).resolve().exists(), f"Broken relative link: {rel}")


if __name__ == "__main__":
    unittest.main()
