"""Run actual installed-provider and Hermes tool calls against a Render sandbox."""

import json
import sys
import time

from test_support import ROOT, load_provider, prepare_home


def main():
    _, workspace = prepare_home("smoke", "--render-cli-auth" in sys.argv)
    provider = load_provider()
    assert provider.is_available()
    from tools.file_tools import patch_tool, read_file_tool, write_file_tool
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_lifecycle import cleanup_vm, ensure_task_env

    started = time.monotonic()
    task = "render-smoke"
    report = {"workspace_id": workspace, "checks": []}
    env = None
    try:
        result = terminal_tool("printf 'hermes-render-ready'", task_id=task)
        print("terminal", result, flush=True)
        assert "hermes-render-ready" in result
        env = ensure_task_env(task)
        report["sandbox_id"] = env.sandbox_id
        report["checks"].append("installed plugin command dispatch")
        result = write_file_tool("/workspace/greeting.py", "print(6 * 7)\n", task_id=task)
        print("write", result, flush=True)
        result = terminal_tool("python3 /workspace/greeting.py", task_id=task)
        print("execute", result, flush=True)
        assert "42" in result
        result = read_file_tool("/workspace/greeting.py", task_id=task)
        assert "6 * 7" in result
        report["checks"].append("Hermes file write/read and Python execution")
        changed = patch_tool(
            path="/workspace/greeting.py", old_string="6 * 7", new_string="7 * 8", task_id=task
        )
        print("patch", changed, flush=True)
        result = terminal_tool("python3 /workspace/greeting.py", task_id=task)
        assert "56" in result
        report["checks"].append("Hermes patch tool")
        result = env.execute("mkdir -p /workspace/nested && cd /workspace/nested")
        assert result["returncode"] == 0
        result = env.execute("pwd")
        assert result["output"].strip() == "/workspace/nested"
        report["checks"].append("working directory persists across commands")
        result = env.execute("printf 'expected failure' >&2; exit 7")
        assert result["returncode"] == 7 and "expected failure" in result["output"]
        report["checks"].append("nonzero exit and stderr")
        result = env.execute("test -z \"${RENDER_API_KEY+x}\" && printf 'credential-absent'")
        assert result["returncode"] == 0 and "credential-absent" in result["output"]
        report["checks"].append("Render credential absent from sandbox")
    finally:
        cleanup_vm(task)
        if env is not None:
            # Hermes lifecycle logs cleanup errors, so explicitly verify through the provider too.
            env.cleanup()
            report["cleanup_verified"] = env._closed
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (ROOT / "validation" / "live-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
