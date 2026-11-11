---
name: gcp-serverless-kit
description: Guide for project lead and specialist agents on recording /home/scion/.scion/task-context.json via scion-tracker/ft on task claim, triggering Clones de Tarefas (scion-eval clone-task), and building or reviewing tasks targeting the GCP Serverless Kit (Cloud Run services/jobs/workers without polling, Spanner, Firestore, AlloyDB Express, GCS, Pub/Sub, BigQuery, Workflows, Cloud Tasks, Cloud Scheduler, Golang).
---

# GCP Serverless Kit & Task Attribution Guide

Use this skill whenever a project `lead` or specialist claims a task, evaluates competing approaches with task clones, or plans, implements, and reviews work targeting the GCP Serverless Kit.

---

## 1. Record `/home/scion/.scion/task-context.json` on Task Claim

Runtime telemetry (`scion-analytics`, `_AllLogs`), Cloud Trace (`_AllSpans`), and `scion-eval transcript-ship` correlate costs, latency, and transcripts to tasks **without modifying upstream `sciontool`**. That correlation depends on `/home/scion/.scion/task-context.json`.

### When claiming a task

- **Preferred (`scion-tracker`):**
  ```bash
  scion-tracker claim <task_id> --role <agent_role>
  ```
  In `github` mode this updates the linked Farm Table task; in `farmtable` (Walgit) mode it claims directly in Farm Table (`ft`). Either way it writes `/home/scion/.scion/task-context.json`.

- **When claiming via `ft` directly:**
  1. Claim the task in Farm Table:
     ```bash
     ft task claim <task_id> --agent "$SCION_AGENT_NAME"
     ```
  2. Write `/home/scion/.scion/task-context.json` with the active task attribution fields:
     ```json
     {
       "task_id": "<ft_task_uuid>",
       "parent_task_id": "<parent_task_uuid_or_null>",
       "clone_group_id": "<clone_group_id_or_null>",
       "clone_id": "<clone_id_or_null>",
       "template_name": "<applied_template>",
       "template_hash": "<sha256_or_empty>",
       "agent_role": "<role>"
     }
     ```
  3. Keep `RemoteData.eval_fabrica` and labels (`clone-group:...`, `clone:...`, `model:...`, `template:...`) aligned when creating or updating subtasks.

- **Subtask vs. parent closure boundary:**
  - Technical subtasks and clone subtasks in Farm Table are closed via `ft task close <task_id>` (`CloseTask`) once verified (`status:verified`) so downstream `BLOCKED_BY` tasks unlock and `EVAL FÁBRICA` ingestion triggers fire.
  - Parent PO macro tasks/issues stay open for PO acceptance unless configured otherwise.

---

## 2. Trigger `Clones de Tarefas` (`scion-eval clone-task`)

Use **Task Clones** to run 2–5 isolated variants of the same task in parallel on the exact same `base_commit_sha`:
- **Exploratory UI/UX:** comparing 2–5 frontend designs using fast/low-cost models.
- **Backend architecture trade-offs:** evaluating 2 competing designs with unit/integration benchmarks instead of guessing.
- **Hypothesis testing:** paired Control vs. Treatment runs anchored to an `eval_analyses` document.

### Prerequisites & execution

1. Confirm the Hub project uses `workspaceMode: "per-agent"` (never `shared` or `worktree-per-agent` for clones).
2. Prepare a variants spec (`variants.json`) defining `clone_id`, `model`, `thinking_level`, `template`, and `role` (`control` / `treatment`).
3. Trigger the clone group as `lead`:
   ```bash
   scion-eval clone-task --task <task_id> --variants variants.json --triggered-by leader
   ```
4. **Isolation rules:**
   - Each clone works in its own Farm Table subtask (`ParentTaskId = <task_id>`) and its own Walgit/Git branch `task/<task_id>/clone-<k>`.
   - Clone workers push **only** to `task/<task_id>/clone-<k>`. Never push from a clone branch directly to `main`.
   - When all clone subtasks close, Farm Table unblocks the `BLOCKED_BY` `decision` task.
