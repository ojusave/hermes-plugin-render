"""Run multiple real Hermes tasks with protected, independent result checks."""

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from scenario_fixtures import SCENARIOS
from test_support import ROOT, load_provider, prepare_home


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def put(env, path, relative):
    target = "/workspace/" + relative
    env._control("mkdir -p " + shlex.quote(str(Path(target).parent)))
    env._api.copy_to(env.sandbox_id, str(path), target)


def run_case(case, provider, tokens, base_url, run_dir, save):
    from run_agent import AIAgent
    from tools.terminal_tool_lifecycle import cleanup_vm, ensure_task_env

    name = case["name"]
    task_id = "render-scenarios-" + run_dir.name + "-" + name
    local = run_dir / name
    inputs = local / "inputs"
    outputs = local / "outputs"
    outputs.mkdir(parents=True)
    for relative, content in case["inputs"].items():
        path = inputs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = {
        "name": name,
        "status": "running",
        "prompt": case["prompt"],
        "tool_calls": [],
        "sandboxes": [],
        "cleanup_failures": [],
    }
    started = time.monotonic()
    environments = []
    agent = None
    print(f"START {name}", flush=True)

    def remember(env, purpose):
        environments.append(env)
        state = env._api.from_id(env.sandbox_id)
        result["sandboxes"].append(
            {
                "id": env.sandbox_id,
                "purpose": purpose,
                "plan": state.plan,
                "region": state.region,
                "network_policy": state.network_policy,
                "timeout_seconds": state.timeout_seconds,
            }
        )
        save(result)

    try:
        env = ensure_task_env(task_id)
        remember(env, "agent")
        for relative in case["inputs"]:
            put(env, inputs / relative, relative)
        probe = env.execute(
            'python3 -c \'import os,platform,json; print(json.dumps({"system":platform.system(),"python":platform.python_version(),"render_key_present":"RENDER_API_KEY" in os.environ}))\''
        )
        assert probe["returncode"] == 0, "Execution probe failed"
        result["execution_host"] = json.loads(probe["output"].strip())
        assert result["execution_host"]["system"] == "Linux"
        assert not result["execution_host"]["render_key_present"]
        if case.get("baseline"):
            baseline = env.execute(case["baseline"])
            result["baseline"] = baseline
            assert baseline["returncode"] != 0, "Bug fixture unexpectedly passed"

        export_names = sorted(set(case["inputs"]) | set(case["outputs"]))
        export_probe = (
            "import json,pathlib; names=" + repr(export_names) + "; "
            "print(json.dumps([n for n in names if (pathlib.Path('/workspace') / n).is_file()]))"
        )

        def completed(call_id, name, arguments, tool_result):
            result["tool_calls"].append(name)
            # Download only this scenario's named files, before normal turn cleanup.
            present = json.loads(env._control("python3 -c " + shlex.quote(export_probe)))
            for relative in present:
                path = outputs / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                env._api.copy_from(env.sandbox_id, "/workspace/" + relative, str(path))
            save(result)

        agent = AIAgent(
            api_key=tokens["access_token"],
            base_url=base_url,
            provider="openai-codex",
            api_mode="codex_responses",
            model="gpt-6.1-sol",
            enabled_toolsets=["terminal", "file"],
            quiet_mode=True,
            max_iterations=16,
            run_budget_seconds=180,
            max_tokens=6000,
            skip_memory=True,
            skip_context_files=True,
            skip_background_review=True,
            save_trajectories=False,
            cwd="/workspace",
            tool_complete_callback=completed,
            reasoning_config={"effort": "low"},
        )
        answer = agent.run_conversation(
            "This is a synthetic Render integration test. Work only in /workspace using "
            "terminal and file tools. No internet or package installation is needed. "
            + case["prompt"],
            task_id=task_id,
        )
        result["agent_completed"] = bool(answer.get("completed", not answer.get("error")))
        result["api_calls"] = answer.get("api_calls")
        result["final_response"] = answer.get("final_response", "")
        assert result["agent_completed"], "Agent did not complete"
        assert result["tool_calls"], "Agent made no tool calls"
        for relative in case["outputs"]:
            assert (outputs / relative).is_file(), "An expected output is missing"
        unchanged = [n for n in case["inputs"] if n not in case["outputs"]]
        assert all((inputs / n).read_bytes() == (outputs / n).read_bytes() for n in unchanged), (
            "Protected input changed"
        )
        result["protected_inputs_unchanged"] = unchanged
        if case.get("baseline"):
            assert all(
                (inputs / n).read_bytes() != (outputs / n).read_bytes() for n in case["outputs"]
            ), "Expected source repairs were not made"
        result["output_sha256"] = {
            n: hashlib.sha256((outputs / n).read_bytes()).hexdigest() for n in case["outputs"]
        }
        print(f"VERIFY {case['name']} in a fresh sandbox", flush=True)
        verifier = provider.create_environment(
            cwd="/workspace",
            timeout=60,
            task_id=task_id + "-verify",
            container_config={"container_persistent": False},
        )
        remember(verifier, "independent_verifier")
        # Original inputs come from the host, never from the agent's copies.
        for relative in case["inputs"]:
            put(verifier, inputs / relative, relative)
        for relative in case["outputs"]:
            put(verifier, outputs / relative, relative)
        checker = local / "trusted_check.py"
        checker.write_text(case["checker"])
        put(verifier, checker, "trusted_check.py")
        verification = verifier.execute("cd /workspace && python3 trusted_check.py")
        result["independent_verification"] = verification
        assert verification["returncode"] == 0, "Independent verification failed"
        result["verified_checks"] = json.loads(verification["output"].strip().splitlines()[-1])
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error_type"] = type(exc).__name__
        # Detailed SDK errors can contain request information. Do not persist them.
    finally:
        try:
            cleanup_vm(task_id)
        except Exception as exc:
            result["lifecycle_cleanup_error_type"] = type(exc).__name__
        for env in environments:
            try:
                env.cleanup()
                state = env._api.from_id(env.sandbox_id)
                assert state.status == "terminated" or state.terminated_at is not None
                record = next(s for s in result["sandboxes"] if s["id"] == env.sandbox_id)
                record["final_status"] = state.status
                record["terminated_at"] = str(state.terminated_at)
            except Exception as exc:
                result["cleanup_failures"].append(
                    {"id": env.sandbox_id, "error_type": type(exc).__name__}
                )
        if agent is not None:
            try:
                agent.close()
            except Exception as exc:
                result["agent_close_error_type"] = type(exc).__name__
        if result["cleanup_failures"]:
            result["status"] = "failed"
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        save(result)
    print(f"{result['status'].upper()} {case['name']} ({result['elapsed_seconds']}s)", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-cli-auth", action="store_true")
    parser.add_argument("--codex-login", action="store_true", required=True)
    parser.add_argument("--case", choices=[c["name"] for c in SCENARIOS])
    args = parser.parse_args()
    _, workspace = prepare_home("scenarios", args.render_cli_auth)
    provider = load_provider()
    from hermes_cli.auth_codex import _import_codex_cli_tokens
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL

    tokens = _import_codex_cli_tokens()
    if not tokens or not tokens.get("access_token"):
        raise RuntimeError("An existing Codex login is required; no login is changed or refreshed.")
    now = datetime.now(UTC)
    run_dir = ROOT / "validation" / "private" / "scenarios" / now.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True)
    sources = [
        ROOT / "__init__.py",
        *sorted((ROOT / "hermes_render").glob("*.py")),
        Path(__file__),
        Path(__file__).with_name("scenario_fixtures.py"),
    ]
    report = {
        "started_at": now.isoformat(),
        "model": "gpt-6.1-sol",
        "provider": "openai-codex",
        "workspace_id": workspace,
        "plugin_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "cases": [],
    }

    def save(case):
        report["cases"] = [c for c in report["cases"] if c["name"] != case["name"]] + [case]
        write_json(run_dir / "report.json", report)
        write_json(ROOT / "validation" / "live-scenarios.json", report)

    for case in SCENARIOS:
        if not args.case or args.case == case["name"]:
            run_case(case, provider, tokens, DEFAULT_CODEX_BASE_URL, run_dir, save)
    report["all_passed"] = all(c["status"] == "passed" for c in report["cases"])
    report["sandbox_count"] = sum(len(c["sandboxes"]) for c in report["cases"])
    report["all_terminated"] = all(
        s.get("final_status") == "terminated" for c in report["cases"] for s in c["sandboxes"]
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_json(run_dir / "report.json", report)
    write_json(ROOT / "validation" / "live-scenarios.json", report)
    public = json.loads(json.dumps(report))
    del public["workspace_id"]
    for case in public["cases"]:
        for sandbox in case["sandboxes"]:
            del sandbox["id"]
        for failure in case["cleanup_failures"]:
            failure.pop("id", None)
    public["publication_note"] = (
        "Synthetic inputs; account and resource identifiers omitted. Raw reports and downloaded artifacts stay in validation/private/."
    )
    suffix = "-" + args.case if args.case else ""
    write_json(ROOT / "validation" / "public" / f"live-scenarios{suffix}.json", public)
    print(
        json.dumps(
            {
                "all_passed": report["all_passed"],
                "all_terminated": report["all_terminated"],
                "sandbox_count": report["sandbox_count"],
                "cases": [
                    {"name": c["name"], "status": c["status"], "tools": len(c["tool_calls"])}
                    for c in report["cases"]
                ],
            },
            indent=2,
        )
    )
    if not report["all_passed"] or not report["all_terminated"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
