"""Registration and settings. No network requests during discovery or setup probes."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from agent.secret_scope import get_secret
from agent.terminal_env_provider import TerminalEnvironmentProvider


@dataclass(frozen=True)
class Settings:
    workspace_id: str
    lifetime_seconds: int = 900
    plan: str = "starter"
    network_policy: str = "deny-all"

    def __post_init__(self):
        if not isinstance(self.workspace_id, str) or not self.workspace_id.startswith("tea-"):
            raise ValueError(
                "Set plugins.entries.render.settings.workspace_id to your tea-... workspace ID."
            )
        if type(self.lifetime_seconds) is not int or not 60 <= self.lifetime_seconds <= 86400:
            raise ValueError("lifetime_seconds must be an integer from 60 to 86400.")
        if self.plan not in {"starter", "standard", "pro"}:
            raise ValueError("plan must be starter, standard, or pro.")
        if self.network_policy not in {"deny-all", "allow-all"}:
            raise ValueError(
                "network_policy must be deny-all or allow-all; allow-list is not supported by this SDK path."
            )


class RenderProvider(TerminalEnvironmentProvider):
    name = "render"
    display_name = "Render Sandboxes"
    is_remote = True
    is_container = True
    session_isolated_when_nonpersistent = True

    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def description(self):
        return "Run commands and edit files in a temporary Render Sandbox."

    @property
    def skip_container_guards(self):
        # Keep Hermes's normal approval policy in force.
        return False

    @property
    def cache_path_base(self):
        return "~/.hermes"

    @property
    def strip_env_keys(self):
        return frozenset({"RENDER_API_KEY"})

    def _settings(self):
        return Settings(
            **{
                key: self.ctx.get_config(key, default)
                for key, default in {
                    "workspace_id": "",
                    "lifetime_seconds": 900,
                    "plan": "starter",
                    "network_policy": "deny-all",
                }.items()
            }
        )

    def is_available(self):
        try:
            self._settings()
            return (
                bool(get_secret("RENDER_API_KEY"))
                and importlib.util.find_spec("render") is not None
            )
        except ValueError, RuntimeError:
            return False

    def setup_instructions(self):
        return [
            "Set RENDER_API_KEY in this Hermes profile's .env file.",
            "Set plugins.entries.render.settings.workspace_id to your Render workspace ID.",
            "Set terminal.container_persistent to false and terminal.cwd to /workspace.",
            "The workspace must have Render Sandboxes enabled. Sandboxes are billed to that workspace.",
        ]

    def create_environment(
        self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs
    ):
        from .environment import RenderEnvironment

        if (container_config or {}).get("container_persistent", True):
            raise ValueError(
                "This preview uses disposable sessions. Set terminal.container_persistent: false."
            )
        token = get_secret("RENDER_API_KEY")
        if not token:
            raise ValueError("Set RENDER_API_KEY in your Hermes profile before selecting Render.")
        return RenderEnvironment(
            settings=self._settings(),
            token=token,
            cwd=cwd or "/workspace",
            timeout=timeout,
            task_id=task_id,
        )
