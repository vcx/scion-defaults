#!/usr/bin/env python3
"""Serverless factory control plane on Firestore: a standard-library REST client and CLI.

The Hub sync writes agent status, eval agents record evaluation runs, and the dashboard reads both. Everything runs
through the Firestore REST API with IAM credentials, so no client library or provisioned database is needed.
Data contract: docs/plans/2026-10-20-firestore-control-plane.md (and config/firestore/README.md in a compiled kit).

Configuration (environment or flags):
  FIRESTORE_PROJECT   project that holds the database (default: SANDBOX_PROJECT, then APP_GCP_PROJECT)
  FIRESTORE_DATABASE  database ID (default: (default))
  BILLING_PROJECT     quota project sent as x-goog-user-project (default: the Firestore project; L12)
  FIRESTORE_EMULATOR_HOST  host:port of a local Firestore emulator; when set, requests go there without credentials

Usage:
  factory_store.py list-agents [--project-slug SLUG]
  factory_store.py list-eval-runs [--project-slug SLUG] [--limit N]
  factory_store.py put-eval-run --file run.json        # or JSON on stdin
  factory_store.py put-analysis --file analysis.json
  factory_store.py get-analysis [--analysis-id ID | ID]
  factory_store.py list-analyses [--project-slug SLUG] [--status STATUS] [--limit N]
  factory_store.py put-clone-group --file group.json
  factory_store.py get-clone-group [--clone-group-id ID | ID]
  factory_store.py get-agent-state [--project-slug SLUG | SLUG]
  factory_store.py put-agent-state --file state.json
  factory_store.py put-override --file override.json
  factory_store.py list-active-overrides [--project-slug SLUG] [--limit N]
  factory_store.py resolve-override [--override-id ID | ID] --status STATUS [--note TEXT]
  factory_store.py put-snapshot --file snapshot.json
  factory_store.py get-snapshot [--project-slug SLUG] [--snapshot-id ID | ID]
  factory_store.py list-snapshots [--project-slug SLUG] [--limit N]
  factory_store.py --dry-run put-eval-run --file run.json   # print the request, write nothing
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AGENT_STATUS = "factory_agent_status"
EVAL_RUNS = "eval_runs"
CONTROL_STATE_COLLECTION = "factory_control_state"
EVAL_ANALYSES = "eval_analyses"
EVAL_CLONE_GROUPS = "eval_clone_groups"
EVAL_AGENT_STATE = "eval_agent_state"
EVAL_FACTORY_OVERRIDES = "eval_factory_overrides"
EVAL_SNAPSHOTS = "eval_snapshots"
MAX_WRITES_PER_COMMIT = 500
API = "https://firestore.googleapis.com/v1"

EVAL_RUN_REQUIRED = ("run_id", "project_slug", "overall_status", "primary_metric_name", "primary_metric_value")
EVAL_ANALYSIS_REQUIRED = ("analysis_id", "project_slug", "status")
EVAL_CLONE_GROUP_REQUIRED = ("clone_group_id", "project_slug", "source_task_id")
EVAL_AGENT_STATE_REQUIRED = ("project_slug", "governance_mode")
EVAL_OVERRIDE_REQUIRED = ("override_id", "project_slug", "analysis_id", "target_scope", "status")
EVAL_SNAPSHOT_REQUIRED = ("snapshot_id", "project_slug")

ANALYSIS_STATUSES = (
    "drafting",
    "testing",
    "awaiting_approval",
    "applying",
    "monitoring",
    "completed",
    "rolled_back",
    "rejected",
)
ANALYSIS_CREATED_BY = ("eval-agent", "leader", "human")
EXPERIMENT_TYPES = ("task_clone", "canary_override", "replay")
TRACKER_MODES = ("github", "farmtable")
CLONE_TRIGGERED_BY = ("leader", "eval_agent", "human")
ISOLATION_MODES = ("worker_branch", "ephemeral_project")
SELECTION_MODES = ("manual", "eval_agent")
GOVERNANCE_MODES = ("autonomous", "human_approval", "manual")
OVERRIDE_SCOPES = ("project_settings", "template", "agent_patch", "schedule")
OVERRIDE_STATUSES = ("active", "promoted_to_git", "rolled_back", "expired")
OVERRIDE_RESOLVE_STATUSES = ("promoted_to_git", "rolled_back", "expired")
ARTIFACT_URL_FIELDS = ("report_url", "snapshot_url", "diff_url")

_DOC_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------

def _rfc3339(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_value(value: Any) -> dict[str, Any]:
    """Encode a Python value as a Firestore REST `Value`."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, datetime.datetime):
        return {"timestampValue": _rfc3339(value)}
    if isinstance(value, dict):
        return {"mapValue": {"fields": to_fields(value)}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [to_value(v) for v in value]}}
    return {"stringValue": str(value)}


def to_fields(doc: dict[str, Any]) -> dict[str, Any]:
    return {str(k): to_value(v) for k, v in doc.items()}


def from_value(value: dict[str, Any]) -> Any:
    """Decode a Firestore REST `Value`. Timestamps stay RFC 3339 strings."""
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        return value["timestampValue"]
    if "stringValue" in value:
        return value["stringValue"]
    if "mapValue" in value:
        return from_fields(value["mapValue"].get("fields", {}))
    if "arrayValue" in value:
        return [from_value(v) for v in value["arrayValue"].get("values", [])]
    return None


