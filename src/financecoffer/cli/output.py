"""Envelope rendering for the CLI (stdout for data, stderr for diagnostics)."""

from __future__ import annotations

import sys
from typing import Any, NoReturn

import typer

from financecoffer.cli.encoding import ensure_utf8_stdio
from financecoffer.cli.options import GlobalOptions
from financecoffer.core.envelope import Envelope, Meta
from financecoffer.core.errors import FinanceCofferError


def emit_envelope(envelope: Envelope, *, quiet: bool = False) -> None:
    """Write the envelope as JSON.

    ``--quiet`` moves the envelope to stderr as well, matching the CLI contract
    that stdout carries data only. Format renderers (table/csv/yaml) arrive
    with the first real command in T-013; error envelopes stay JSON.
    """

    ensure_utf8_stdio()
    stream = sys.stderr if quiet else sys.stdout
    typer.echo(envelope.model_dump_json(indent=2), file=stream)


def emit_success(options: GlobalOptions, data: Any, *, meta: Meta | None = None) -> None:
    """Render a successful envelope tagged with the run's trace id."""

    emit_envelope(
        Envelope.success(data, meta=meta, trace_id=options.trace_id),
        quiet=options.quiet,
    )


def emit_failure(options: GlobalOptions, error: FinanceCofferError) -> NoReturn:
    """Render an error envelope and exit with the code mapped from ``error``."""

    emit_envelope(Envelope.from_error(error, trace_id=options.trace_id), quiet=options.quiet)
    raise typer.Exit(code=error.exit_code)
