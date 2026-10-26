"""Monitoring commands."""

from __future__ import annotations

import typer

from financecoffer.cli.placeholder import add_placeholder


def register(app: typer.Typer) -> None:
    add_placeholder(app, "metrics", path="metrics", summary="Print Prometheus metrics")
    add_placeholder(app, "health", path="health", summary="Check service health")
