"""Tests for EVAL FABRICA Firestore collections, CLI CRUD, spec, and lead/serverless-kit guidance."""

from __future__ import annotations

import copy
import datetime
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_store  # noqa: E402
import scion_factory  # noqa: E402


# ---------------------------------------------------------------------------
# In-process Firestore REST Emulator mock (no external Java/emulator required)
# ---------------------------------------------------------------------------


class _FakeFirestoreBackend:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _now_ts() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _apply_mask(self, existing: dict[str, Any], incoming: dict[str, Any], paths: list[str]) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        for path in paths:
            parts = path.split(".")
            src = incoming
            ok = True
            for part in parts:
                if isinstance(src, dict) and part in src:
                    src = src[part]
                else:
                    ok = False
                    break
            if not ok:
                continue
            dst = merged
            for part in parts[:-1]:
                dst = dst.setdefault(part, {})
            dst[parts[-1]] = copy.deepcopy(src)
        return merged

    def commit(self, writes: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            for write in writes:
                if "delete" in write:
                    self.docs.pop(write["delete"], None)
                    continue
                upd = write["update"]
                name = upd["name"]
                fields = copy.deepcopy(upd.get("fields", {}))
                mask = (write.get("updateMask") or {}).get("fieldPaths")
                if mask:
                    base = self.docs.get(name, {}).get("fields", {})
                    fields = self._apply_mask(base, fields, mask)
                for transform in write.get("updateTransforms") or []:
                    if transform.get("setToServerValue") == "REQUEST_TIME":
                        fields[transform["fieldPath"]] = {"timestampValue": self._now_ts()}
                self.docs[name] = {"name": name, "fields": fields}
        return {"writeResults": [{}] * len(writes)}

    @staticmethod
    def _decode_val(v: dict[str, Any]) -> Any:
        return factory_store.from_value(v)

    def _matches_where(self, fields: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        if "fieldFilter" in where:
            ff = where["fieldFilter"]
            path = ff["field"]["fieldPath"]
            op = ff["op"]
            target = self._decode_val(ff["value"])
            val = self._decode_val(fields.get(path, {"nullValue": None}))
            if op == "EQUAL":
                return val == target
            if op == "LESS_THAN":
                return val is not None and val < target
            return False
        if "compositeFilter" in where:
            cf = where["compositeFilter"]
            subs = [self._matches_where(fields, f) for f in cf.get("filters", [])]
            return all(subs) if cf.get("op") == "AND" else any(subs)
        return True

    def run_query(self, sq: dict[str, Any]) -> list[dict[str, Any]]:
        coll = sq["from"][0]["collectionId"]
        where = sq.get("where")
        order_by = sq.get("orderBy") or []
        limit = sq.get("limit")
        with self.lock:
            matched = []
            suffix = f"/documents/{coll}/"
            for name, doc in self.docs.items():
                if suffix not in name:
                    continue
                if self._matches_where(doc["fields"], where):
                    matched.append(copy.deepcopy(doc))
        for ob in reversed(order_by):
            field_path = ob["field"]["fieldPath"]
            desc = ob.get("direction", "ASCENDING") == "DESCENDING"
            matched.sort(
                key=lambda d: str(self._decode_val(d["fields"].get(field_path, {"stringValue": ""})) or ""),
                reverse=desc,
            )
        if isinstance(limit, int):
            matched = matched[:limit]
        return [{"document": d} for d in matched]


@pytest.fixture
def fake_emulator(monkeypatch):
    backend = _FakeFirestoreBackend()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code: int, payload: Any) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.startswith("/v1/"):
                doc_name = path[len("/v1/"):]
                with backend.lock:
                    doc = backend.docs.get(doc_name)
                if doc is None:
                    self._send(404, {"error": {"code": 404, "message": "not found"}})
                else:
                    self._send(200, doc)
                return
            self._send(404, {"error": "unknown path"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            path = self.path.split("?", 1)[0]
            if path.endswith(":commit"):
                self._send(200, backend.commit(body.get("writes", [])))
                return
            if path.endswith(":runQuery"):
                self._send(200, backend.run_query(body.get("structuredQuery", {})))
                return
            self._send(404, {"error": "unknown endpoint"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", host)
    monkeypatch.setenv("FIRESTORE_PROJECT", "demo-proj")
    try:
        yield host, backend
    finally:
        server.shutdown()
        server.server_close()


def _run_cli(argv: list[str], stdin_data: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "factory_store.py"), *argv],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Validation unit tests
# ---------------------------------------------------------------------------


def test_validate_analysis_rules_and_artifact_urls():
    good = {
        "analysis_id": "an-20261111-01",
        "project_slug": "acme-shop",
        "status": "testing",
        "created_by": "eval-agent",
        "task_ids": ["task-1", "task-2"],
        "experiment": {"type": "task_clone", "clone_group_ids": ["cg-1"]},
        "artifacts": {
            "report_url": "gs://eval-bucket/analyses/an-20261111-01/report.json",
            "snapshot_url": "https://storage.googleapis.com/eval-bucket/snapshots/s1/trajectories.json",
            "diff_url": "?view=factory&analysis=an-20261111-01",
        },
    }
    assert factory_store.validate_analysis(good) == []

    bad = copy.deepcopy(good)
    bad["analysis_id"] = "bad/id"
    bad["status"] = "unknown"
    bad["created_by"] = "bot"
    bad["experiment"]["type"] = "magic"
    bad["artifacts"]["report_url"] = "ftp://invalid/report.json"
    bad["artifacts"]["snapshot_url"] = "gs://"
    problems = " | ".join(factory_store.validate_analysis(bad))
    assert "analysis_id" in problems
    assert "status" in problems
    assert "created_by" in problems
    assert "experiment.type" in problems
    assert "artifacts.report_url" in problems
    assert "artifacts.snapshot_url" in problems


def test_validate_clone_group_rules_and_urls():
    good = {
        "clone_group_id": "cg-20261111-01",
        "project_slug": "acme-shop",
        "source_task_id": "task-101",
        "source_tracker_mode": "farmtable",
        "triggered_by": "leader",
        "isolation_mode": "worker_branch",
        "selection_mode": "manual",
        "diff_url": "gs://eval-bucket/clones/cg-20261111-01/diff.json",
        "variants": [
            {
                "clone_id": "c1",
                "branch": "task/task-101/clone-1",
                "model": "gemini-4-argon",
                "snapshot_url": "gs://eval-bucket/snapshots/c1.json",
                "transcript_uri": "gs://eval-bucket/transcripts/c1.jsonl.gz",
            }
        ],
    }
    assert factory_store.validate_clone_group(good) == []

    bad = copy.deepcopy(good)
    bad["variants"] = [{"branch": "no-clone-id", "snapshot_url": "not a url"}]
    bad["triggered_by"] = "nobody"
    bad["diff_url"] = "relative-bad-path"
    problems = " | ".join(factory_store.validate_clone_group(bad))
    assert "variants[0]" in problems
    assert "triggered_by" in problems
    assert "diff_url" in problems


def test_validate_agent_state_and_override_rules():
    state = {
        "project_slug": "acme-shop",
        "governance_mode": "human_approval",
        "enabled": True,
        "policy": {"daily_experiment_budget_usd": 25.0, "min_sample_size": 5},
        "spend": {"daily_usd": 4.2, "monthly_usd": 19.5},
    }
    assert factory_store.validate_agent_state(state) == []

    bad_state = {
        "project_slug": "acme-shop",
        "governance_mode": "yolo",
        "enabled": "yes",
        "policy": {"daily_experiment_budget_usd": -1, "min_sample_size": True},
        "spend": {"daily_usd": "ten"},
    }
    state_problems = " | ".join(factory_store.validate_agent_state(bad_state))
    assert "governance_mode" in state_problems
    assert "enabled" in state_problems
    assert "daily_experiment_budget_usd" in state_problems
    assert "min_sample_size" in state_problems
    assert "spend.daily_usd" in state_problems

    ovr = {
        "override_id": "ovr-1",
        "project_slug": "acme-shop",
        "analysis_id": "an-1",
        "target_scope": "project_settings",
        "status": "active",
        "previous_state": {"default_model": "opus"},
        "applied_state": {"default_model": "gemini-4-argon"},
        "rollback_after_tasks": 10,
    }
    assert factory_store.validate_override(ovr) == []

    bad_ovr = dict(ovr, target_scope="bad", status="weird", rollback_after_tasks=True)
    ovr_problems = " | ".join(factory_store.validate_override(bad_ovr))
    assert "target_scope" in ovr_problems
    assert "status" in ovr_problems
    assert "rollback_after_tasks" in ovr_problems


# ---------------------------------------------------------------------------
# Dry-run CLI tests for all 10 new subcommands
# ---------------------------------------------------------------------------


def test_all_new_subcommands_dry_run(tmp_path):
    analysis = tmp_path / "analysis.json"
    analysis.write_text(json.dumps({
        "analysis_id": "an-1",
        "project_slug": "demo",
        "status": "drafting",
        "artifacts": {
            "report_url": "gs://b/an-1/report.json",
            "snapshot_url": "gs://b/an-1/snap.json",
            "diff_url": "gs://b/an-1/diff.json",
        },
    }))
    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "put-analysis", "--file", str(analysis)])
    assert rc == 0, err
    req = json.loads(out)
    assert req["body"]["writes"][0]["update"]["name"].endswith("/eval_analyses/an-1")
    assert {t["fieldPath"] for t in req["body"]["writes"][0]["updateTransforms"]} == {"created_at", "updated_at"}

    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "get-analysis", "an-1"])
    assert rc == 0, err
    assert json.loads(out)["url"].endswith("/eval_analyses/an-1")

    rc, out, err = _run_cli([
        "--project", "demo-proj", "--dry-run", "list-analyses", "--project-slug", "demo", "--status", "drafting",
    ])
    assert rc == 0, err
    q = json.loads(out)["body"]["structuredQuery"]
    assert q["from"][0]["collectionId"] == "eval_analyses"
    assert q["where"]["compositeFilter"]["op"] == "AND"

    cg = tmp_path / "cg.json"
    cg.write_text(json.dumps({
        "clone_group_id": "cg-1",
        "project_slug": "demo",
        "source_task_id": "t-1",
        "variants": [{"clone_id": "c1", "snapshot_url": "gs://b/c1.json"}],
    }))
    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "put-clone-group", "--file", str(cg)])
    assert rc == 0, err
    assert json.loads(out)["body"]["writes"][0]["update"]["name"].endswith("/eval_clone_groups/cg-1")

    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "get-clone-group", "--clone-group-id", "cg-1"])
    assert rc == 0, err
    assert json.loads(out)["url"].endswith("/eval_clone_groups/cg-1")

    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "get-agent-state", "--project-slug", "demo"])
    assert rc == 0, err
    assert json.loads(out)["url"].endswith("/eval_agent_state/demo")

    st = tmp_path / "state.json"
    st.write_text(json.dumps({"project_slug": "demo", "governance_mode": "autonomous", "enabled": True}))
    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "put-agent-state", "--file", str(st)])
    assert rc == 0, err
    assert json.loads(out)["body"]["writes"][0]["update"]["name"].endswith("/eval_agent_state/demo")

    ovr = tmp_path / "ovr.json"
    ovr.write_text(json.dumps({
        "override_id": "ovr-1",
        "project_slug": "demo",
        "analysis_id": "an-1",
        "target_scope": "template",
        "status": "active",
    }))
    rc, out, err = _run_cli(["--project", "demo-proj", "--dry-run", "put-override", "--file", str(ovr)])
    assert rc == 0, err
    assert json.loads(out)["body"]["writes"][0]["update"]["name"].endswith("/eval_factory_overrides/ovr-1")

    rc, out, err = _run_cli([
        "--project", "demo-proj", "--dry-run", "list-active-overrides", "--project-slug", "demo",
    ])
    assert rc == 0, err
    q = json.loads(out)["body"]["structuredQuery"]
    assert q["from"][0]["collectionId"] == "eval_factory_overrides"
    assert len(q["where"]["compositeFilter"]["filters"]) == 2

    rc, out, err = _run_cli([
        "--project", "demo-proj", "--dry-run", "resolve-override", "ovr-1", "--status", "promoted_to_git",
        "--note", "Merged PR #12",
    ])
    assert rc == 0, err
    w = json.loads(out)["body"]["writes"][0]
    assert w["update"]["name"].endswith("/eval_factory_overrides/ovr-1")
    assert set(w["updateMask"]["fieldPaths"]) == {"status", "resolution_note"}
    assert w["updateTransforms"][0]["fieldPath"] == "resolved_at"