def from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: from_value(v) for k, v in fields.items()}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Token for the attached service account (metadata server), or the active gcloud account."""
    meta_timeout = 10 if os.environ.get("KUBERNETES_SERVICE_HOST") else 3
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=meta_timeout) as resp:
            token = json.loads(resp.read().decode()).get("access_token", "")
            if token:
                return token
    except (OSError, ValueError):
        pass
    try:
        res = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class FirestoreStore:
    """Minimal Firestore REST client. With dry_run=True, requests are printed as JSON lines, not sent."""

    def __init__(self, project: str, database: str = "(default)", billing_project: str | None = None,
                 dry_run: bool = False, timeout: int = 30):
        if not project:
            raise ValueError("Firestore project is not set (FIRESTORE_PROJECT or SANDBOX_PROJECT)")
        self.project = project
        self.database = database or "(default)"
        self.billing_project = billing_project or project
        self.dry_run = dry_run
        self.timeout = timeout
        self._token: str | None = None

    @property
    def api(self) -> str:
        emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
        return f"http://{emulator}/v1" if emulator else API

    @property
    def root(self) -> str:
        return f"projects/{self.project}/databases/{self.database}/documents"

    def doc_name(self, collection: str, doc_id: str) -> str:
        return f"{self.root}/{collection}/{doc_id}"

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        if self.dry_run:
            print(json.dumps({"method": method, "url": url, "body": body}, sort_keys=True))
            return [] if url.endswith(":runQuery") else {}
        if self._token is None:
            self._token = "owner" if os.environ.get("FIRESTORE_EMULATOR_HOST") else get_access_token()
            if not self._token:
                raise RuntimeError("no Google credentials: attach a service account or run gcloud auth login")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-goog-user-project": self.billing_project,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="ignore")[:500]
            raise RuntimeError(f"Firestore {method} {url} failed: HTTP {err.code}: {detail}") from err

    def commit(self, writes: list[dict[str, Any]]) -> int:
        """Apply writes in batches of at most 500. Returns the number of writes sent."""
        for start in range(0, len(writes), MAX_WRITES_PER_COMMIT):
            self._request("POST", f"{self.api}/{self.root}:commit",
                          {"writes": writes[start:start + MAX_WRITES_PER_COMMIT]})
        return len(writes)

    def run_query(self, structured_query: dict[str, Any]) -> list[dict[str, Any]]:
        """Return decoded documents (with `_name`) matching a structured query."""
        rows = self._request("POST", f"{self.api}/{self.root}:runQuery", {"structuredQuery": structured_query})
        docs = []
        for row in rows or []:
            doc = row.get("document")
            if doc:
                decoded = from_fields(doc.get("fields", {}))
                decoded["_name"] = doc["name"]
                docs.append(decoded)
        return docs

    def get_document(self, collection: str, doc_id: str) -> dict | None:
        """Fetch and decode a single document. Return None on 404."""
        try:
            doc = self._request("GET", f"{self.api}/{self.doc_name(collection, doc_id)}")
            return from_fields(doc.get("fields", {}))
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return None
            raise

    def upsert_document(self, collection: str, doc_id: str, data: dict) -> None:
        """Upsert a document, replacing entirely. 'REQUEST_TIME' values become server timestamps."""
        fields = {}
        transforms = []
        for k, v in data.items():
            if v == "REQUEST_TIME":
                transforms.append({"fieldPath": k, "setToServerValue": "REQUEST_TIME"})
            else:
                fields[k] = v
        writes = [{"update": {"name": self.doc_name(collection, doc_id), "fields": to_fields(fields)}}]
        if transforms:
            writes[0]["updateTransforms"] = transforms
        self.commit(writes)

    def list_documents(self, collection: str) -> list[dict]:
        """List all documents in a collection, following pagination."""
        docs = []
        page_token = None
        while True:
            url = f"{self.api}/{self.root}/{collection}?pageSize=300"
            if page_token:
                url += f"&pageToken={page_token}"
            resp = self._request("GET", url)
            for doc in resp.get("documents", []):
                docs.append(from_fields(doc.get("fields", {})))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return docs


def store_from_env(dry_run: bool = False, project: str | None = None, database: str | None = None) -> FirestoreStore:
    project = project or os.environ.get("FIRESTORE_PROJECT") or os.environ.get("SANDBOX_PROJECT") \
        or os.environ.get("APP_GCP_PROJECT", "")
    return FirestoreStore(
        project=project,
        database=database or os.environ.get("FIRESTORE_DATABASE", "(default)"),
        billing_project=os.environ.get("BILLING_PROJECT") or project,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Agent status (written by the Hub sync)
# ---------------------------------------------------------------------------

def agent_doc_id(project_slug: str, agent_name: str) -> str:
    return f"{project_slug}__{agent_name}"


def agent_status_writes(store: FirestoreStore, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert one document per agent; `synced_at` is set to the server's commit time."""
    return [{
        "update": {"name": store.doc_name(AGENT_STATUS, agent_doc_id(r["project_slug"], r["agent_name"])),
                   "fields": to_fields(r)},
        "updateTransforms": [{"fieldPath": "synced_at", "setToServerValue": "REQUEST_TIME"}],
    } for r in rows]


