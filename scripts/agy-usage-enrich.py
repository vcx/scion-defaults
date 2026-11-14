#!/usr/bin/env python3
"""Enrich Antigravity (agy) PostInvocation hook JSON on stdin with transcript token usage.

Upstream Antigravity CLI omits token counts from PostInvocation hook payloads
(google-antigravity/antigravity-cli#366), leaving Scion's antigravity dialect in
calls-only mode (ptone/scion#2237). This filter sits before
`sciontool hook --dialect=antigravity`, performs a bounded 4 MiB tail read on the
conversation's sibling compact `transcript.jsonl` (falling back to `transcriptPath`),
joins the completed MODEL step at `step_index == initialNumSteps` (with a safe
fallback for contiguous SYSTEM steps from PreInvocation `injectSteps`), and injects
`input_tokens`, `output_tokens`, and `cached_tokens` without modifying core Scion.

Fail-open contract:
  - Never writes non-JSON bytes to stdout.
  - On any non-PostInvocation event, missing field, or I/O/JSON error, writes the
    exact raw stdin bytes back to stdout unchanged and exits 0.
  - Logs diagnostics to stderr only when SCION_DEBUG_USAGE=1.

Usage:
  jq --arg ev PostInvocation '. + {"hook_event_name": $ev}' \\
    | scripts/agy-usage-enrich.py \\
    | sciontool hook --dialect=antigravity

  DRY_RUN=1 scripts/agy-usage-enrich.py
  scripts/agy-usage-enrich.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import sys
import tempfile
import time
from typing import Any, Callable

os.environ.setdefault("CLOUDSDK_CORE_DISABLE_PROMPTS", "1")

TAIL_BYTES = 4 * 1024 * 1024
RETRY_SLEEP_SECONDS = 0.030


def _debug(msg: str) -> None:
    if os.environ.get("SCION_DEBUG_USAGE") == "1":
        sys.stderr.write(f"[agy-usage-enrich] {msg}\n")
        sys.stderr.flush()


def _is_non_bool_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _is_non_bool_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _positive_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        val = row.get(key)
        if _is_non_bool_number(val) and val > 0:
            return int(val)
    return None


def _non_negative_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        val = row.get(key)
        if _is_non_bool_number(val) and val >= 0:
            return int(val)
    return None


def _has_native_tokens(payload: dict[str, Any]) -> bool:
    if _positive_int(payload, "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens") is not None:
        return True
    usage = payload.get("usage")
    if isinstance(usage, dict):
        if _positive_int(usage, "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens") is not None:
            return True
    return False


def _normalize_existing_cached_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    if "cached_tokens" not in payload and "cache_read_input_tokens" not in payload:
        cached = _non_negative_int(payload, "cache_read_tokens")
        if cached is not None:
            out = dict(payload)
            out["cached_tokens"] = cached
            return out
    return payload


def _candidate_transcript_paths(transcript_path_raw: str) -> list[Path]:
    target = Path(transcript_path_raw).expanduser()
    candidates: list[Path] = []
    if target.name != "transcript.jsonl":
        sibling_compact = target.parent / "transcript.jsonl"
        candidates.append(sibling_compact)
    if target.suffix == ".jsonl":
        candidates.append(target)
    return candidates


def read_tail_steps(path: Path, max_bytes: int = TAIL_BYTES) -> dict[int, dict[str, Any]]:
    """Read up to the last `max_bytes` of a JSONL transcript and index rows by `step_index`."""
    steps: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return steps

    with path.open("rb") as fp:
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        if size <= 0:
            return steps
        offset = max(0, size - max_bytes)
        mid_line = False
        if offset > 0:
            fp.seek(offset - 1, os.SEEK_SET)
            mid_line = fp.read(1) != b"\n"
        else:
            fp.seek(0, os.SEEK_SET)
        chunk = fp.read()

    if mid_line:
        newline_idx = chunk.find(b"\n")
        if newline_idx == -1:
            return steps
        chunk = chunk[newline_idx + 1 :]

    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        step_idx = row.get("step_index")
        if _is_non_bool_int(step_idx) and step_idx >= 0:
            steps[step_idx] = row
    return steps


def _is_completed_model_usage_row(row: dict[str, Any]) -> bool:
    if row.get("status") != "DONE":
        return False
    if row.get("type") == "GENERIC":
        return False
    if row.get("source") != "MODEL" and row.get("type") != "PLANNER_RESPONSE":
        return False
    return (
        _non_negative_int(row, "input_tokens") is not None
        and _non_negative_int(row, "output_tokens") is not None
    )


def _match_step_in_index(
    steps: dict[int, dict[str, Any]], initial_num_steps: int
) -> tuple[dict[str, Any] | None, bool]:
    """Return `(matched_row, should_retry)` for `initial_num_steps`.

    Matches `step_index == initial_num_steps` first. If `PreInvocation` injected one or
    more contiguous `source == "SYSTEM"` steps at `initial_num_steps .. idx - 1`, matches
    the completed model step at `idx`. Never falls back to an earlier step or across
    non-SYSTEM rows.
    """
    direct = steps.get(initial_num_steps)
    if direct is None:
        return None, True
    if _is_completed_model_usage_row(direct):
        return direct, False
    if direct.get("status") == "RUNNING":
        return None, True

    if direct.get("source") == "SYSTEM":
        idx = initial_num_steps
        while idx in steps and steps[idx].get("source") == "SYSTEM":
            idx += 1
        candidate = steps.get(idx)
        if candidate is None:
            return None, True
        if _is_completed_model_usage_row(candidate):
            return candidate, False
        if candidate.get("status") == "RUNNING":
            return None, True

    return None, False


def find_invocation_usage_row(
    transcript_path_raw: str,
    initial_num_steps: int,
    *,
    max_bytes: int = TAIL_BYTES,
    retry_sleep_s: float = RETRY_SLEEP_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Locate the completed model response row for `initial_num_steps` across candidate files."""
    candidates = _candidate_transcript_paths(transcript_path_raw)
    if not candidates:
        return None

    for attempt in range(2):
        should_retry_any = False
        for path in candidates:
            try:
                steps = read_tail_steps(path, max_bytes=max_bytes)
            except OSError as exc:
                _debug(f"read failed for {path}: {exc}")
                continue
            if not steps:
                should_retry_any = True
                continue
            matched, should_retry = _match_step_in_index(steps, initial_num_steps)
            if matched is not None:
                return matched
            if should_retry:
                should_retry_any = True

        if attempt == 0 and should_retry_any and retry_sleep_s > 0:
            sleep_fn(retry_sleep_s)
        else:
            break
    return None


