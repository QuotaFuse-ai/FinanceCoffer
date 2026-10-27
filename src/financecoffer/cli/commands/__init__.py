"""CLI command modules. Each module exposes ``register(app)``."""

from financecoffer.cli.commands import (
    archive,
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
    archive,
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
