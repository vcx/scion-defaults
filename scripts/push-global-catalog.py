#!/usr/bin/env python3
"""Publish the clean global specialist template catalog to a Scion Hub.

Generates project-agnostic Antigravity + Farm Table specialist templates from
`role_library.ROLES` (excluding legacy psearch-specific roles `eval-lead`,
`eval-deployer`, and `search-evaluator`) and uploads them to Hub `global` scope
via `push-templates.py --global`.

Usage:
    scripts/push-global-catalog.py           # dry run (default)
    scripts/push-global-catalog.py --apply   # create/update global templates on Hub
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import role_library  # noqa: E402
from hub_client import HubClient  # noqa: E402
import importlib.util  # noqa: E402

_pt_spec = importlib.util.spec_from_file_location("push_templates", os.path.join(HERE, "push-templates.py"))
_push_templates = importlib.util.module_from_spec(_pt_spec)
_pt_spec.loader.exec_module(_push_templates)

EXCLUDED_GLOBAL_ROLES = {"lead", "deployer", "eval-lead", "eval-deployer", "search-evaluator"}
DEFAULT_MODEL = "Gemini 3.8 Flash (Medium)"


def render_global_catalog(out_dir: str, roles: list[str] | None = None) -> list[str]:
    selected = roles or [r for r in role_library.ROLES if r not in EXCLUDED_GLOBAL_ROLES]
    written: list[str] = []
    for role in selected:
        rdef = role_library.ROLES.get(role)
        if not rdef:
            raise ValueError(f"unknown role {role!r}")
        rdir = os.path.join(out_dir, role)
        os.makedirs(rdir, exist_ok=True)
        desc = rdef["responsibility"].replace('"', "'")
        with open(os.path.join(rdir, "scion-agent.yaml"), "w") as f:
            f.write(
                f'schema_version: "1"\n'
                f'description: "{desc}"\n'
                f"default_harness_config: antigravity\n"
                f'model: "{DEFAULT_MODEL}"\n'
                f"message_mode: project\n"
                f"agent_instructions: agents.md\n"
                f"system_prompt: system-prompt.md\n"
                f"mcp_servers:\n"
                f"  farmtable:\n"
                f"    transport: stdio\n"
                f"    command: ft\n"
                f"    args:\n"
                f"      - mcp\n"
                f"      - serve\n"
                f"    scope: global\n"
                f"skills:\n"
                f'  - uri: "skill://scion/global/farmtable@1.0.0"\n'
                f"    optional: false\n"
                f"env:\n"
                f"  FACTORY_ROLE: {role}\n"
                f'  AGY_MODEL: "{DEFAULT_MODEL}"\n'
                f"  GOOGLE_CLOUD_LOCATION: global\n"
            )
        with open(os.path.join(rdir, "agents.md"), "w") as f:
            f.write(
                f"# Specialist Agent Instructions (`{role}`)\n\n"
                "- **Farm Table Discipline (MCP-first):** Prefer the `farmtable` MCP server (consult the `farmtable` skill) over shelling out to `ft`.\n"
                "- **Atomic Claim:** Always claim tasks with `task_claim(id)` — NEVER call `task_update(stage=\"working\")`.\n"
                "- **Group Chat (`conv:<id>`):** Report progress, questions, and completion in the project's shared group conversation (`conv:<id>`) mentioning `@lead`. Verify `Message accepted`.\n"
                "- **Deployment Boundary:** Never deploy directly to Cloud Run / Spanner / AlloyDB / GCP; leave live deployments to `deployer`.\n"
                '- **Lifecycle:** End every completed turn with `sciontool status idle "ready for next task"` (NEVER `task_completed`).\n'
            )
        with open(os.path.join(rdir, "system-prompt.md"), "w") as f:
            protocol = role_library.role_protocol(role, "service") or rdef["responsibility"]
            f.write(
                f"# Specialist Role (`{role}`)\n\n"
                f"{rdef['responsibility']}\n\n"
                "## Role Protocol\n"
                f"{protocol.strip()}\n"
            )
        written.append(role)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--roles", default="", help="Optional comma-separated subset of roles")
    parser.add_argument("--apply", action="store_true", help="Publish to Hub (default: dry run)")
    args = parser.parse_args()

    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    with tempfile.TemporaryDirectory() as tmp:
        written = render_global_catalog(tmp, roles=roles)
        client = HubClient.from_env()
        _push_templates.push_templates_via_api(client, "", written, tmp, apply=args.apply, global_scope=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