def prune_agent_status(store: FirestoreStore, older_than: datetime.datetime) -> int:
    """Delete agent status documents not refreshed since `older_than` (deleted agents)."""
    stale = store.run_query({
        "from": [{"collectionId": AGENT_STATUS}],
        "where": {"fieldFilter": {"field": {"fieldPath": "synced_at"}, "op": "LESS_THAN",
                                  "value": to_value(older_than)}},
    })
    return store.commit([{"delete": d["_name"]} for d in stale]) if stale else 0


def list_agents(store: FirestoreStore, project_slug: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"from": [{"collectionId": AGENT_STATUS}],
                             "orderBy": [{"field": {"fieldPath": "agent_name"}, "direction": "ASCENDING"}]}
    if project_slug:
        query["where"] = {"fieldFilter": {"field": {"fieldPath": "project_slug"}, "op": "EQUAL",
                                          "value": to_value(project_slug)}}
    return store.run_query(query)


# ---------------------------------------------------------------------------
# Evaluation runs (written by eval agents)
# ---------------------------------------------------------------------------

def validate_eval_run(run: dict[str, Any]) -> list[str]:
    problems = [f"missing required field {k}" for k in EVAL_RUN_REQUIRED if run.get(k) in (None, "")]
    if run.get("run_id") and not _DOC_ID.fullmatch(str(run["run_id"])):
        problems.append("run_id may only contain letters, digits, '.', '_' and '-'")
    value = run.get("primary_metric_value")
    if value is not None and not isinstance(value, (int, float)):
        problems.append("primary_metric_value must be a number")
    for idx, req in enumerate(run.get("requirements") or []):
        if not isinstance(req, dict) or "id" not in req or "status" not in req:
            problems.append(f"requirements[{idx}] must be an object with id and status")
    return problems


def put_eval_run(store: FirestoreStore, run: dict[str, Any]) -> None:
    problems = validate_eval_run(run)
    if problems:
        raise ValueError("; ".join(problems))
    fields = {k: v for k, v in run.items() if k != "evaluated_at"}
    store.commit([{
        "update": {"name": store.doc_name(EVAL_RUNS, str(run["run_id"])), "fields": to_fields(fields)},
        "updateTransforms": [{"fieldPath": "evaluated_at", "setToServerValue": "REQUEST_TIME"}],
    }])


def list_eval_runs(store: FirestoreStore, project_slug: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"from": [{"collectionId": EVAL_RUNS}],
                             "orderBy": [{"field": {"fieldPath": "evaluated_at"}, "direction": "DESCENDING"}],
                             "limit": limit}
    if project_slug:
        query["where"] = {"fieldFilter": {"field": {"fieldPath": "project_slug"}, "op": "EQUAL",
                                          "value": to_value(project_slug)}}
    return store.run_query(query)


# ---------------------------------------------------------------------------
# Helpers for Eval Fabrica collections
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_doc_id(value: Any, field_name: str, problems: list[str]) -> None:
    if value and not _DOC_ID.fullmatch(str(value)):
        problems.append(f"{field_name} may only contain letters, digits, '.', '_' and '-'")


def _validate_artifact_url(value: Any, field_name: str, problems: list[str]) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        problems.append(f"{field_name} must be a string URL")
        return
    if any(ch.isspace() for ch in value):
        problems.append(f"{field_name} must not contain whitespace")
        return
    if value.startswith(("gs://", "https://", "http://")):
        rest = value.split("://", 1)[1]
        if not rest or rest.startswith("/"):
            problems.append(f"{field_name} must include a bucket/host")
        return
    if value.startswith(("?view=", "/")):
        return
    problems.append(
        f"{field_name} must be a canonical GCS/HTTP(S) URL or App Shell route (gs://..., https://..., ?view=...)"
    )


def _validate_artifact_fields(container: dict[str, Any], prefix: str, problems: list[str],
                              extra_fields: tuple[str, ...] = ()) -> None:
    for field in ARTIFACT_URL_FIELDS + extra_fields:
        if field in container:
            _validate_artifact_url(container.get(field), f"{prefix}{field}", problems)


def _where_filter(filters: list[tuple[str, str, Any]]) -> dict[str, Any] | None:
    if not filters:
        return None
    if len(filters) == 1:
        field, op, value = filters[0]
        return {"fieldFilter": {"field": {"fieldPath": field}, "op": op, "value": to_value(value)}}
    return {
        "compositeFilter": {
            "op": "AND",
            "filters": [
                {"fieldFilter": {"field": {"fieldPath": field}, "op": op, "value": to_value(value)}}
                for field, op, value in filters
            ],
        }
    }


