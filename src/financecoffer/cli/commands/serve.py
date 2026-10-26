"""Service commands: serve api/admin and start-all."""

from __future__ import annotations

import os

import typer

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure
from financecoffer.cli.placeholder import add_group, add_placeholder
from financecoffer.config.loader import load_config
from financecoffer.core.errors import DependencyError, FinanceCofferError, InvalidArgumentError

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def register(app: typer.Typer) -> None:
    serve = add_group(app, name="serve", help_text="Run FinanceCoffer services.")

    @serve.command("api", help="Run the REST API server (FastAPI + uvicorn).")
    def api(
        ctx: typer.Context,
        host: str = typer.Option(
            "127.0.0.1", "--host", help="Bind address; only loopback is allowed for now."
        ),
        port: int = typer.Option(8100, "--port", min=1, max=65535, help="Listen port."),
        workers: int = typer.Option(1, "--workers", min=1, max=64, help="Worker processes."),
    ) -> None:
        options = from_context(ctx)
        if host not in _LOOPBACK_HOSTS:
            emit_failure(
                options,
                InvalidArgumentError(
                    "serving on a non-loopback address requires token auth and a Host allowlist",
                    details={
                        "host": host,
                        "hint": "bind 127.0.0.1 and put an authenticating proxy in front",
                    },
                ),
            )
        try:
            config = load_config(overrides={"db_url": options.db_url})
        except FinanceCofferError as exc:
            emit_failure(options, exc)

        try:
            import uvicorn

            from financecoffer.api import create_app
        except ImportError:
            emit_failure(
                options,
                DependencyError(
                    "the REST API extras are not installed",
                    details={"hint": "install financecoffer[api]"},
                ),
            )

        log_level = config.log_level.lower()
        if workers > 1:
            # uvicorn needs an import string to fork workers; each worker reads
            # the same FC_* environment, so the CLI override is exported first.
            if options.db_url is not None:
                os.environ["FC_DB_URL"] = options.db_url
            uvicorn.run(
                "financecoffer.api.app:create_app",
                factory=True,
                host=host,
                port=port,
                workers=workers,
                log_level=log_level,
            )
        else:
            uvicorn.run(create_app(config=config), host=host, port=port, log_level=log_level)

    add_placeholder(serve, "admin", path="serve admin", summary="Run the admin UI server")
    add_placeholder(
        app, "start-all", path="start-all", summary="Run API and the ETL consumer together"
    )
