"""Universal tool registry commands (``tool list`` / ``tool call``).

The catalogue is ``financecoffer.mcp.tools.TOOL_SPECS`` -- the same data the MCP
server registers -- so a tool's name, parameters and JSON schema cannot drift
between the CLI and the MCP surface. ``tool call`` validates ``--params`` against
that schema and dispatches to :class:`~financecoffer.query.QueryService` with
the exact signatures the MCP adapters use.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.config.loader import load_cli_config
from financecoffer.core.errors import FinanceCofferError, InvalidArgumentError
from financecoffer.core.logging import log_query_audit
from financecoffer.mcp.tools import TOOL_SPECS
from financecoffer.query import QueryService

LAYERS: tuple[str, ...] = ("structured", "vector", "risk")
LAYER_BY_TOOL: dict[str, str] = {
    "query_structured_data": "structured",
    "search_announcements_semantically": "vector",
    "get_risk_history": "risk",
}

_SPECS: dict[str, dict[str, Any]] = {spec["name"]: spec for spec in TOOL_SPECS}


def _signature(spec: Mapping[str, Any]) -> str:
    properties = spec["inputSchema"]["properties"]
    return f"{spec['name']}({', '.join(properties)})"


def _layer(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in LAYERS:
        raise InvalidArgumentError(
            "layer must be one of: " + ", ".join(LAYERS),
            details={"field": "layer", "value": value, "valid": list(LAYERS)},
        )
    return normalized


def _spec(name: str) -> dict[str, Any]:
    spec = _SPECS.get(name.strip())
    if spec is None:
        raise InvalidArgumentError(
            f"unknown tool: {name}",
            details={
                "field": "tool-name",
                "value": name,
                "valid": sorted(_SPECS),
            },
        )
    return spec


def _enum(property_schema: Mapping[str, Any]) -> list[Any] | None:
    if "enum" in property_schema:
        return list(property_schema["enum"])
    for branch in property_schema.get("anyOf", ()):
        if isinstance(branch, Mapping) and "enum" in branch:
            return list(branch["enum"])
    return None


def _check_type(name: str, value: Any, property_schema: Mapping[str, Any]) -> None:
    if value is None:
        return
    declared = property_schema.get("type")
    if declared is None:
        for branch in property_schema.get("anyOf", ()):
            if isinstance(branch, Mapping) and branch.get("type") not in (None, "null"):
                declared = branch["type"]
                break
    if declared == "string" and not isinstance(value, str):
        raise InvalidArgumentError(
            f"parameter '{name}' must be a string",
            details={"field": "params", "parameter": name, "got": type(value).__name__},
        )


def validate_params(spec: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """Reject unknown, missing, mistyped or out-of-enum parameters."""

    schema = spec["inputSchema"]
    properties = schema["properties"]
    unknown = sorted(set(params) - set(properties))
    if unknown:
        raise InvalidArgumentError(
            f"{spec['name']} does not accept: " + ", ".join(unknown),
            details={
                "field": "params",
                "unknown": unknown,
                "allowed": sorted(properties),
            },
        )
    missing = [name for name in schema.get("required", ()) if params.get(name) is None]
    if missing:
        raise InvalidArgumentError(
            f"{spec['name']} requires: " + ", ".join(missing),
            details={"field": "params", "missing": missing, "required": list(schema["required"])},
        )
    for name, value in params.items():
        _check_type(name, value, properties[name])
        allowed = _enum(properties[name])
        if allowed is not None and value is not None and value not in allowed:
            raise InvalidArgumentError(
                f"parameter '{name}' must be one of: " + ", ".join(str(item) for item in allowed),
                details={
                    "field": "params",
                    "parameter": name,
                    "value": value,
                    "valid": allowed,
                },
            )
    return {name: value for name, value in params.items() if value is not None}


def _parse_params(raw: str) -> dict[str, Any]:
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentError(
            "--params is not valid JSON",
            details={"field": "params", "reason": f"line {exc.lineno} column {exc.colno}"},
        ) from exc
    if not isinstance(params, Mapping):
        raise InvalidArgumentError(
            "--params must be a JSON object",
            details={"field": "params", "got": type(params).__name__},
        )
    return dict(params)


def _service(options: GlobalOptions) -> QueryService:
    try:
        config = load_cli_config(db_url=options.db_url)
    except FinanceCofferError as exc:
        emit_failure(options, exc)
    return QueryService(config=config, dsn=options.db_url, timeout=options.timeout)


def _dispatch(
    service: QueryService, spec: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[Any, int | None, str | None]:
    """Call the service with the MCP adapter's signature; return hits and year."""

    name = spec["name"]
    symbol = str(params["symbol"])
    year_range = params.get("year_range")
    if name == "query_structured_data":
        result = service.structured(
            symbol,
            metric=params.get("metric"),
            year_range=year_range,
        )
        return result, len(result.data.get("years") or ()), year_range
    if name == "search_announcements_semantically":
        result = service.search_announcements(
            symbol,
            str(params["query_text"]),
            year_range=year_range,
        )
        return result, result.data.get("count"), year_range
    result = service.risk(symbol)
    return result, result.data.get("count"), None


