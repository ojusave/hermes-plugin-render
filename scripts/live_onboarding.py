"""Follow the GitHub install and ordinary Hermes chat path in an isolated profile.

This host-side script supplies an existing model connection and Render key,
just as the README prerequisites require. It drives the published Hermes
commands, without loading AIAgent directly or adding artifact-export callbacks.
It is an automated onboarding check, not evidence from an independent person.
"""

from __future__ import annotations

# Standard-library tools drive the real host CLI, capture its output, and record timings.
import argparse
import hashlib
import json
import os
import pty
import re
import select
import subprocess
import time
from datetime import UTC, datetime

# Reuse task-owned credential reading and independent Render cleanup observations.
from live_reliability import active_ids, authentication, client, save, state_record
from test_support import ROOT


def install(hermes, environment, log_path):
    """Answer the two ordinary installer prompts in a real terminal on the host.

    The key is already in the isolated profile's protected .env file, so no
    credential travels through terminal input or the captured installation log.
    """
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(hermes), "plugins", "install", "ojusave/hermes-plugin-render"],
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output, answered = "", set()
    deadline = time.monotonic() + 180
    prompts = {
        "Enable 'render' now?": "enable",
        "Prepare these with Hermes through PM now?": "dependencies",
    }
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                raise TimeoutError("Installation exceeded three minutes")
            if select.select([master], [], [], 0.5)[0]:
                try:
                    data = os.read(master, 8192)
                except OSError:
                    break
                if not data:
                    break
                output += data.decode(errors="replace")
                for prompt, label in prompts.items():
                    if prompt in output and label not in answered:
                        os.write(master, b"y\n")
                        answered.add(label)
            log_path.write_text(output)
        code = process.wait(timeout=10)
        assert code == 0 and answered == {"enable", "dependencies"}
        return {
            "exit_code": code,
            "accepted_prompts": sorted(answered),
            "enabled_message_seen": "Plugin render enabled" in output,
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        os.close(master)
        log_path.write_text(output)


def chat(hermes, environment, run_dir, name, prompt, session=None):
    """Run the real CLI and retain its actual tool outputs for independent checking."""
    query = run_dir / (name + ".prompt.txt")
    query.write_text(prompt)
    command = [
        str(hermes),
        "chat",
        "--oneshot",
        "--format",
        "stream-json",
        "--query-file",
        str(query),
        "--provider",
        "openai-codex",
        "-m",
        "gpt-6.1-sol",
        "-t",
        "terminal,file",
        "--reasoning",
        "low",
        "--max-turns",
        "8",
        "--run-budget",
        "150",
    ]
    if session:
        command += ["--resume", session]
    started = time.monotonic()
    # Write as the CLI runs, so even a timeout leaves its partial tool transcript.
    output_path = run_dir / (name + ".jsonl")
    with output_path.open("w") as stdout, (run_dir / (name + ".stderr.log")).open("w") as stderr:
        result = subprocess.run(
            command,
            env=environment,
            cwd=run_dir,
            text=True,
            stdout=stdout,
            stderr=stderr,
            timeout=210,
        )
    events = []
    for line in output_path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    assert result.returncode == 0 and final.get("exit_code") == 0, "Chat did not complete"
    tools = [e for e in events if e.get("type") == "tool_result"]
    assert tools and any(t["name"] == "terminal" for t in tools), "No real terminal call"
    return {
        "process_exit_code": result.returncode,
        "session_id": final["session_id"],
        "tool_results": tools,
        "final_text": final["text"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main(args):
    """Install, merge the exact sample settings, run two turns, and clean up owned resources."""
    from hermes_cli.auth import AUTH_STORE_VERSION
    from hermes_cli.auth_codex import _import_codex_cli_tokens
    from ruamel.yaml import YAML

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    home = ROOT / ".test-home" / ("onboarding-" + stamp)
    home.mkdir(mode=0o700)
    run_dir = ROOT / "validation/private/onboarding" / stamp
    run_dir.mkdir(parents=True)
    environment = dict(
        os.environ,
        HERMES_HOME=str(home),
        HERMES_RUNTIME_DIR=str(ROOT / ".runtime" / ("onboarding-" + stamp)),
    )
    # Strip inherited Render credentials so the installed plugin must use the
    # active profile's .env, the same location documented for normal users.
    environment.pop("RENDER_API_KEY", None)
    token, workspace = authentication()
    tokens = _import_codex_cli_tokens()
    assert tokens and tokens.get("access_token"), "An existing Codex login is required"
    hermes = ROOT / ".test-home/installer/base-venv/bin/hermes"
    assert hermes.is_file(), "Prepare the isolated base Hermes environment first"
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "method": "Normal GitHub installation and two ordinary CLI chat turns; automated operator; no custom agent callbacks.",
    }
    api = client()
    try:
        # Create private credential files without a world-readable intermediate.
        # Only a still-valid access token is copied. A dummy refresh value keeps
        # this test from rotating the user's original single-use refresh token.
        for path, value in (
            (home / ".env", "RENDER_API_KEY=" + token + "\n"),
            (
                home / "auth.json",
                json.dumps(
                    {
                        "version": AUTH_STORE_VERSION,
                        "active_provider": "openai-codex",
                        "providers": {
                            "openai-codex": {
                                "tokens": {
                                    "access_token": tokens["access_token"],
                                    "refresh_token": "unused-test-refresh-token",
                                }
                            }
                        },
                    }
                ),
            ),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(value)
        probe = subprocess.run(
            [
                str(hermes.parent / "python"),
                "-c",
                'import importlib.util; print(importlib.util.find_spec("render") is None)',
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        report["base_environment_initially_without_render"] = probe.stdout.strip() == "True"
        assert report["base_environment_initially_without_render"]
        print("INSTALL fresh profile from GitHub", flush=True)
        report["installation"] = install(hermes, environment, run_dir / "installation.log")
        metadata = json.loads((home / "plugins/.install-metadata.json").read_text())["render"]
        report["installed_revision"] = metadata["revision"]
        report["runtime_sha256"] = {
            str(p.relative_to(home / "plugins/render")): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (home / "plugins/render/hermes_render").glob("*.py")
        }
        # Merge, rather than replace, the configuration written by Hermes's installer.
        yaml = YAML(typ="safe")
        cfg = yaml.load((home / "config.yaml").read_text())
        example = yaml.load((ROOT / "config.example.yaml").read_text())
        example["plugins"]["entries"]["render"]["settings"]["workspace_id"] = workspace
        cfg["plugins"].setdefault("entries", {})["render"] = example["plugins"]["entries"]["render"]
        cfg["terminal"] = example["terminal"]
        with (home / "config.yaml").open("w") as stream:
            yaml.dump(cfg, stream)
        listed = subprocess.run(
            [str(hermes), "plugins", "list", "--user", "--json"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=45,
        )
        (run_dir / "plugin-list.json").write_text(listed.stdout)
        assert listed.returncode == 0 and "render" in listed.stdout
        report["listed_after_restart"] = True
        print("CHAT ordinary CLI: calculate, write, and read a report", flush=True)
        first = chat(
            hermes,
            environment,
            run_dir,
            "first-turn",
            "This is a synthetic integration check. Use terminal to run Python in /workspace. "
            "Calculate sum([12,24,30]), write report.json with exactly "
            '{"total":66,"system":platform.system(),"render_key_present":("RENDER_API_KEY" in os.environ)} '
            "using the computed sum for total. Print the JSON from the file. Then use read_file "
            "to inspect /workspace/report.json and report the actual result. Also use terminal to "
            "count all files below Path.home() / '.hermes/skills' with pathlib, and print "
            "SKILL_FILE_COUNT=<count>. Do not use the internet.",
        )
        terminal_outputs = "\n".join(
            t["output"] for t in first["tool_results"] if t["name"] == "terminal"
        )
        # Inspect the real tool results, not just the model's final sentence.
        normalized = terminal_outputs.replace('\\"', '"').replace(" ", "").lower()
        assert '"total":66' in normalized and '"system":"linux"' in normalized
        assert '"render_key_present":false' in normalized
        assert any(t["name"] == "read_file" and "66" in t["output"] for t in first["tool_results"])
        first["independent_checks"] = [
            "computed total 66",
            "Linux tool output",
            "Render key absent",
            "file read through ordinary Hermes tool",
        ]
        # The first chat initializes Hermes's bundled skills. Count after that
        # initialization, and compare the host library with the real remote output.
        report["installed_skill_file_count"] = sum(
            path.is_file() for path in (home / "skills").rglob("*")
        )
        assert report["installed_skill_file_count"] >= 300
        assert f"SKILL_FILE_COUNT={report['installed_skill_file_count']}" in terminal_outputs
        first["independent_checks"].append("remote skill count equals full installed host library")
        report["first_turn"] = first
        save(run_dir / "report.json", report)
        print("CHAT resume: verify previous temporary file is gone", flush=True)
        second = chat(
            hermes,
            environment,
            run_dir,
            "second-turn",
            "Use terminal to check whether /workspace/report.json from the previous turn exists. "
            "Do not recreate it. Run: if test -f /workspace/report.json; then echo FILE_PRESENT; "
            "else echo FILE_ABSENT; fi. Explain the actual result briefly.",
            first["session_id"],
        )
        outputs = "\n".join(t["output"] for t in second["tool_results"] if t["name"] == "terminal")
        assert "FILE_ABSENT" in outputs and "FILE_PRESENT" not in outputs
        report["second_turn"] = second
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        # The plugin itself records each created ID in Hermes's ordinary log.
        # Never infer ownership from a shared workspace's before/after difference.
        log = home / "logs/agent.log"
        log_text = log.read_text() if log.exists() else ""
        ids = sorted(set(re.findall(r"Render sandbox created: (sbx-[a-zA-Z0-9]+)", log_text)))
        cleanup = []
        for sid in ids:
            state = api.from_id(sid)
            normal_cleanup = bool(state.terminated_at) or state.status == "terminated"
            api.terminate(sid)
            state = api.from_id(sid)
            cleanup.append(
                {
                    "sandbox_id": sid,
                    "ordinary_hermes_cleanup": normal_cleanup,
                    **state_record(state),
                }
            )
        report["cleanup"] = cleanup
        report["sandbox_count"] = len(ids)
        report["expected_sandbox_count"] = 3
        report["extra_sandbox_reason"] = "Hermes prompt-backend-probe before the first task"
        report["all_cleanup_verified"] = (
            bool(ids)
            and not (set(ids) & active_ids(api))
            and all(x["terminated_at"] or x["status"] == "terminated" for x in cleanup)
        )
        for path in (home / ".env", home / "auth.json"):
            path.unlink(missing_ok=True)
        report["temporary_credentials_removed"] = (
            not (home / ".env").exists() and not (home / "auth.json").exists()
        )
        # Cleanup is part of passing, not a later assertion after a success receipt.
        if report["status"] == "passed" and not (
            # Pinned Hermes first probes the remote operating system in its own
            # short-lived environment, then creates one environment for each turn.
            len(ids) == 3
            and all(x["ordinary_hermes_cleanup"] for x in cleanup)
            and report["all_cleanup_verified"]
            and report["temporary_credentials_removed"]
        ):
            report["status"] = "failed"
            report["error_type"] = "CleanupAssertionError"
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(run_dir / "report.json", report)

        def sanitize(value):
            if isinstance(value, dict):
                return {
                    k: sanitize(v)
                    for k, v in value.items()
                    if k not in {"sandbox_id", "session_id"}
                }
            return [sanitize(x) for x in value] if isinstance(value, list) else value

        public = sanitize(report)
        serialized = json.dumps(public, default=str)
        assert token not in serialized and tokens["access_token"] not in serialized
        save(ROOT / "validation/public/live-onboarding.json", public)
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "status",
                        "sandbox_count",
                        "all_cleanup_verified",
                        "temporary_credentials_removed",
                    )
                },
                indent=2,
            ),
            flush=True,
        )
        assert report["status"] == "passed", "See the saved failure receipt"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-cli-auth", action="store_true", required=True)
    parser.add_argument("--codex-login", action="store_true", required=True)
    main(parser.parse_args())
