import importlib.util
import os
import sys
import tempfile
from unittest import mock

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import role_library  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_render_global_catalog_excludes_legacy_and_has_no_placeholders():
    pgc = _load("push_global_catalog", "push-global-catalog.py")
    with tempfile.TemporaryDirectory() as tmp:
        written = pgc.render_global_catalog(tmp)
        assert "backend" in written
        assert "frontend" in written
        assert "reviewer" in written
        assert "search-evaluator" not in written
        assert "eval-lead" not in written
        assert "eval-deployer" not in written
        for role in written:
            for fname in ("scion-agent.yaml", "agents.md", "system-prompt.md"):
                content = open(os.path.join(tmp, role, fname)).read()
                assert "${" not in content, f"unrendered placeholder in {role}/{fname}"


def test_push_templates_global_scope_uses_global_endpoint():
    pt = _load("push_templates", "push-templates.py")
    pgc = _load("push_global_catalog", "push-global-catalog.py")
    calls = []

    class FakeClient:
        def request(self, method, path, body=None, raw=None, headers=None):
            calls.append((method, path, body))
            if method == "GET":
                return 200, {"templates": []}
            if method == "POST" and path == "/api/v1/templates":
                return 201, {"template": {"id": "tpl-1"}}
            if method == "POST" and path.endswith("/upload"):
                return 200, {"uploadUrls": []}
            if method == "POST" and path.endswith("/finalize"):
                return 200, {"status": "active", "contentHash": "sha256:abc"}
            return 200, {}

    with tempfile.TemporaryDirectory() as tmp:
        pgc.render_global_catalog(tmp, roles=["backend"])
        pt.push_templates_via_api(FakeClient(), "", ["backend"], tmp, apply=True, global_scope=True)

    create_calls = [c for c in calls if c[0] == "POST" and c[1] == "/api/v1/templates"]
    assert len(create_calls) == 1
    assert create_calls[0][2]["scope"] == "global"
    assert "scopeId" not in create_calls[0][2]
    assert "projectId" not in create_calls[0][2]


def test_lead_protocol_includes_catalog_and_clone():
    for key in ("protocol", "protocol_none"):
        proto = role_library.ROLES["lead"][key]
        assert "scion templates list" in proto
        assert "scion templates clone" in proto
