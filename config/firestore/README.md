# Factory control plane on Firestore

The factory stores agent status and evaluation runs in Firestore (Native mode). Firestore is serverless: there is no
instance to size, and idle cost is zero. The factory never needs Cloud Spanner.

| Collection | Document ID | Writer | Reader |
|---|---|---|---|
| `factory_agent_status` | `<project_slug>__<agent_name>` | [`scripts/sync-factory-agents.py`](../../scripts/sync-factory-agents.py) on the Hub VM, every minute | Eval agents, dashboard |
| `eval_runs` | `<run_id>` | Eval agents, with [`scripts/factory_store.py`](../../scripts/factory_store.py) `put-eval-run` | Eval agents, dashboard |
| `eval_analyses` | `<analysis_id>` | Eval-Agent / leader, with `factory_store.py put-analysis` | Eval-Agent, App Shell (`?view=factory&analysis=<id>`) |
| `eval_clone_groups` | `<clone_group_id>` | `scion-eval clone-task` / Eval-Agent / leader, with `factory_store.py put-clone-group` | Leader, Eval-Agent, App Shell |
| `eval_agent_state` | `<project_slug>` | Eval-Agent / operator, with `factory_store.py put-agent-state` | Eval-Agent, dashboard |
| `eval_factory_overrides` | `<override_id>` | Eval-Agent, with `factory_store.py put-override` & `resolve-override` | Eval-Agent, rollback watchdog, dashboard |
| `eval_snapshots` | `<project_slug>__<snapshot_id>` | `raw_reconciler.py` / `scion-eval publish`, with `factory_store.py put-snapshot` | Cloud Run Direct IAP UI (`/api/v2/bootstrap`, `/api/v2/snapshots/*`, SSE) |
| `factory_fleet` | `<slug>` | `scripts/fleet_registry.py` | Control plane |
| `factory_fleet_snapshots` | `<slug>` | `scripts/fleet_registry.py` | Control plane |

## `EVAL FÁBRICA` Collections (`eval_*`)

- **Canonical GCS Artifact URLs:** `eval_analyses.artifacts` (and top-level `eval_analyses` / `eval_clone_groups` / `eval_clone_groups.variants[]`) carry canonical GCS or App Shell links in `report_url`, `snapshot_url`, and `diff_url` (`gs://...`, `https://...`, or `?view=factory&analysis=<id>`).
- **`eval_analyses` (`<analysis_id>`):** `analysis_id`, `project_slug`, `collection_id`, `status` (`drafting` | `testing` | `awaiting_approval` | `applying` | `monitoring` | `completed` | `rolled_back` | `rejected`), `created_by` (`eval-agent` | `leader` | `human`), `task_ids`, `bq_evidence`, `hypothesis`, `experiment` (`type`: `task_clone` | `canary_override` | `replay`), `results`, `artifacts` (`{report_url, snapshot_url, diff_url}`), `governance`, `action_log`, `eval_run_ids`, `created_at`, `updated_at`.
- **`eval_clone_groups` (`<clone_group_id>`):** `clone_group_id`, `project_slug`, `source_task_id`, `source_tracker_mode` (`github` | `farmtable`), `base_commit_sha`, `triggered_by` (`leader` | `eval_agent` | `human`), `analysis_id`, `isolation_mode` (`worker_branch` | `ephemeral_project`), `decision_task_id`, `variants` (`[{clone_id, ft_task_id, branch, hub_project_id, agent_id, model, thinking_level, template, harness_config, profile, status, cost_usd, duration_s, passed, snapshot_url, transcript_uri}]`), `winner_clone_id`, `selection_mode` (`manual` | `eval_agent`), `merged_commit_sha`, `diff_url`, `created_at`, `updated_at`, `closed_at`.
- **`eval_agent_state` (`<project_slug>`):** `project_slug`, `governance_mode` (`autonomous` | `human_approval` | `manual`), `enabled`, `policy` (`{daily_experiment_budget_usd, monthly_experiment_budget_usd, max_concurrent_analyses, min_sample_size, max_clone_variants, rollback_window_tasks, rollback_regression_threshold_pct}`), `cursors`, `spend`, `cooldown_until`, `updated_at`.
- **`eval_factory_overrides` (`<override_id>`):** `override_id`, `analysis_id`, `project_slug`, `hub_project_id`, `target_scope` (`project_settings` | `template` | `agent_patch` | `schedule`), `api_endpoint`, `target_id`, `previous_state`, `applied_state`, `status` (`active` | `promoted_to_git` | `rolled_back` | `expired`), `rollback_after_tasks`, `tasks_observed_since_apply`, `applied_at`, `resolved_at`, `resolution_note`.
- **`eval_snapshots` (`<project_slug>__<snapshot_id>`):** `snapshot_id`, `project_slug`, `git_sha`, `git_branch`, `trigger`, `reconciliation_id`, `finops` (`{total_cost_usd, productive_cost_usd, rework_cost_usd, heartbeat_tax_usd, conservation_drift_usd}`), `conservation_check`, `trajectories_summary`, `agents_summary`, `system_summary`, `transcript_refs` (`[{task_id, agent_id, incarnation_id, proxy_url: "/api/v2/transcripts/..."}]`), `published_at`, `updated_at`.

- [`firestore.indexes.json`](firestore.indexes.json): composite indexes. [`setup-sandbox.sh`](../../scripts/setup-sandbox.sh)
  creates them.
- [`firestore.rules`](firestore.rules): denies every client SDK request. All access is server side, through IAM:
  `roles/datastore.user` for writers (the Hub VM service account) and `roles/datastore.viewer` for the dashboard.
- Field-level contract: [`docs/plans/2026-10-20-firestore-control-plane.md`](../../docs/plans/2026-10-20-firestore-control-plane.md) and [`docs/specs/eval-fabrica-spec.md`](../../docs/specs/eval-fabrica-spec.md).
  Every compiled kit also contains a project-specific `config/firestore/README.md`.

Inspect the data from any machine with Google credentials:

```bash
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-agents --project-slug "$BUILD_PROJECT"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-eval-runs --limit 5
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-analyses --project-slug "$BUILD_PROJECT" --limit 10
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" get-analysis --analysis-id "$ANALYSIS_ID"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" get-clone-group --clone-group-id "$CLONE_GROUP_ID"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" get-agent-state --project-slug "$BUILD_PROJECT"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-active-overrides --project-slug "$BUILD_PROJECT"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-snapshots --project-slug "$BUILD_PROJECT" --limit 10
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" get-snapshot --project-slug "$BUILD_PROJECT" --snapshot-id "$SNAPSHOT_ID"
```
