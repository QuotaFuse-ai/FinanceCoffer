"""Model ↔ migrated-database parity.

Every column (name, type, nullability, default) and every primary key, unique
constraint, check constraint, foreign key and index must match one-to-one. The
database is produced by running the forward-only migrations, so a drift between
``Base.metadata`` and the DDL fails here before it reaches production.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from financecoffer.db import connect
from financecoffer.db.models import Base

SCHEMAS = ("ods", "dwd", "dws", "ops", "vector")
DIALECT = postgresql.dialect()
_DELETE_RULES = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}
_CAST = re.compile(r"::[a-z_ ]+(?:\([0-9, ]+\))?(?:\[\])?")


def _normalize_type(value: str) -> str:
    text = " ".join(value.lower().split())
    text = text.replace("character varying", "varchar")
    text = text.replace("character(", "char(")
    text = text.replace("timestamp with time zone", "timestamptz")
    text = text.replace(", ", ",")
    return text.replace(" ", "")


def _normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    return _CAST.sub("", value.lower()).replace(" ", "")


@pytest.fixture(scope="module")
def catalog(migrated_warehouse: str) -> Iterator[dict[str, Any]]:
    with connect(migrated_warehouse) as conn:
        yield {
            "tables": _db_tables(conn),
            "columns": _db_columns(conn),
            "constraints": _db_constraints(conn),
            "indexes": _db_indexes(conn),
            "partition_key": conn.execute(
                "SELECT pg_get_partkeydef('ods.stock_raw_data'::regclass)"
            ).fetchone()[0],
            "hnsw_reloptions": conn.execute(
                "SELECT reloptions FROM pg_class "
                "WHERE oid = 'vector.idx_ann_chunk_embedding'::regclass"
            ).fetchone()[0],
        }


def _db_tables(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('ods', 'dwd', 'dws', 'ops', 'vector')
          AND c.relkind IN ('r', 'p')
          AND NOT c.relispartition
        """
    ).fetchall()
    return {f"{schema}.{table}" for schema, table in rows}


def _db_columns(conn: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT n.nspname,
               c.relname,
               a.attname,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               NOT a.attnotnull AS is_nullable,
               pg_get_expr(d.adbin, d.adrelid) AS column_default
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname IN ('ods', 'dwd', 'dws', 'ops', 'vector')
          AND c.relkind IN ('r', 'p')
          AND NOT c.relispartition
          AND a.attnum > 0
          AND NOT a.attisdropped
        """
    ).fetchall()
    return {
        (schema, table, column): {
            "type": data_type,
            "nullable": nullable,
            "default": default,
        }
        for schema, table, column, data_type, nullable, default in rows
    }


def _db_constraints(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT n.nspname,
               c.relname,
               con.conname,
               con.contype,
               (SELECT array_agg(att.attname ORDER BY u.ord)
                  FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
                  JOIN pg_attribute att
                    ON att.attrelid = con.conrelid AND att.attnum = u.attnum)
                   AS columns,
               fn.nspname,
               fc.relname,
               (SELECT array_agg(att.attname ORDER BY u.ord)
                  FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
                  JOIN pg_attribute att
                    ON att.attrelid = con.confrelid AND att.attnum = u.attnum)
                   AS ref_columns,
               con.confdeltype,
               COALESCE(i.indnullsnotdistinct, false)
        FROM pg_constraint con
        LEFT JOIN pg_index i ON i.indexrelid = con.conindid
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_class fc ON fc.oid = con.confrelid
        LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
        WHERE n.nspname IN ('ods', 'dwd', 'dws', 'ops', 'vector')
          AND NOT c.relispartition
        """
    ).fetchall()
    catalog: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        (
            schema,
            table,
            name,
            kind,
            columns,
            ref_schema,
            ref_table,
            ref_columns,
            delete_rule,
            nulls_not_distinct,
        ) = row
        catalog.setdefault((schema, table), []).append(
            {
                "name": name,
                "kind": kind,
                "columns": tuple(columns or ()),
                "ref_schema": ref_schema,
                "ref_table": ref_table,
                "ref_columns": tuple(ref_columns or ()),
                "delete_rule": _DELETE_RULES.get(delete_rule, delete_rule),
                "nulls_not_distinct": nulls_not_distinct,
            }
        )
    return catalog


def _db_indexes(conn: Any) -> dict[tuple[str, str], set[str]]:
    rows = conn.execute(
        """
        SELECT n.nspname, c.relname, ic.relname
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('ods', 'dwd', 'dws', 'ops', 'vector')
          AND NOT c.relispartition
          AND NOT i.indisprimary
          AND NOT EXISTS (
              SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid
          )
        """
    ).fetchall()
    catalog: dict[tuple[str, str], set[str]] = {}
    for schema, table, index in rows:
        catalog.setdefault((schema, table), set()).add(index)
    return catalog