5. **Select and integrate the winner:**
   - Inspect branch diffs, snapshots, and paired `BQ.task_eval_facts` metrics grouped by `clone_group_id` (enforcing `ci_passed == true` and zero critical anti-patterns).
   - Record `winner_clone_id` and `merged_commit_sha` in Firestore (`scripts/factory_store.py put-clone-group`).
   - Merge or cherry-pick **only** the winning branch into the task branch / `main` and close the `decision` task.

---

## 3. GCP Serverless Kit Architecture & Review Rules

When sizing, briefing, and reviewing tasks for the GCP Serverless Kit, enforce these service boundaries and idioms:

### Compute: Cloud Run Services, Jobs & Worker Pools (No Polling in Services)
- **Cloud Run Services:** stateless, request-driven HTTP/gRPC handlers only.
  - **NEVER run background polling loops** (`for { time.Sleep(...) }`, ticker loops, or `while True` pollers) inside a Cloud Run service. Cloud Run throttles CPU outside active request processing, and CPU-always-on polling wastes billable instance-time.
  - Replace polling with **Pub/Sub push**, **Cloud Tasks**, **Cloud Workflows**, or **Cloud Scheduler**.
- **Cloud Run Jobs:** finite batch processing, database migrations, backfills, and heavy ETL triggered by Cloud Scheduler, Cloud Workflows, or Cloud Tasks.
- **Cloud Run Worker Pools:** continuous pull/stream consumers only when push delivery is not feasible.

### Data & Storage
- **Firestore (Native):** serverless document store for operational state and the factory control plane (`scripts/factory_store.py`). Define composite indexes in `firestore.indexes.json`; keep security rules strict (`allow read, write: if false` for server-only collections).
- **Spanner:** use for product workloads requiring global/regional strong relational consistency, interleaved tables, `Version` optimistic locking (CAS), and Spanner Data Boost (`EXTERNAL_QUERY`) for zero-impact analytical reads.
- **AlloyDB Express (PostgreSQL):** relational SQL and `pgvector` workloads. On Cloud Run, connect via AlloyDB Auth Proxy / Unix socket or VPC Direct Egress and cap connection pool sizes (`MaxConns`) proportionally to Cloud Run `--concurrency` and max instances.
- **Cloud Storage (GCS):** immutable artifacts, snapshots, transcripts, and static bundles with uniform bucket-level access and public access prevention.
- **BigQuery:** append-oriented analytics warehouse. Always partition large fact tables (`require_partition_filter = true`), cluster by `(project_id, task_id, ...)`, enforce query time windows and `maximum_bytes_billed` caps, and prefer **Observability Linked Datasets** (`_AllLogs`, `_AllSpans`) over duplicating logs with Cloud Logging Sinks.

### Messaging & Workflow Orchestration
- **Pub/Sub:** domain and lifecycle events (`farmtable-task-events`) with idempotent consumers, configured dead-letter topics (DLQs), and OIDC/Direct IAP authentication on push endpoints.
- **Cloud Workflows:** multi-step stateful orchestration, retries with backoff, and parallel fan-out/fan-in across Cloud Run services/jobs without building a custom stateful polling coordinator.
- **Cloud Tasks:** rate-limited, deduplicated, retriable per-item HTTP/Cloud Run dispatch.
- **Cloud Scheduler:** cron schedules targeting Cloud Run Jobs, Workflows, or authenticated HTTP endpoints (never in-container `cron`).

### Golang (`Go`) & IAM Hygiene
- Follow standard `cmd/` + `internal/` package layout.
- Propagate `context.Context` with explicit deadlines into every HTTP handler, SQL/Spanner/Firestore query, and GCP client call.
- Implement graceful `SIGTERM` shutdown draining in-flight requests and closing DB/Pub/Sub clients cleanly.
- Emit structured JSON logs (`log/slog`) with `severity` and Cloud Trace correlation; propagate OpenTelemetry trace headers.
- Use parameterized SQL queries only (never string-interpolated SQL).
- Require `go vet ./...` and `go test -race ./...` to pass before granting the commit slot.
- Run every Cloud Run service/job under a dedicated runtime service account with least-privilege IAM roles (never the default Compute Engine SA), and use OIDC / IAM `signJwt` for Cloud Run Direct IAP service-to-service calls.
