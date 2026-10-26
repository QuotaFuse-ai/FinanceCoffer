"""Register placeholder commands for the P0 command tree.

Every command listed as P0 in ``docs/specs/cli.md`` exists after T-001 but
returns a stable ``E_NOT_IMPLEMENTED`` envelope. Unknown options are accepted
and ignored so the command line from the spec can be exercised today.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from financecoffer.cli.options import GlobalOptions
from financecoffer.cli.output import emit_envelope
from financecoffer.core.envelope import Envelope
from financecoffer.core.errors import NotImplementedYetError

_PLACEHOLDER_CONTEXT = {"ignore_unknown_options": True, "allow_extra_args": True}


def placeholder(path: str, summary: str) -> Callable[[typer.Context], None]:
    """Build a callback that reports ``path`` as not implemented yet."""

    def _callback(ctx: typer.Context) -> None:
        options: GlobalOptions = ctx.obj if ctx.obj is not None else GlobalOptions()
        error = NotImplementedYetError(
            f"fcoffer {path} is not implemented yet (T-001 skeleton)",
            details={"command": path, "task": "T-013"},
        )
        emit_envelope(Envelope.from_error(error, trace_id=options.trace_id), quiet=options.quiet)
        raise typer.Exit(code=error.exit_code)

    return _callback


def add_placeholder(
    app: typer.Typer,
    name: str,
    *,
    path: str,
    summary: str,
) -> None:
    """Attach a placeholder command named ``name`` to ``app``."""

    app.command(
        name=name,
        help=f"{summary} (placeholder; implemented in a later task)",
        context_settings=_PLACEHOLDER_CONTEXT,
    )(placeholder(path, summary))


def add_group(
    parent: typer.Typer,
    *,
    name: str,
    help_text: str,
) -> typer.Typer:
    """Create and attach a sub-application (command group)."""

    group = typer.Typer(help=help_text, no_args_is_help=True, add_completion=False)
    parent.add_typer(group, name=name)
    return group
