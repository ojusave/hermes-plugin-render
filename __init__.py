"""Hermes loads this entry point from the installed plugin directory."""


def register(ctx):
    from .hermes_render.provider import RenderProvider

    ctx.register_terminal_environment_provider(RenderProvider(ctx))
