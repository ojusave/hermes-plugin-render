"""Assemble and validate a clean plugin payload and a separate reviewable source tree."""

import json
import os
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "__init__.py",
    "plugin.yaml",
    "pyproject.toml",
    "config.example.yaml",
    "README.md",
    "VALIDATION.md",
)


def main():
    os.environ["HERMES_HOME"] = str(ROOT / ".test-home" / "validate")
    os.environ["HERMES_RUNTIME_DIR"] = str(ROOT / ".runtime")
    from hermes_cli.plugin_validate import validate_plugin_dir

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for label in ("render", "source-review"):
        target = dist / label
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        for name in RUNTIME_FILES:
            shutil.copy2(ROOT / name, target / name)
        shutil.copytree(
            ROOT / "hermes_render",
            target / "hermes_render",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        evidence = target / "validation"
        evidence.mkdir()
        shutil.copytree(ROOT / "validation" / "public", evidence / "public")
        shutil.copytree(
            ROOT / "validation" / "agent-artifacts",
            evidence / "agent-artifacts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        if label == "source-review":
            for name in ("tests", "scripts", "dev"):
                shutil.copytree(
                    ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__")
                )
        result = validate_plugin_dir(target).to_dict()
        (ROOT / "validation" / f"plugin-{label}-validation.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "target": label,
                    "ok": result["ok"],
                    "failures": [c for c in result["checks"] if not c["ok"]],
                    "warnings": result["warnings"],
                    "isolation": result["isolation"],
                },
                indent=2,
            )
        )
        if not result["ok"]:
            raise SystemExit(1)
    archive = dist / "hermes-render-0.1.0a1.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(
            dist / "render",
            arcname="render",
            filter=lambda item: None if "__pycache__" in Path(item.name).parts else item,
        )
    print(f"Packaged: {archive}")


if __name__ == "__main__":
    main()
