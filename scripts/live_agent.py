"""Real Hermes/model loop plus fresh-sandbox verification of the downloaded result."""

import hashlib
import json
import sys
import time

from test_support import ROOT, load_provider, prepare_home

BUGGY = '''def total_positive(values):
    """Sum every strictly positive number; do not modify the input."""
    return sum(value for value in values[:-1] if value > 0)
'''
TESTS = """import unittest
from totals import total_positive
class TotalsTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(total_positive([]), 0)
    def test_single(self): self.assertEqual(total_positive([7]), 7)
    def test_mixed(self): self.assertEqual(total_positive([-4, 0, 2, 9]), 11)
    def test_all_negative(self): self.assertEqual(total_positive([-2, -1]), 0)
    def test_no_mutation(self):
        values = [2, -3, 5]
        self.assertEqual(total_positive(values), 7)
        self.assertEqual(values, [2, -3, 5])
if __name__ == '__main__': unittest.main()
"""


def main():
    if "--codex-login" not in sys.argv:
        raise RuntimeError(
            "Pass --codex-login to authorize read-only use of the existing Codex login."
        )
    prepare_home("agent", "--render-cli-auth" in sys.argv)
    provider = load_provider()
    from hermes_cli.auth_codex import _import_codex_cli_tokens
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    from run_agent import AIAgent
    from tools.terminal_tool_lifecycle import cleanup_vm, ensure_task_env

    tokens = _import_codex_cli_tokens()
    if not tokens or not tokens.get("access_token"):
        raise RuntimeError(
            "An existing Codex access token is required. This script does not sign in or refresh it."
        )
    artifacts = ROOT / "validation" / "agent-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "original_totals.py").write_text(BUGGY)
    (artifacts / "test_totals.py").write_text(TESTS)
    solution = artifacts / "verified_totals.py"
    solution.unlink(missing_ok=True)
    report = {
        "model": "gpt-6.1-sol",
        "provider": "openai-codex",
        "tool_calls": [],
        "sandboxes": [],
        "cleanup_failures": [],
    }
    environments = []
    started = time.monotonic()
    task_id = "render-agent-acceptance"
    agent = None
    try:
        env = ensure_task_env(task_id)
        environments.append(env)
        report["sandboxes"].append(env.sandbox_id)
        env._api.copy_to(
            env.sandbox_id, str(artifacts / "original_totals.py"), "/workspace/totals.py"
        )
        env._api.copy_to(
            env.sandbox_id, str(artifacts / "test_totals.py"), "/workspace/test_totals.py"
        )
        before = env.execute("cd /workspace && python3 -m unittest -v test_totals")
        assert before["returncode"] != 0
        report["original_fails"] = True

        def completed(call_id, name, arguments, result):
            report["tool_calls"].append(name)
            # Export before Hermes's normal end-of-turn cleanup. No core hooks are replaced.
            env._api.copy_from(env.sandbox_id, "/workspace/totals.py", str(solution))

        agent = AIAgent(
            api_key=tokens["access_token"],
            base_url=DEFAULT_CODEX_BASE_URL,
            provider="openai-codex",
            api_mode="codex_responses",
            model=report["model"],
            enabled_toolsets=["terminal", "file"],
            quiet_mode=True,
            max_iterations=12,
            run_budget_seconds=180,
            max_tokens=4000,
            skip_memory=True,
            skip_context_files=True,
            skip_background_review=True,
            save_trajectories=False,
            cwd="/workspace",
            tool_complete_callback=completed,
            reasoning_config={"effort": "low"},
        )
        answer = agent.run_conversation(
            "In /workspace, fix totals.py so total_positive sums every strictly positive number "
            "without modifying the input. Read the files, reproduce the failure, edit only totals.py, "
            "and run python3 -m unittest -v test_totals. Do not change the tests. "
            "This is a synthetic integration test; stay within /workspace and use only the terminal and file tools. "
            "Summarize the change and actual test result.",
            task_id=task_id,
        )
        report["agent_completed"] = bool(answer.get("completed", not answer.get("error")))
        report["api_calls"] = answer.get("api_calls")
        report["final_response"] = answer.get("final_response", "")
        assert report["agent_completed"], "Model turn did not complete."
        assert report["tool_calls"] and solution.exists(), (
            "No actual tool-produced result was captured."
        )
        assert solution.read_text() != BUGGY, "The agent did not change the buggy implementation."
        # Re-run the original, trusted tests in a separate clean sandbox.
        verifier = provider.create_environment(
            cwd="/workspace",
            timeout=30,
            task_id="independent-verifier",
            container_config={"container_persistent": False},
        )
        environments.append(verifier)
        report["sandboxes"].append(verifier.sandbox_id)
        verifier._api.copy_to(verifier.sandbox_id, str(solution), "/workspace/totals.py")
        verifier._api.copy_to(
            verifier.sandbox_id, str(artifacts / "test_totals.py"), "/workspace/test_totals.py"
        )
        verification = verifier.execute(
            "cd /workspace && python3 -I -m unittest discover -s . -p test_totals.py -v"
        )
        report["independent_verification"] = verification
        assert verification["returncode"] == 0, verification
        report["solution_sha256"] = hashlib.sha256(solution.read_bytes()).hexdigest()
        report["tests_sha256"] = hashlib.sha256(TESTS.encode()).hexdigest()
    finally:
        cleanup_vm(task_id)
        for env in environments:
            try:
                env.cleanup()
                state = env._api.from_id(env.sandbox_id)
                assert state.status == "terminated" or state.terminated_at is not None
            except Exception as exc:
                report["cleanup_failures"].append(
                    {"id": env.sandbox_id, "error_type": type(exc).__name__}
                )
        if agent is not None:
            agent.close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (ROOT / "validation" / "live-agent.json").write_text(json.dumps(report, indent=2) + "\n")
        if report["cleanup_failures"]:
            raise RuntimeError("Sandbox cleanup needs attention; see validation/live-agent.json.")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