# ---------------------------------------------------------------------------
# End-to-end CRUD against the mock Firestore emulator over HTTP
# ---------------------------------------------------------------------------


def test_eval_fabrica_end_to_end_crud_with_emulator(fake_emulator, tmp_path):
    # 1. eval_analyses: put, get, list (by project_slug + status), missing -> null
    analysis_payload = {
        "analysis_id": "an-100",
        "project_slug": "acme-shop",
        "collection_id": "col-1",
        "status": "testing",
        "created_by": "eval-agent",
        "task_ids": ["t-1", "t-2"],
        "artifacts": {
            "report_url": "gs://eval-art/analyses/an-100/report.json",
            "snapshot_url": "gs://eval-art/snapshots/s-100/trajectories.json",
            "diff_url": "https://storage.googleapis.com/eval-art/analyses/an-100/diff.json",
        },
        "eval_run_ids": ["run-1"],
    }
    rc, _, err = _run_cli(["put-analysis"], stdin_data=json.dumps(analysis_payload))
    assert rc == 0, err

    other_analysis = {
        "analysis_id": "an-101",
        "project_slug": "other-shop",
        "status": "completed",
        "created_by": "leader",
    }
    rc, _, err = _run_cli(["put-analysis"], stdin_data=json.dumps(other_analysis))
    assert rc == 0, err

    rc, out, err = _run_cli(["get-analysis", "an-100"])
    assert rc == 0, err
    fetched = json.loads(out)
    assert fetched["analysis_id"] == "an-100"
    assert fetched["artifacts"]["report_url"] == "gs://eval-art/analyses/an-100/report.json"
    assert fetched["artifacts"]["snapshot_url"] == "gs://eval-art/snapshots/s-100/trajectories.json"
    assert fetched["artifacts"]["diff_url"] == "https://storage.googleapis.com/eval-art/analyses/an-100/diff.json"
    assert fetched["created_at"] and fetched["updated_at"]

    rc, out, err = _run_cli(["get-analysis", "--analysis-id", "does-not-exist"])
    assert rc == 0, err
    assert json.loads(out) is None

    rc, out, err = _run_cli(["list-analyses", "--project-slug", "acme-shop", "--status", "testing"])
    assert rc == 0, err
    listed = json.loads(out)
    assert [d["analysis_id"] for d in listed] == ["an-100"]

    # 2. eval_clone_groups: put, get
    cg_payload = {
        "clone_group_id": "cg-100",
        "project_slug": "acme-shop",
        "source_task_id": "t-1",
        "source_tracker_mode": "farmtable",
        "base_commit_sha": "abc123",
        "triggered_by": "leader",
        "analysis_id": "an-100",
        "isolation_mode": "worker_branch",
        "diff_url": "gs://eval-art/clones/cg-100/diff.json",
        "variants": [
            {
                "clone_id": "c1",
                "branch": "task/t-1/clone-1",
                "model": "opus",
                "cost_usd": 1.25,
                "passed": True,
                "snapshot_url": "gs://eval-art/snapshots/cg-100-c1.json",
                "transcript_uri": "gs://eval-art/transcripts/c1.jsonl.gz",
            },
            {
                "clone_id": "c2",
                "branch": "task/t-1/clone-2",
                "model": "gemini-4-argon",
                "cost_usd": 0.45,
                "passed": True,
                "snapshot_url": "gs://eval-art/snapshots/cg-100-c2.json",
            },
        ],
    }
    rc, _, err = _run_cli(["put-clone-group"], stdin_data=json.dumps(cg_payload))
    assert rc == 0, err

    rc, out, err = _run_cli(["get-clone-group", "cg-100"])
    assert rc == 0, err
    got_cg = json.loads(out)
    assert got_cg["clone_group_id"] == "cg-100"
    assert len(got_cg["variants"]) == 2
    assert got_cg["diff_url"] == "gs://eval-art/clones/cg-100/diff.json"

    # 3. eval_agent_state: missing -> null, put -> get
    rc, out, err = _run_cli(["get-agent-state", "acme-shop"])
    assert rc == 0, err
    assert json.loads(out) is None

    state_file = tmp_path / "agent_state.json"
    state_file.write_text(json.dumps({
        "project_slug": "acme-shop",
        "governance_mode": "human_approval",
        "enabled": True,
        "policy": {
            "daily_experiment_budget_usd": 15.0,
            "monthly_experiment_budget_usd": 200.0,
            "min_sample_size": 5,
            "max_clone_variants": 3,
        },
        "cursors": {"tasks_since_last_batch": 2},
        "spend": {"daily_usd": 1.7, "monthly_usd": 12.4},
    }))
    rc, _, err = _run_cli(["put-agent-state", "--file", str(state_file)])
    assert rc == 0, err

    rc, out, err = _run_cli(["get-agent-state", "--project-slug", "acme-shop"])
    assert rc == 0, err
    got_state = json.loads(out)
    assert got_state["governance_mode"] == "human_approval"
    assert got_state["policy"]["min_sample_size"] == 5
    assert got_state["updated_at"]

    # 4. eval_factory_overrides: put -> list-active-overrides -> resolve-override
    ovr1 = {
        "override_id": "ovr-active-1",
        "analysis_id": "an-100",
        "project_slug": "acme-shop",
        "target_scope": "project_settings",
        "target_id": "p-1",
        "previous_state": {"default_model": "opus"},
        "applied_state": {"default_model": "gemini-4-argon"},
        "status": "active",
        "rollback_after_tasks": 10,
    }
    ovr2 = {
        "override_id": "ovr-active-2",
        "analysis_id": "an-101",
        "project_slug": "other-shop",
        "target_scope": "schedule",
        "status": "active",
        "previous_state": {"cron": "*/15 * * * *"},
        "applied_state": {"cron": "*/30 * * * *"},
    }
    assert _run_cli(["put-override"], stdin_data=json.dumps(ovr1))[0] == 0
    assert _run_cli(["put-override"], stdin_data=json.dumps(ovr2))[0] == 0

    rc, out, err = _run_cli(["list-active-overrides", "--project-slug", "acme-shop"])
    assert rc == 0, err
    active = json.loads(out)
    assert [d["override_id"] for d in active] == ["ovr-active-1"]

    rc, _, err = _run_cli([
        "resolve-override", "ovr-active-1", "--status", "rolled_back", "--note", "Rework regression +25%",
    ])
    assert rc == 0, err

    rc, out, err = _run_cli(["list-active-overrides", "--project-slug", "acme-shop"])
    assert rc == 0, err
    assert json.loads(out) == []

    # Verify updateMask preserved previous_state/applied_state while updating status and resolved_at
    store = factory_store.store_from_env()
    resolved_doc = factory_store.get_override(store, "ovr-active-1")
    assert resolved_doc is not None
    assert resolved_doc["status"] == "rolled_back"
    assert resolved_doc["resolution_note"] == "Rework regression +25%"
    assert resolved_doc["previous_state"] == {"default_model": "opus"}
    assert resolved_doc["applied_state"] == {"default_model": "gemini-4-argon"}
    assert resolved_doc["resolved_at"]

    # Missing override cannot be resolved
    rc, _, err = _run_cli(["resolve-override", "ovr-missing", "--status", "expired"])
    assert rc != 0
    assert "not found" in err


