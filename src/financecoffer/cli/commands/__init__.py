"""CLI command modules. Each module exposes ``register(app)``."""

from financecoffer.cli.commands import (
    bootstrap,
    db,
    etl,
    ingest,
    mcp,
    monitor,
    ods,
    query,
    serve,
    vector,
    warehouse,
)

MODULES = (
    bootstrap,
    db,
    serve,
    mcp,
    etl,
    ingest,
    ods,
    warehouse,
    vector,
    query,
    monitor,
)

__all__ = ["MODULES"]