def enrich_payload(
    payload: dict[str, Any],
    *,
    event_override: str | None = None,
    max_bytes: int = TAIL_BYTES,
    retry_sleep_s: float = RETRY_SLEEP_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Enrich a decoded hook payload dict if it represents an Antigravity PostInvocation event."""
    event_name = payload.get("hook_event_name") or event_override
    if event_name != "PostInvocation":
        return payload

    if _has_native_tokens(payload):
        out = _normalize_existing_cached_tokens(payload)
        if event_override and "hook_event_name" not in out:
            out = dict(out)
            out["hook_event_name"] = event_override
        return out

    transcript_path = payload.get("transcriptPath")
    initial_num_steps = payload.get("initialNumSteps")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return payload
    if not _is_non_bool_int(initial_num_steps) or initial_num_steps < 0:
        return payload

    matched = find_invocation_usage_row(
        transcript_path,
        initial_num_steps,
        max_bytes=max_bytes,
        retry_sleep_s=retry_sleep_s,
        sleep_fn=sleep_fn,
    )
    if matched is None:
        _debug(f"no completed MODEL step for initialNumSteps={initial_num_steps}")
        return payload

    in_tok = _non_negative_int(matched, "input_tokens")
    out_tok = _non_negative_int(matched, "output_tokens")
    if in_tok is None or out_tok is None:
        return payload

    enriched = dict(payload)
    if event_override and "hook_event_name" not in enriched:
        enriched["hook_event_name"] = event_override
    enriched["input_tokens"] = in_tok
    enriched["output_tokens"] = out_tok

    cached_tok = _non_negative_int(
        matched, "cache_read_tokens", "cached_tokens", "cache_read_input_tokens"
    )
    if cached_tok is not None:
        enriched["cached_tokens"] = cached_tok

    cache_write_tok = _positive_int(
        matched, "cache_write_tokens", "cache_creation_input_tokens"
    )
    if cache_write_tok is not None:
        enriched["cache_write_tokens"] = cache_write_tok

    reasoning_tok = _positive_int(matched, "reasoning_tokens", "thinking_tokens")
    if reasoning_tok is not None:
        enriched["reasoning_tokens"] = reasoning_tok

    return enriched


def run_self_test() -> dict[str, Any]:
    """Execute a deterministic in-memory/tempdir dry-run check and return JSON status."""
    with tempfile.TemporaryDirectory(prefix="agy-usage-enrich-dryrun-") as tmpdir:
        logs_dir = Path(tmpdir)
        compact = logs_dir / "transcript.jsonl"
        full = logs_dir / "transcript_full.jsonl"

        rows = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Run unit tests",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "input_tokens": 4889,
                "cache_read_tokens": 3705,
                "output_tokens": 123,
                "truncated_fields": ["thinking"],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "content": "tool output trailing after PLANNER_RESPONSE",
            },
        ]
        serialized = "\n".join(json.dumps(r) for r in rows) + "\n"
        compact.write_text(serialized, encoding="utf-8")
        full.write_text(serialized, encoding="utf-8")

        sample_in = {
            "hook_event_name": "PostInvocation",
            "conversationId": "00000000-0000-0000-0000-000000000001",
            "transcriptPath": str(full),
            "modelName": "Gemini 3.8 Flash (Medium)",
            "invocationNum": 0,
            "initialNumSteps": 1,
        }
        sample_out = enrich_payload(sample_in, retry_sleep_s=0.0)
        if (
            sample_out.get("input_tokens") != 4889
            or sample_out.get("output_tokens") != 123
            or sample_out.get("cached_tokens") != 3705
        ):
            raise RuntimeError(f"unexpected self-test output: {sample_out!r}")

    return {
        "dry_run": True,
        "self_test": "ok",
        "sample_enriched": {
            "hook_event_name": sample_out["hook_event_name"],
            "initialNumSteps": sample_out["initialNumSteps"],
            "input_tokens": sample_out["input_tokens"],
            "output_tokens": sample_out["output_tokens"],
            "cached_tokens": sample_out["cached_tokens"],
        },
    }


def _stdin_has_piped_data() -> bool:
    try:
        if sys.stdin.isatty():
            return False
        ready, _, _ = select.select([sys.stdin], [], [], 0.0)
        return bool(ready)
    except (OSError, ValueError):
        return True


def main(argv: list[str] | None = None) -> int:
    raw_bytes = b""
    try:
        parser = argparse.ArgumentParser(
            description="Enrich Antigravity PostInvocation hook JSON on stdin with transcript token usage."
        )
        parser.add_argument(
            "--event",
            default=None,
            help="Optional hook event name override (for example, PostInvocation).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run deterministic self-test without mutating state (also triggered by DRY_RUN=1 with empty stdin).",
        )
        args = parser.parse_args(argv)

        env_dry_run = os.environ.get("DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
        if args.dry_run:
            sys.stdout.write(json.dumps(run_self_test(), separators=(",", ":")) + "\n")
            sys.stdout.flush()
            return 0

        if env_dry_run and not _stdin_has_piped_data():
            sys.stdout.write(json.dumps(run_self_test(), separators=(",", ":")) + "\n")
            sys.stdout.flush()
            return 0

        raw_bytes = sys.stdin.buffer.read()
        if not raw_bytes.strip():
            if env_dry_run:
                sys.stdout.write(json.dumps(run_self_test(), separators=(",", ":")) + "\n")
            elif raw_bytes:
                sys.stdout.buffer.write(raw_bytes)
            sys.stdout.flush()
            return 0

        payload = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            sys.stdout.buffer.write(raw_bytes)
            sys.stdout.flush()
            return 0

        enriched = enrich_payload(payload, event_override=args.event)
        if enriched == payload:
            sys.stdout.buffer.write(raw_bytes)
            sys.stdout.flush()
            return 0

        suffix = b"\n" if raw_bytes.endswith(b"\n") else b""
        sys.stdout.buffer.write(
            json.dumps(enriched, separators=(",", ":")).encode("utf-8") + suffix
        )
        sys.stdout.flush()
        return 0
    except BaseException as exc:  # noqa: BLE001 - strict fail-open guarantee
        if isinstance(exc, SystemExit):
            raise
        _debug(f"fail-open on unexpected error: {exc!r}")
        if raw_bytes:
            sys.stdout.buffer.write(raw_bytes)
            sys.stdout.flush()
        return 0


if __name__ == "__main__":
    sys.exit(main())