# ---------------------------------------------------------------------------
# Spec, README, templates & skill coverage checks
# ---------------------------------------------------------------------------


def test_eval_fabrica_spec_and_firestore_docs_exist():
    spec_doc = ROOT / "docs" / "specs" / "eval-fabrica-spec.md"
    assert spec_doc.exists() and "EVAL FÁBRICA" in spec_doc.read_text()

    readme = (ROOT / "config" / "firestore" / "README.md").read_text()
    for collection in ("eval_analyses", "eval_clone_groups", "eval_agent_state", "eval_factory_overrides"):
        assert collection in readme
    for url_field in ("report_url", "snapshot_url", "diff_url"):
        assert url_field in readme


def test_lead_templates_and_skills_cover_task_context_clones_and_gcp_serverless_kit(tmp_path):
    lead_prompt = (ROOT / "templates" / "lead" / "system-prompt.md").read_text()
    shared_agents = (ROOT / "templates" / "_shared" / "agents.md").read_text()
    skill = (ROOT / "skills" / "gcp-serverless-kit" / "SKILL.md").read_text()

    for content in (lead_prompt, skill):
        assert "/home/scion/.scion/task-context.json" in content
        assert "scion-tracker" in content
        assert "scion-eval clone-task" in content
        for keyword in (
            "Cloud Run",
            "polling",
            "Spanner",
            "Firestore",
            "AlloyDB Express",
            "GCS",
            "Pub/Sub",
            "BigQuery",
            "Workflows",
            "Cloud Tasks",
            "Cloud Scheduler",
            "Golang",
        ):
            assert keyword in content, f"missing {keyword!r}"

    assert "/home/scion/.scion/task-context.json" in shared_agents
    assert "scion-tracker" in shared_agents
    assert "Cloud Run" in shared_agents and "polling" in shared_agents

    # Compiled kit lead & shared agents also include task-context, clone-task, and serverless kit rules
    spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    compiled_lead = (out / "templates" / "lead" / "system-prompt.md").read_text()
    compiled_shared = (out / "templates" / "_shared" / "agents.md").read_text()
    assert "/home/scion/.scion/task-context.json" in compiled_lead
    assert "scion-eval clone-task" in compiled_lead
    assert "AlloyDB Express" in compiled_lead
    assert "/home/scion/.scion/task-context.json" in compiled_shared
    assert "Cloud Run" in compiled_shared
