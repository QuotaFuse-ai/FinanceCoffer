"""Envelope rendering for the CLI (stdout for data, stderr for diagnostics)."""

from __future__ import annotations

import sys

import typer

from financecoffer.core.envelope import Envelope


def emit_envelope(envelope: Envelope, *, quiet: bool = False) -> None:
    """Write the envelope as JSON.

    ``--quiet`` moves the envelope to stderr as well, matching the CLI contract
    that stdout carries data only. Format renderers (table/csv/yaml) arrive
    with the first real command in T-013; error envelopes stay JSON.
    """

    stream = sys.stderr if quiet else sys.stdout
    typer.echo(envelope.model_dump_json(indent=2), file=stream)