def _build_upsert_write(store: FirestoreStore, collection: str, doc_id: str, doc: dict[str, Any],
                        *, created_field: str | None = None, updated_field: str | None = None,
                        extra_time_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    transforms: list[dict[str, Any]] = []
    skip_fields: set[str] = set()
    if updated_field:
        skip_fields.add(updated_field)
        transforms.append({"fieldPath": updated_field, "setToServerValue": "REQUEST_TIME"})
    if created_field and doc.get(created_field) in (None, "", "REQUEST_TIME"):
        skip_fields.add(created_field)
        transforms.append({"fieldPath": created_field, "setToServerValue": "REQUEST_TIME"})
    for field in extra_time_fields:
        if doc.get(field) == "REQUEST_TIME":
            skip_fields.add(field)
            transforms.append({"fieldPath": field, "setToServerValue": "REQUEST_TIME"})
    fields = {k: v for k, v in doc.items() if k not in skip_fields}
    write: dict[str, Any] = {
        "update": {"name": store.doc_name(collection, doc_id), "fields": to_fields(fields)}
    }
    if transforms:
        write["updateTransforms"] = transforms
    return write


# ---------------------------------------------------------------------------
# Eval Fabrica: eval_analyses
# ---------------------------------------------------------------------------

def validate_analysis(analysis: dict[str, Any]) -> list[str]:
    if not isinstance(analysis, dict):
        return ["analysis must be a JSON object"]
    problems = [f"missing required field {k}" for k in EVAL_ANALYSIS_REQUIRED if analysis.get(k) in (None, "")]
    _validate_doc_id(analysis.get("analysis_id"), "analysis_id", problems)
    status = analysis.get("status")
    if status not in (None, "") and status not in ANALYSIS_STATUSES:
        problems.append(f"status must be one of {', '.join(ANALYSIS_STATUSES)}")
    created_by = analysis.get("created_by")
    if created_by not in (None, "") and created_by not in ANALYSIS_CREATED_BY:
        problems.append(f"created_by must be one of {', '.join(ANALYSIS_CREATED_BY)}")
    for list_field in ("task_ids", "eval_run_ids"):
        val = analysis.get(list_field)
        if val is not None and (not isinstance(val, list) or not all(isinstance(item, str) for item in val)):
            problems.append(f"{list_field} must be a list of strings")
    action_log = analysis.get("action_log")
    if action_log is not None and (not isinstance(action_log, list) or not all(isinstance(a, dict) for a in action_log)):
        problems.append("action_log must be a list of objects")
    for map_field in ("bq_evidence", "hypothesis", "experiment", "results", "artifacts", "governance"):
        val = analysis.get(map_field)
        if val is not None and not isinstance(val, dict):
            problems.append(f"{map_field} must be an object")
    experiment = analysis.get("experiment")
    if isinstance(experiment, dict):
        exp_type = experiment.get("type")
        if exp_type not in (None, "") and exp_type not in EXPERIMENT_TYPES:
            problems.append(f"experiment.type must be one of {', '.join(EXPERIMENT_TYPES)}")
    _validate_artifact_fields(analysis, "", problems)
    artifacts = analysis.get("artifacts")
    if isinstance(artifacts, dict):
        _validate_artifact_fields(artifacts, "artifacts.", problems)
    return problems


def put_analysis(store: FirestoreStore, analysis: dict[str, Any]) -> None:
    problems = validate_analysis(analysis)
    if problems:
        raise ValueError("; ".join(problems))
    write = _build_upsert_write(
        store,
        EVAL_ANALYSES,
        str(analysis["analysis_id"]),
        analysis,
        created_field="created_at",
        updated_field="updated_at",
    )
    store.commit([write])


def get_analysis(store: FirestoreStore, analysis_id: str) -> dict[str, Any] | None:
    if not analysis_id or not _DOC_ID.fullmatch(str(analysis_id)):
        raise ValueError("analysis_id must be a valid document ID")
    return store.get_document(EVAL_ANALYSES, str(analysis_id))


def list_analyses(
    store: FirestoreStore,
    project_slug: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if status is not None and status not in ANALYSIS_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ANALYSIS_STATUSES)}")
    filters: list[tuple[str, str, Any]] = []
    if project_slug:
        filters.append(("project_slug", "EQUAL", project_slug))
    if status:
        filters.append(("status", "EQUAL", status))
    query: dict[str, Any] = {
        "from": [{"collectionId": EVAL_ANALYSES}],
        "orderBy": [{"field": {"fieldPath": "updated_at"}, "direction": "DESCENDING"}],
        "limit": limit,
    }
    where = _where_filter(filters)
    if where:
        query["where"] = where
    return store.run_query(query)


# ---------------------------------------------------------------------------
# Eval Fabrica: eval_clone_groups
# ---------------------------------------------------------------------------

