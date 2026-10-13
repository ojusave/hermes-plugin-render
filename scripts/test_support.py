"""Local test setup. Reading the Render CLI login requires an explicit test flag.

This helper is not imported by the plugin. End users provide their own API key.
"""

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare_home(name="live", use_cli=False):
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    home = ROOT / ".test-home" / name
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_RUNTIME_DIR"] = str(ROOT / ".runtime")
    if use_cli:
        # Test-only, read-only reuse of the login authorized by the user.
        config = yaml.load((Path.home() / ".render" / "cli.yaml").read_text())
        os.environ["RENDER_API_KEY"] = config["api"]["key"]
        workspace = config["workspace"]
    else:
        workspace = os.environ.get("RENDER_WORKSPACE_ID", "")
    if not os.environ.get("RENDER_API_KEY") or not workspace:
        raise RuntimeError(
            "Set Render credentials or explicitly use --render-cli-auth for local tests."
        )
    install = home / "plugins" / "render"
    install.mkdir(parents=True, exist_ok=True)
    for file in ("plugin.yaml", "__init__.py", "pyproject.toml"):
        shutil.copy2(ROOT / file, install / file)
    shutil.copytree(
        ROOT / "hermes_render",
        install / "hermes_render",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    settings = {
        "plugins": {
            "enabled": ["render"],
            "entries": {
                "render": {
                    "enabled": True,
                    "settings": {
                        "workspace_id": workspace,
                        "lifetime_seconds": 900,
                        "network_policy": "deny-all",
                    },
                }
            },
        },
        "terminal": {
            "backend": "render",
            "cwd": "/workspace",
            "container_persistent": False,
            "timeout": 30,
            "lifetime_seconds": 900,
        },
    }
    with (home / "config.yaml").open("w") as output:
        yaml.dump(settings, output)
    return home, workspace


def load_provider():
    from agent.terminal_env_registry import get_provider
    from hermes_cli.plugins import discover_plugins

    discover_plugins()
    provider = get_provider("render")
    if provider is None:
        raise RuntimeError("Installed Render plugin was not discovered by Hermes.")
    return provider
