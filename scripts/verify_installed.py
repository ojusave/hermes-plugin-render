"""Verify a GitHub-installed plugin without copying local plugin code into its profile.

Install with the real `hermes plugins install` command first. This acceptance
helper only configures the isolated profile and runs the existing CSV scenario.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--render-cli-auth", action="store_true", required=True)
    parser.add_argument("--codex-login", action="store_true", required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    if not home.is_relative_to(ROOT / ".test-home"):
        raise RuntimeError("Use an isolated profile under this checkout's .test-home directory.")
    os.environ["HERMES_HOME"] = str(home)
    if not os.environ.get("HERMES_RUNTIME_DIR"):
        raise RuntimeError("Set HERMES_RUNTIME_DIR to the installation test's managed runtime.")

    # Activate the environment that Hermes's installer selected before imports.
    import hermes_bootstrap  # noqa: F401
    from hermes_cli.auth_codex import _import_codex_cli_tokens
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    from hermes_cli.config import save_env_value
    from live_scenarios import run_case, write_json
    from ruamel.yaml import YAML
    from scenario_fixtures import SCENARIOS
    from test_support import load_provider

    installed = home / "plugins" / "render"
    metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text())["render"]
    assert metadata["revision"] == args.expected_revision, "Unexpected installed revision"
    assert metadata["source"] == "https://github.com/ojusave/hermes-plugin-render.git"
    yaml = YAML(typ="safe")
    config = yaml.load((home / "config.yaml").read_text())
    assert "render" in config["plugins"]["enabled"], "Installer did not enable the plugin"
    auth = yaml.load((Path.home() / ".render" / "cli.yaml").read_text())
    workspace = auth["workspace"]
    config["plugins"].setdefault("entries", {}).setdefault("render", {})["settings"] = {
        "workspace_id": workspace,
        "lifetime_seconds": 900,
        "plan": "starter",
        "network_policy": "deny-all",
    }
    config["terminal"] = {
        "backend": "render",
        "cwd": "/workspace",
        "container_persistent": False,
        "timeout": 30,
        "lifetime_seconds": 900,
    }
    with (home / "config.yaml").open("w") as stream:
        yaml.dump(config, stream)
    tokens = _import_codex_cli_tokens()
    if not tokens or not tokens.get("access_token"):
        raise RuntimeError("An existing Codex login is required; no login is refreshed or changed.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "validation" / "private" / "installer" / stamp
    run_dir.mkdir(parents=True)
    env_path = home / ".env"
    before_env = env_path.read_bytes() if env_path.exists() else None
    report = {
        "installed_revision": metadata["revision"],
        "source": metadata["source"],
        "model": "gpt-6.1-sol",
        "provider": "openai-codex",
        "render_version": importlib.metadata.version("render"),
        "workspace_id": workspace,
        "runtime_sha256": {
            str(p.relative_to(installed)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                installed / "__init__.py",
                installed / "plugin.yaml",
                *sorted((installed / "hermes_render").glob("*.py")),
            ]
        },
    }
    try:
        # Test-only reuse of the authorized CLI key. Remove it again in finally.
        save_env_value("RENDER_API_KEY", auth["api"]["key"])
        env_path.chmod(0o600)
        provider = load_provider()
        loaded = Path(sys.modules[type(provider).__module__].__file__).resolve()
        assert loaded.is_relative_to(installed), "Provider did not load from the GitHub install"
        import render

        dependency = Path(render.__file__).resolve()
        assert dependency.is_relative_to(home / "installs"), (
            "Render did not load from the PM-selected environment"
        )
        report["provider_loaded_from_github_install"] = True
        report["render_loaded_from_pm_environment"] = True

        def save(case):
            report["case"] = case
            write_json(run_dir / "report.json", report)

        case = run_case(SCENARIOS[0], provider, tokens, DEFAULT_CODEX_BASE_URL, run_dir, save)
        report["all_passed"] = case["status"] == "passed"
        report["all_terminated"] = all(
            s.get("final_status") == "terminated" for s in case["sandboxes"]
        )
        report["sandbox_count"] = len(case["sandboxes"])
    finally:
        if before_env is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_bytes(before_env)
            env_path.chmod(0o600)
        report["temporary_render_credential_removed"] = True
        write_json(run_dir / "report.json", report)
        write_json(ROOT / "validation" / "live-installation.json", report)
    public = json.loads(json.dumps(report))
    del public["workspace_id"]
    for sandbox in public["case"]["sandboxes"]:
        del sandbox["id"]
    for failure in public["case"]["cleanup_failures"]:
        failure.pop("id", None)
    public["publication_note"] = (
        "Synthetic task; account and resource identifiers omitted. The actual CLI installation and consent checks are described in VALIDATION.md."
    )
    write_json(ROOT / "validation" / "public" / "live-installation.json", public)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "installed_revision",
                    "provider_loaded_from_github_install",
                    "render_loaded_from_pm_environment",
                    "all_passed",
                    "all_terminated",
                    "sandbox_count",
                    "temporary_render_credential_removed",
                )
            },
            indent=2,
        )
    )
    if not report["all_passed"] or not report["all_terminated"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