def _project(data: Mapping[str, Any], fields: str | None) -> dict[str, Any]:
    if not fields:
        return dict(data)
    names = [part.strip() for part in fields.split(",") if part.strip()]
    if not names:
        raise InvalidArgumentError(
            "--fields must name at least one field",
            details={"field": "fields", "value": fields},
        )
    unknown = [name for name in names if name not in data]
    if unknown:
        raise InvalidArgumentError(
            "unknown field(s) for this tool: " + ", ".join(unknown),
            details={"field": "fields", "unknown": unknown, "available": sorted(data)},
        )
    return {name: data[name] for name in names}


def register(app: typer.Typer) -> None:
    tool = add_group(app, name="tool", help_text="Universal tool registry.")

    @tool.command("list", help="List the tools shared with the MCP server.")
    def list_tools(
        ctx: typer.Context,
        layer: str | None = typer.Option(
            None, "--layer", help="Filter by layer: " + " | ".join(LAYERS) + "."
        ),
    ) -> None:
        options = from_context(ctx)
        try:
            wanted = _layer(layer)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        specs = [
            spec for spec in TOOL_SPECS if wanted is None or LAYER_BY_TOOL[spec["name"]] == wanted
        ]
        payload = [
            {
                "name": spec["name"],
                "layer": LAYER_BY_TOOL[spec["name"]],
                "signature": _signature(spec),
                "description": spec["description"],
                "input_schema": spec["inputSchema"],
            }
            for spec in specs
        ]
        emit_success(
            options,
            {"count": len(payload), "layer": wanted, "tools": payload},
            meta=None,
        )

    @tool.command("call", help="Call one registered tool with JSON parameters.")
    def call(
        ctx: typer.Context,
        tool_name: str = typer.Argument(..., help="Tool name from `fcoffer tool list`."),
        params: str = typer.Option("{}", "--params", help="JSON object of tool parameters."),
        fields: str | None = typer.Option(
            None, "--fields", help="Comma-separated projection over the result data."
        ),
    ) -> None:
        options = from_context(ctx)
        started = time.perf_counter()
        audit_tool = tool_name
        try:
            spec = _spec(tool_name)
            audit_tool = spec["name"]
            cleaned = validate_params(spec, _parse_params(params))
            result, hits, year_range = _dispatch(_service(options), spec, cleaned)
            projected = _project(result.data, fields)
        except FinanceCofferError as exc:
            log_query_audit(
                tool=audit_tool,
                trace_id=options.trace_id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                outcome=exc.code.value,
            )
            emit_failure(options, exc)
        log_query_audit(
            tool=spec["name"],
            trace_id=options.trace_id,
            symbol=str(cleaned["symbol"]),
            year_range=year_range,
            top_k=10 if spec["name"] == "search_announcements_semantically" else None,
            hits=hits,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            outcome="ok",
        )
        emit_success(options, projected, meta=result.meta(options.trace_id))


__all__ = ["LAYERS", "LAYER_BY_TOOL", "register", "validate_params"]
