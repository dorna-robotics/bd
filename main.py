"""BT project entry point. All config lives in ``launch.yaml``.

Canonical entry point for every BT project on the platform — kept
identical across projects unless the project has a specific reason
to diverge. Project-specific behaviour lives in:

  * ``launch.yaml``   — project name, port, scene, recipes, kwargs
  * ``recipes.yaml``  — recipe wiring
  * ``actions.py``    — BT actions
  * ``checks.py``     — vision / sensor checks
  * ``scene/*.j2``    — components + populated items

This file just loads ``launch.yaml`` and runs the BT runner. If you
need to copy this for a new project, copy verbatim — no edits.

Framework reference: docs/bt-framework-guide.md §2
"""

import argparse
import importlib
import os
import pkgutil
from pathlib import Path

import yaml

from workspace.workspace import Workspace
from workspace.runtime_server import RuntimeServer
from workspace.bt.launcher import load_checks, load_recipes, run_protocol


LAUNCH_FILE   = "launch.yaml"
PORT_ENV_VAR  = "PORT"

_BASE_DIR = Path(__file__).parent
with open(_BASE_DIR / LAUNCH_FILE) as f:
    LAUNCH = yaml.safe_load(f)


def _register_project_components():
    """Import every module in this project's ``components/`` package so
    their ``@register("...")`` decorators run before the scene boots.

    Mirrors how the library's ``workspace/components`` package auto-
    imports itself. Without this, a scene that references a project-
    local ``type:`` fails with "Unknown component type". Keeps main.py
    project-agnostic — drop a registered module in ``components/`` and
    it just works, no edits here."""
    comp_dir = _BASE_DIR / "components"
    if not comp_dir.is_dir():
        return
    for mod in pkgutil.iter_modules([str(comp_dir)]):
        if not mod.name.startswith("_"):
            importlib.import_module(f"components.{mod.name}")


_register_project_components()


def _import_module(rel_path: str):
    """``'actions.py'`` → ``'actions'``; ``'protocol/actions.py'`` → ``'protocol.actions'``."""
    name = rel_path.removesuffix(".py").replace("/", ".")
    return importlib.import_module(name)


actions = _import_module(LAUNCH.get("actions", "actions.py"))
checks  = _import_module(LAUNCH.get("checks",  "checks.py"))


def workflow_fn(*, workspace, core, **kwargs):
    recipes   = load_recipes(workspace, core, _BASE_DIR / LAUNCH["recipes"])
    check_fns = load_checks(workspace, core, recipes, checks_module=checks, **kwargs)
    return run_protocol(
        workspace, core, actions,
        recipes=recipes,
        checks=check_fns,
        project_name=LAUNCH["project_name"],
        plan_window=int(LAUNCH.get("plan_window", 4)),
        # Unset → the launcher auto-infers it from the single objects
        # key. Only multi-dimension protocols need to name it here.
        slice_dim=LAUNCH.get("slice_dim"),
        scheduler=str(LAUNCH.get("scheduler", "cpsat")),
        **kwargs,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--port", type=int,
        default=int(os.getenv(PORT_ENV_VAR, str(LAUNCH["port"]))),
    )
    args = p.parse_args()

    # Resolve scene paths relative to the project dir (like recipes /
    # launch.yaml) so the launch is cwd-independent — the orchestrator
    # runs main.py by absolute path from an arbitrary working directory.
    scene = LAUNCH["scene"]
    if isinstance(scene, str):
        scene = [scene]
    scene = [str(_BASE_DIR / p) for p in scene]

    ws = Workspace(config_path=scene, port=args.port)
    RuntimeServer(runtime=ws.rt, workflow_fn=workflow_fn, workspace=ws).run()


if __name__ == "__main__":
    main()