def _model_tables() -> dict[tuple[str, str], Any]:
    return {
        (table.schema or "public", table.name): table
        for table in Base.metadata.tables.values()
    }


def test_table_inventory_matches(catalog: dict[str, Any]) -> None:
    assert catalog["tables"] == set(Base.metadata.tables)


def test_column_names_types_nullability_and_defaults_match(catalog: dict[str, Any]) -> None:
    database = catalog["columns"]
    model: dict[tuple[str, str, str], Any] = {}
    for (schema, name), table in _model_tables().items():
        for column in table.columns:
            model[(schema, name, column.name)] = column

    assert set(database) == set(model)

    problems: list[str] = []
    for key, column in model.items():
        actual = database[key]
        model_type = _normalize_type(str(column.type.compile(dialect=DIALECT)))
        if _normalize_type(actual["type"]) != model_type:
            problems.append(
                f"{key}: type {actual['type']!r} != {model_type!r}"
            )
        if actual["nullable"] != column.nullable:
            problems.append(
                f"{key}: nullable {actual['nullable']} != {column.nullable}"
            )
        db_default = _normalize_default(actual["default"])
        if column.server_default is None:
            if db_default is not None and not db_default.startswith("nextval("):
                problems.append(f"{key}: unexpected default {actual['default']!r}")
        else:
            expected = _normalize_default(str(column.server_default.arg))
            if db_default != expected:
                problems.append(
                    f"{key}: default {actual['default']!r} != {expected!r}"
                )

    assert problems == []


def test_primary_keys_match(catalog: dict[str, Any]) -> None:
    model = {
        key: (
            table.primary_key.name,
            tuple(column.name for column in table.primary_key.columns),
        )
        for key, table in _model_tables().items()
    }
    database = {
        key: (constraint["name"], constraint["columns"])
        for key, constraints in catalog["constraints"].items()
        for constraint in constraints
        if constraint["kind"] == "p"
    }

    assert database == model


def test_unique_constraints_include_nulls_not_distinct(catalog: dict[str, Any]) -> None:
    model = {
        key: {
            (
                constraint.name,
                tuple(column.name for column in constraint.columns),
                bool(
                    constraint.dialect_kwargs.get(
                        "postgresql_nulls_not_distinct", False
                    )
                ),
            )
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        for key, table in _model_tables().items()
    }
    database = {
        key: {
            (
                constraint["name"],
                constraint["columns"],
                constraint["nulls_not_distinct"],
            )
            for constraint in constraints
            if constraint["kind"] == "u"
        }
        for key, constraints in catalog["constraints"].items()
    }

    assert {key: value for key, value in model.items() if value} == {
        key: value for key, value in database.items() if value
    }


def test_check_constraint_names_match(catalog: dict[str, Any]) -> None:
    model = {
        key: {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        for key, table in _model_tables().items()
    }
    database = {
        key: {
            constraint["name"]
            for constraint in constraints
            if constraint["kind"] == "c"
        }
        for key, constraints in catalog["constraints"].items()
    }

    assert {key: value for key, value in model.items() if value} == {
        key: value for key, value in database.items() if value
    }


def test_foreign_keys_match(catalog: dict[str, Any]) -> None:
    model = {
        key: {
            (
                foreign_key.name,
                tuple(element.parent.name for element in foreign_key.elements),
                foreign_key.referred_table.schema,
                foreign_key.referred_table.name,
                tuple(element.column.name for element in foreign_key.elements),
                foreign_key.ondelete or "NO ACTION",
            )
            for foreign_key in table.foreign_key_constraints
        }
        for key, table in _model_tables().items()
    }
    database = {
        key: {
            (
                constraint["name"],
                constraint["columns"],
                constraint["ref_schema"],
                constraint["ref_table"],
                constraint["ref_columns"],
                constraint["delete_rule"],
            )
            for constraint in constraints
            if constraint["kind"] == "f"
        }
        for key, constraints in catalog["constraints"].items()
    }

    assert {key: value for key, value in model.items() if value} == {
        key: value for key, value in database.items() if value
    }


def test_index_inventory_matches(catalog: dict[str, Any]) -> None:
    model = {
        key: {index.name for index in table.indexes}
        for key, table in _model_tables().items()
    }

    assert {key: value for key, value in model.items() if value} == {
        key: value for key, value in catalog["indexes"].items() if value
    }


def test_ods_partition_strategy_matches(catalog: dict[str, Any]) -> None:
    table = _model_tables()[("ods", "stock_raw_data")]

    assert table.kwargs["postgresql_partition_by"] == "RANGE (fetch_time)"
    assert catalog["partition_key"].upper() == "RANGE (FETCH_TIME)"


def test_hnsw_index_parameters_match(catalog: dict[str, Any]) -> None:
    options = set(catalog["hnsw_reloptions"] or [])

    assert options == {"m=16", "ef_construction=64"}