def validate_clone_group(group: dict[str, Any]) -> list[str]:
    if not isinstance(group, dict):
        return ["clone group must be a JSON object"]
    problems = [f"missing required field {k}" for k in EVAL_CLONE_GROUP_REQUIRED if group.get(k) in (None, "")]
    _validate_doc_id(group.get("clone_group_id"), "clone_group_id", problems)
    variants = group.get("variants")
    if not isinstance(variants, list) or len(variants) < 1:
        problems.append("variants must be a non-empty list of objects")
    else:
        for idx, variant in enumerate(variants):
            if isinstance(variant, dict) and variant.get("clone_id") not in (None, ""):
                _validate_artifact_fields(
                    variant,
                    f"variants[{idx}].",
                    problems,
                    extra_fields=("transcript_uri", "transcript_proxy_path"),
                )
            else:
                problems.append(f"variants[{idx}] must be an object with clone_id")
    for field, allowed in (
        ("source_tracker_mode", TRACKER_MODES),
        ("triggered_by", CLONE_TRIGGERED_BY),
        ("isolation_mode", ISOLATION_MODES),
        ("selection_mode", SELECTION_MODES),
    ):
        val = group.get(field)
        if val not in (None, "") and val not in allowed:
            problems.append(f"{field} must be one of {', '.join(allowed)}")
    _validate_artifact_fields(group, "", problems)
    artifacts = group.get("artifacts")
    if isinstance(artifacts, dict):
        _validate_artifact_fields(artifacts, "artifacts.", problems)
    return problems


def put_clone_group(store: FirestoreStore, group: dict[str, Any]) -> None:
    problems = validate_clone_group(group)
    if problems:
        raise ValueError("; ".join(problems))
    write = _build_upsert_write(
        store,
        EVAL_CLONE_GROUPS,
        str(group["clone_group_id"]),
        group,
        created_field="created_at",
        updated_field="updated_at",
        extra_time_fields=("closed_at",),
    )
    store.commit([write])


def get_clone_group(store: FirestoreStore, clone_group_id: str) -> dict[str, Any] | None:
    if not clone_group_id or not _DOC_ID.fullmatch(str(clone_group_id)):
        raise ValueError("clone_group_id must be a valid document ID")
    return store.get_document(EVAL_CLONE_GROUPS, str(clone_group_id))


# ---------------------------------------------------------------------------
# Eval Fabrica: eval_agent_state
# ---------------------------------------------------------------------------

def validate_agent_state(state: dict[str, Any]) -> list[str]:
    if not isinstance(state, dict):
        return ["agent state must be a JSON object"]
    problems = [f"missing required field {k}" for k in EVAL_AGENT_STATE_REQUIRED if state.get(k) in (None, "")]
    _validate_doc_id(state.get("project_slug"), "project_slug", problems)
    mode = state.get("governance_mode")
    if mode not in (None, "") and mode not in GOVERNANCE_MODES:
        problems.append(f"governance_mode must be one of {', '.join(GOVERNANCE_MODES)}")
    enabled = state.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        problems.append("enabled must be a boolean")
    policy = state.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            problems.append("policy must be an object")
        else:
            for num_field in (
                "daily_experiment_budget_usd",
                "monthly_experiment_budget_usd",
                "max_concurrent_analyses",
                "min_sample_size",
                "max_clone_variants",
                "rollback_window_tasks",
                "rollback_regression_threshold_pct",
            ):
                val = policy.get(num_field)
                if val is not None and (not _is_number(val) or val < 0):
                    problems.append(f"policy.{num_field} must be a non-negative number")
    cursors = state.get("cursors")
    if cursors is not None and not isinstance(cursors, dict):
        problems.append("cursors must be an object")
    spend = state.get("spend")
    if spend is not None:
        if not isinstance(spend, dict):
            problems.append("spend must be an object")
        else:
            for num_field in ("daily_usd", "monthly_usd"):
                val = spend.get(num_field)
                if val is not None and (not _is_number(val) or val < 0):
                    problems.append(f"spend.{num_field} must be a non-negative number")
    return problems


def get_agent_state(store: FirestoreStore, project_slug: str) -> dict[str, Any] | None:
    if not project_slug or not _DOC_ID.fullmatch(str(project_slug)):
        raise ValueError("project_slug must be a valid document ID")
    return store.get_document(EVAL_AGENT_STATE, str(project_slug))


def put_agent_state(store: FirestoreStore, state: dict[str, Any]) -> None:
    problems = validate_agent_state(state)
    if problems:
        raise ValueError("; ".join(problems))
    write = _build_upsert_write(
        store,
        EVAL_AGENT_STATE,
        str(state["project_slug"]),
        state,
        updated_field="updated_at",
    )
    store.commit([write])


# ---------------------------------------------------------------------------
# Eval Fabrica: eval_factory_overrides
# ---------------------------------------------------------------------------

def validate_override(override: dict[str, Any]) -> list[str]:
    if not isinstance(override, dict):
        return ["override must be a JSON object"]
    problems = [f"missing required field {k}" for k in EVAL_OVERRIDE_REQUIRED if override.get(k) in (None, "")]
    _validate_doc_id(override.get("override_id"), "override_id", problems)
    scope = override.get("target_scope")
    if scope not in (None, "") and scope not in OVERRIDE_SCOPES:
        problems.append(f"target_scope must be one of {', '.join(OVERRIDE_SCOPES)}")
    status = override.get("status")
    if status not in (None, "") and status not in OVERRIDE_STATUSES:
        problems.append(f"status must be one of {', '.join(OVERRIDE_STATUSES)}")
    for map_field in ("previous_state", "applied_state"):
        val = override.get(map_field)
        if val is not None and not isinstance(val, dict):
            problems.append(f"{map_field} must be an object")
    for int_field in ("rollback_after_tasks", "tasks_observed_since_apply"):
        val = override.get(int_field)
        if val is not None and (not _is_int(val) or val < 0):
            problems.append(f"{int_field} must be a non-negative integer")
    return problems


