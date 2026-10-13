"""Verify Python-tool dispatch, shell state and normal Hermes skill synchronization."""

import json
import shlex
import sys

from test_support import ROOT, load_provider, prepare_home


def main():
    home, _ = prepare_home("features", "--render-cli-auth" in sys.argv)
    skill = home / "skills" / "render-fixture" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: render-fixture\ndescription: Synthetic synchronization fixture.\n---\noriginal-fixture\n"
    )
    load_provider()
    from tools.code_execution_tool import execute_code
    from tools.terminal_tool_lifecycle import cleanup_vm, ensure_task_env

    task = "render-features"
    env = None
    report = {"checks": []}
    try:
        env = ensure_task_env(task)
        report["sandbox_id"] = env.sandbox_id
        remote_skill = f"{env._remote_state}/skills/render-fixture/SKILL.md"
        assert "original-fixture" in env.execute(f"cat {shlex.quote(remote_skill)}")["output"]
        report["checks"].append("Hermes skill synchronized to remote workspace")
        result = env.execute(f"printf '\\nremote-fixture\\n' >> {shlex.quote(remote_skill)}")
        assert result["returncode"] == 0
        first = json.loads(
            execute_code("value = 20\nprint(value + 22)", task_id=task, enabled_tools=["terminal"])
        )
        print("execute_code", first, flush=True)
        assert first.get("output", "").strip() == "42", first
        report["checks"].append("Hermes execute_code runs Python on Render")
        second = json.loads(
            execute_code("print(value + 23)", task_id=task, enabled_tools=["terminal"])
        )
        assert second.get("output", "").strip() == "43", second
        report["checks"].append("Python kernel retains variables across calls")
        result = env.execute("export HERMES_RENDER_TEST_VALUE=retained")
        assert result["returncode"] == 0
        observed = env.execute('printf "%s" "$HERMES_RENDER_TEST_VALUE"')
        assert observed["output"] == "retained", observed
        report["checks"].append("Shell environment retained across commands")
    finally:
        cleanup_vm(task)
        if env:
            env.cleanup()
            state = env._api.from_id(env.sandbox_id)
            report["cleanup_verified"] = (
                state.status == "terminated" or state.terminated_at is not None
            )
            assert report["cleanup_verified"]
        report["skill_sync_back_verified"] = "remote-fixture" in skill.read_text()
        (ROOT / "validation" / "live-features.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["skill_sync_back_verified"]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
