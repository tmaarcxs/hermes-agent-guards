"""Acceptance 3: register() registers exactly the hooks declared in plugin.yaml.

Standalone script; load the plugin the way the Hermes plugin host would, count
the registrations, compare against the manifest.

Run from the repo root:
    PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python \
        workspace-guards/tests/test_register_matches_plugin_yaml.py
"""

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def test_register_matches_plugin_yaml():
    spec = importlib.util.spec_from_file_location(
        "workspace_guards", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["workspace_guards"] = module
    spec.loader.exec_module(module)

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    declared = sorted(manifest["hooks"])

    class Probe:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, fn):
            self.hooks.setdefault(name, []).append(fn)

    ctx = Probe()
    module.register(ctx)
    registered = sorted(ctx.hooks)

    assert registered == declared, f"plugin.yaml={declared} register()={registered}"
    assert len(ctx.hooks["pre_tool_call"]) == 1, "expected exactly one pre_tool_call wiring"


if __name__ == "__main__":
    test_register_matches_plugin_yaml()
    print("OK: register() registers exactly the hooks declared in workspace-guards/plugin.yaml")
