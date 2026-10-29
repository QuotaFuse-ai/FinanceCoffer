"""Local MinIO process commands: ``status``, ``ensure``, ``start`` and ``stop``.

The archive talks to the object store over S3, so nothing here is on the read
path. These commands exist because the store is a local process (DEC-0014) and
a reboot used to leave it down until someone started it by hand (DEC-0028).

``ensure`` is the one a launcher calls: it does nothing unless
``FC_MINIO_AUTOSTART=true``, and reports ``skipped`` when the switch is off
rather than failing. ``start`` is the explicit, always-attempts variant.
"""

from __future__ import annotations

from typing import Any

import typer

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import FinanceCofferError
from financecoffer.ops import minio_process as runtime


def _wait_option(default: float, help_text: str) -> Any:
    """Build a fresh ``--wait`` option; typer options must not be shared."""

    return typer.Option(default, "--wait", min=1.0, max=300.0, help=help_text)


def register(app: typer.Typer) -> None:
    group = add_group(
        app,
        name="minio",
        help_text="Inspect or control the local object store process.",
    )

    @group.command("status", help="Report whether the store answers and how it would start.")
    def status(ctx: typer.Context) -> None:
        options = from_context(ctx)
        try:
            config = load_cli_config(db_url=options.db_url)
            data = runtime.state(config)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="minio"))

    @group.command("ensure", help="Start the store only when FC_MINIO_AUTOSTART is enabled.")
    def ensure(
        ctx: typer.Context,
        wait: float = _wait_option(20.0, "Seconds to wait for the health endpoint."),
    ) -> None:
        options = from_context(ctx)
        try:
            config = load_cli_config(db_url=options.db_url)
            data = runtime.ensure(config, wait_seconds=wait)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="minio"))

    @group.command("start", help="Start the store if it is not already answering.")
    def start(
        ctx: typer.Context,
        wait: float = _wait_option(20.0, "Seconds to wait for the health endpoint."),
    ) -> None:
        options = from_context(ctx)
        try:
            config = load_cli_config(db_url=options.db_url)
            data = runtime.start(config, wait_seconds=wait)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="minio"))

    @group.command("stop", help="Stop the process recorded in the pid file.")
    def stop(
        ctx: typer.Context,
        wait: float = _wait_option(5.0, "Seconds to wait for the process to exit."),
    ) -> None:
        options = from_context(ctx)
        try:
            config = load_cli_config(db_url=options.db_url)
            data = runtime.stop(config, timeout=wait)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        emit_success(options, data, meta=Meta(source="minio"))