def put_override(store: FirestoreStore, override: dict[str, Any]) -> None:
    problems = validate_override(override)
    if problems:
        raise ValueError("; ".join(problems))
    write = _build_upsert_write(
        store,
        EVAL_FACTORY_OVERRIDES,
        str(override["override_id"]),
        override,
        created_field="applied_at",
        extra_time_fields=("resolved_at",),
    )
    store.commit([write])


def get_override(store: FirestoreStore, override_id: str) -> dict[str, Any] | None:
    if not override_id or not _DOC_ID.fullmatch(str(override_id)):
        raise ValueError("override_id must be a valid document ID")
    return store.get_document(EVAL_FACTORY_OVERRIDES, str(override_id))


def list_active_overrides(
    store: FirestoreStore,
    project_slug: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    filters: list[tuple[str, str, Any]] = [("status", "EQUAL", "active")]
    if project_slug:
        filters.append(("project_slug", "EQUAL", project_slug))
    query: dict[str, Any] = {
        "from": [{"collectionId": EVAL_FACTORY_OVERRIDES}],
        "where": _where_filter(filters),
        "orderBy": [{"field": {"fieldPath": "applied_at"}, "direction": "DESCENDING"}],
        "limit": limit,
    }
    return store.run_query(query)


def resolve_override(
    store: FirestoreStore,
    override_id: str,
    status: str,
    resolution_note: str | None = None,
) -> None:
    if not override_id or not _DOC_ID.fullmatch(str(override_id)):
        raise ValueError("override_id must be a valid document ID")
    if status not in OVERRIDE_RESOLVE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(OVERRIDE_RESOLVE_STATUSES)}")
    if not store.dry_run:
        existing = get_override(store, override_id)
        if existing is None:
            raise ValueError(f"override not found: {override_id}")
    fields: dict[str, Any] = {"status": status}
    if resolution_note not in (None, ""):
        fields["resolution_note"] = resolution_note
    store.commit([{
        "update": {
            "name": store.doc_name(EVAL_FACTORY_OVERRIDES, str(override_id)),
            "fields": to_fields(fields),
        },
        "updateMask": {"fieldPaths": list(fields.keys())},
        "updateTransforms": [{"fieldPath": "resolved_at", "setToServerValue": "REQUEST_TIME"}],
    }])


# ---------------------------------------------------------------------------
# Eval Fabrica v2: eval_snapshots
# ---------------------------------------------------------------------------

def snapshot_doc_id(project_slug: str, snapshot_id: str) -> str:
    slug = str(project_slug).strip()
    sid = str(snapshot_id).strip()
    if sid.startswith(f"{slug}__"):
        return sid
    return f"{slug}__{sid}"


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    if not isinstance(snapshot, dict):
        return ["snapshot must be a JSON object"]
    problems = [f"missing required field {k}" for k in EVAL_SNAPSHOT_REQUIRED if snapshot.get(k) in (None, "")]
    _validate_doc_id(snapshot.get("snapshot_id"), "snapshot_id", problems)
    _validate_doc_id(snapshot.get("project_slug"), "project_slug", problems)
    if snapshot.get("project_slug") and snapshot.get("snapshot_id"):
        _validate_doc_id(
            snapshot_doc_id(str(snapshot["project_slug"]), str(snapshot["snapshot_id"])),
            "snapshot_doc_id",
            problems,
        )

    finops = snapshot.get("finops")
    if finops is not None:
        if not isinstance(finops, dict):
            problems.append("finops must be an object")
        else:
            for num_field in (
                "total_cost_usd",
                "productive_cost_usd",
                "rework_cost_usd",
                "heartbeat_tax_usd",
                "conservation_drift_usd",
            ):
                val = finops.get(num_field)
                if val is not None and not _is_number(val):
                    problems.append(f"finops.{num_field} must be a number")
            if all(
                _is_number(finops.get(k))
                for k in ("total_cost_usd", "productive_cost_usd", "rework_cost_usd", "heartbeat_tax_usd")
            ):
                drift = abs(
                    float(finops["total_cost_usd"])
                    - (
                        float(finops["productive_cost_usd"])
                        + float(finops["rework_cost_usd"])
                        + float(finops["heartbeat_tax_usd"])
                    )
                )
                if round(drift, 6) > 0.000001:
                    problems.append(f"finops violates conservation law (drift={drift:.6f})")

    traj_sum = snapshot.get("trajectories_summary")
    if traj_sum is not None and not isinstance(traj_sum, (dict, list)):
        problems.append("trajectories_summary must be an object or list")

    for map_field in ("conservation_check", "agents_summary", "system_summary"):
        val = snapshot.get(map_field)
        if val is not None and not isinstance(val, dict):
            problems.append(f"{map_field} must be an object")

    refs = snapshot.get("transcript_refs")
    if refs is not None:
        if not isinstance(refs, list):
            problems.append("transcript_refs must be a list of objects")
        else:
            for idx, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    problems.append(f"transcript_refs[{idx}] must be an object")
                elif "proxy_url" in ref:
                    _validate_artifact_url(ref.get("proxy_url"), f"transcript_refs[{idx}].proxy_url", problems)

    return problems


