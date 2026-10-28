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
    skill,
    tool,
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
    tool,
    skill,
)

__all__ = ["MODULES"]