def put_snapshot(store: FirestoreStore, snapshot: dict[str, Any]) -> None:
    problems = validate_snapshot(snapshot)
    if problems:
        raise ValueError("; ".join(problems))
    doc_id = snapshot_doc_id(str(snapshot["project_slug"]), str(snapshot["snapshot_id"]))
    write = _build_upsert_write(
        store,
        EVAL_SNAPSHOTS,
        doc_id,
        snapshot,
        created_field="published_at",
        updated_field="updated_at",
    )
    store.commit([write])


def get_snapshot(store: FirestoreStore, project_slug_or_doc_id: str, snapshot_id: str | None = None) -> dict[str, Any] | None:
    if snapshot_id:
        if not _DOC_ID.fullmatch(str(project_slug_or_doc_id)):
            raise ValueError("project_slug must be a valid document ID")
        if not _DOC_ID.fullmatch(str(snapshot_id)):
            raise ValueError("snapshot_id must be a valid document ID")
        doc_id = snapshot_doc_id(project_slug_or_doc_id, snapshot_id)
    else:
        if not project_slug_or_doc_id or not _DOC_ID.fullmatch(str(project_slug_or_doc_id)):
            raise ValueError("snapshot document ID must be a valid document ID")
        doc_id = str(project_slug_or_doc_id)
    return store.get_document(EVAL_SNAPSHOTS, doc_id)


def list_snapshots(
    store: FirestoreStore,
    project_slug: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    filters: list[tuple[str, str, Any]] = []
    if project_slug:
        filters.append(("project_slug", "EQUAL", project_slug))
    query: dict[str, Any] = {
        "from": [{"collectionId": EVAL_SNAPSHOTS}],
        "orderBy": [{"field": {"fieldPath": "published_at"}, "direction": "DESCENDING"}],
        "limit": limit,
    }
    where = _where_filter(filters)
    if where:
        query["where"] = where
    return store.run_query(query)


# ---------------------------------------------------------------------------
# Control State (written by watchdog)
# ---------------------------------------------------------------------------

def load_control_state(store, state_id: str) -> dict:
    default_state = {"restarts": {}, "alerted": [], "last_user_msg_ts": ""}
    # store could be a duck-typed fake_firestore that implements get_document
    if hasattr(store, "get_document"):
        doc = store.get_document(CONTROL_STATE_COLLECTION, state_id)
    else:
        # Our FirestoreStore _request can do GET
        try:
            doc = store._request("GET", f"{store.api}/{store.doc_name(CONTROL_STATE_COLLECTION, state_id)}")
            if "fields" in doc:
                doc = from_fields(doc["fields"])
            else:
                doc = None
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                doc = None
            else:
                raise

    if doc and "state_json" in doc:
        try:
            loaded = json.loads(doc["state_json"])
            default_state.update(loaded)
        except json.JSONDecodeError:
            pass
    return default_state


def save_control_state(store, state_id: str, state: dict) -> None:
    state_json = json.dumps(state)
    fields = {
        "state_json": state_json,
    }

    if hasattr(store, "upsert_document"):
        store.upsert_document(CONTROL_STATE_COLLECTION, state_id, {"state_json": state_json, "updated_at": "REQUEST_TIME"})
    else:
        writes = [{
            "update": {"name": store.doc_name(CONTROL_STATE_COLLECTION, state_id), "fields": to_fields(fields)},
            "updateTransforms": [{"fieldPath": "updated_at", "setToServerValue": "REQUEST_TIME"}],
        }]
        store.commit(writes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_json_arg(file_path: str | None) -> Any:
    if file_path:
        with open(file_path, encoding="utf-8") as fh:
            return json.loads(fh.read())
    return json.loads(sys.stdin.read())


def _resolve_id_arg(positional: str | None, flagged: str | None, label: str) -> str:
    value = flagged or positional
    if not value:
        raise ValueError(f"missing {label}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory control plane on Firestore")
    parser.add_argument("--project", help="Firestore project (default: FIRESTORE_PROJECT or SANDBOX_PROJECT)")
    parser.add_argument("--database", help="Firestore database ID (default: FIRESTORE_DATABASE or (default))")
    parser.add_argument("--dry-run", action="store_true", help="print requests instead of sending them")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_agents = sub.add_parser("list-agents", help="list synced agent status documents")
    p_agents.add_argument("--project-slug")

    p_runs = sub.add_parser("list-eval-runs", help="list recent evaluation runs")
    p_runs.add_argument("--project-slug")
    p_runs.add_argument("--limit", type=int, default=20)

    p_put = sub.add_parser("put-eval-run", help="record one evaluation run (JSON object)")
    p_put.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_put_an = sub.add_parser("put-analysis", help="record or update an eval_analyses document (JSON object)")
    p_put_an.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_get_an = sub.add_parser("get-analysis", help="fetch an eval_analyses document by analysis_id")
    p_get_an.add_argument("analysis_id_pos", nargs="?", metavar="ANALYSIS_ID")
    p_get_an.add_argument("--analysis-id")

    p_list_an = sub.add_parser("list-analyses", help="list eval_analyses documents")
    p_list_an.add_argument("--project-slug")
    p_list_an.add_argument("--status", choices=ANALYSIS_STATUSES)
    p_list_an.add_argument("--limit", type=int, default=20)

    p_put_cg = sub.add_parser("put-clone-group", help="record or update an eval_clone_groups document (JSON object)")
    p_put_cg.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_get_cg = sub.add_parser("get-clone-group", help="fetch an eval_clone_groups document by clone_group_id")
    p_get_cg.add_argument("clone_group_id_pos", nargs="?", metavar="CLONE_GROUP_ID")
    p_get_cg.add_argument("--clone-group-id")

    p_get_state = sub.add_parser("get-agent-state", help="fetch eval_agent_state for a project_slug")
    p_get_state.add_argument("project_slug_pos", nargs="?", metavar="PROJECT_SLUG")
    p_get_state.add_argument("--project-slug")

    p_put_state = sub.add_parser("put-agent-state", help="record or update eval_agent_state (JSON object)")
    p_put_state.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_put_ovr = sub.add_parser("put-override", help="record an eval_factory_overrides document (JSON object)")
    p_put_ovr.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_list_ovr = sub.add_parser("list-active-overrides", help="list active eval_factory_overrides documents")
    p_list_ovr.add_argument("--project-slug")
    p_list_ovr.add_argument("--limit", type=int, default=50)

    p_res_ovr = sub.add_parser("resolve-override", help="resolve an active override (promoted_to_git | rolled_back | expired)")
    p_res_ovr.add_argument("override_id_pos", nargs="?", metavar="OVERRIDE_ID")
    p_res_ovr.add_argument("--override-id")
    p_res_ovr.add_argument("--status", required=True, choices=OVERRIDE_RESOLVE_STATUSES)
    p_res_ovr.add_argument("--note", help="optional resolution note")

    p_put_snap = sub.add_parser("put-snapshot", help="record or update an eval_snapshots document (JSON object)")
    p_put_snap.add_argument("--file", help="path to a JSON file (default: stdin)")

    p_get_snap = sub.add_parser("get-snapshot", help="fetch an eval_snapshots document by project_slug and snapshot_id (or doc ID)")
    p_get_snap.add_argument("first_pos", nargs="?", metavar="PROJECT_OR_DOC_ID")
    p_get_snap.add_argument("second_pos", nargs="?", metavar="SNAPSHOT_ID")
    p_get_snap.add_argument("--project-slug")
    p_get_snap.add_argument("--snapshot-id")

    p_list_snap = sub.add_parser("list-snapshots", help="list eval_snapshots documents")
    p_list_snap.add_argument("--project-slug")
    p_list_snap.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)

    try:
        store = store_from_env(dry_run=args.dry_run, project=args.project, database=args.database)
        if args.cmd == "list-agents":
            result: Any = list_agents(store, args.project_slug)
        elif args.cmd == "list-eval-runs":
            result = list_eval_runs(store, args.project_slug, args.limit)
        elif args.cmd == "put-eval-run":
            put_eval_run(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "put-analysis":
            put_analysis(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "get-analysis":
            result = get_analysis(store, _resolve_id_arg(args.analysis_id_pos, args.analysis_id, "analysis_id"))
        elif args.cmd == "list-analyses":
            result = list_analyses(store, args.project_slug, args.status, args.limit)
        elif args.cmd == "put-clone-group":
            put_clone_group(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "get-clone-group":
            result = get_clone_group(
                store, _resolve_id_arg(args.clone_group_id_pos, args.clone_group_id, "clone_group_id")
            )
        elif args.cmd == "get-agent-state":
            result = get_agent_state(
                store, _resolve_id_arg(args.project_slug_pos, args.project_slug, "project_slug")
            )
        elif args.cmd == "put-agent-state":
            put_agent_state(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "put-override":
            put_override(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "list-active-overrides":
            result = list_active_overrides(store, args.project_slug, args.limit)
        elif args.cmd == "resolve-override":
            resolve_override(
                store,
                _resolve_id_arg(args.override_id_pos, args.override_id, "override_id"),
                args.status,
                resolution_note=args.note,
            )
            if not args.dry_run:
                print("resolved", file=sys.stderr)
            return 0
        elif args.cmd == "put-snapshot":
            put_snapshot(store, _read_json_arg(args.file))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        elif args.cmd == "get-snapshot":
            if args.project_slug and (args.snapshot_id or args.first_pos):
                sid = args.snapshot_id or args.first_pos
                result = get_snapshot(store, args.project_slug, sid)
            elif args.first_pos and args.second_pos:
                result = get_snapshot(store, args.first_pos, args.second_pos)
            else:
                doc_or_sid = args.snapshot_id or args.first_pos
                if not doc_or_sid:
                    raise ValueError("missing snapshot_id (pass --project-slug SLUG --snapshot-id ID or SLUG__ID)")
                result = get_snapshot(store, doc_or_sid)
        elif args.cmd == "list-snapshots":
            result = list_snapshots(store, args.project_slug, args.limit)
        else:
            raise ValueError(f"unknown command: {args.cmd}")

        if not args.dry_run:
            print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
