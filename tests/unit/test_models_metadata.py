"""Static checks that the ORM metadata mirrors the migration schema.

These tests need no database; the byte-for-byte comparison against a migrated
database lives in ``tests/integration/test_schema_parity_integration.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Boolean, CheckConstraint, Float, Numeric, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.schema import Table

from financecoffer.db.models import Base
from financecoffer.db.models.types import Vector

TABLES = {
    "ods.stock_raw_data",
    "dwd.stock_financial_snapshot",
    "dwd.stock_announcement",
    "dwd.stock_kline",
    "dwd.stock_risk_event",
    "dwd.stock_quote_snapshot",
    "dwd.stock_research_report",
    "dwd.northbound_flow",
    "dws.stock_agent_analysis_wide",
    "vector.announcement_chunk",
    "ops.source_heartbeat",
    "ops.etl_event_log",
    "ops.etl_consumer_offset",
    "ops.etl_dlq",
    "ops.archive_job",
    "ops.schema_version",
}

PRIMARY_KEYS = {
    "ods.stock_raw_data": ("id", "fetch_time"),
    "dwd.stock_financial_snapshot": (
        "symbol",
        "report_date",
        "report_type",
        "statement",
        "version",
    ),
    "dwd.stock_announcement": ("id",),
    "dwd.stock_kline": ("symbol", "trade_date", "adjust", "version"),
    "dwd.stock_risk_event": ("id",),
    "dwd.stock_quote_snapshot": ("symbol", "trade_date", "version"),
    "dwd.stock_research_report": ("id",),
    "dwd.northbound_flow": ("trade_date", "channel"),
    "dws.stock_agent_analysis_wide": ("symbol",),
    "vector.announcement_chunk": ("id",),
    "ops.source_heartbeat": ("source",),
    "ops.etl_event_log": ("event_id",),
    "ops.etl_consumer_offset": ("consumer_group", "stream", "shard"),
    "ops.etl_dlq": ("id",),
    "ops.archive_job": ("id",),
    "ops.schema_version": ("version",),
}

UNIQUE_CONSTRAINTS: dict[str, set[tuple[str, ...]]] = {
    "dwd.stock_financial_snapshot": {("event_id",)},
    "dwd.stock_announcement": {("content_hash",), ("event_id",)},
    "dwd.stock_kline": {("event_id",)},
    "dwd.stock_risk_event": {("event_id",)},
    "dwd.stock_quote_snapshot": {("event_id",)},
    "dwd.stock_research_report": {("event_id",)},
    "dwd.northbound_flow": {("event_id",)},
    "vector.announcement_chunk": {("dwd_ref_id", "chunk_index")},
    "ops.etl_event_log": {("provider", "data_type", "symbol", "biz_key")},
}

CHECK_NAMES = {
    "ods.stock_raw_data": {
        "ck_ods_etl_status",
        "ck_ods_raw_payload",
        "ck_ods_raw_compliance_tier",
    },
    "dwd.stock_financial_snapshot": {
        "ck_fin_snapshot_version",
        "ck_fin_snapshot_quality",
        "ck_fin_snapshot_compliance_tier",
    },
    "dwd.stock_announcement": {
        "ck_ann_quality",
        "ck_ann_content_hash",
        "ck_ann_compliance_tier",
    },
    "dwd.stock_kline": {
        "ck_kline_version",
        "ck_kline_quality",
        "ck_kline_high_low",
        "ck_kline_compliance_tier",
    },
    "dwd.stock_risk_event": {
        "ck_risk_severity",
        "ck_risk_quality",
        "ck_risk_compliance_tier",
    },
    "dwd.stock_quote_snapshot": {
        "ck_quote_version",
        "ck_quote_quality",
        "ck_quote_compliance_tier",
    },
    "dwd.stock_research_report": {
        "ck_research_quality",
        "ck_report_compliance_tier",
    },
    "dwd.northbound_flow": {
        "ck_northbound_quality",
        "ck_northbound_compliance_tier",
    },
    "dws.stock_agent_analysis_wide": {
        "ck_dws_wide_fiscal_years",
        "ck_dws_wide_metrics",
        "ck_dws_wide_compliance_tier",
    },
    "vector.announcement_chunk": {
        "ck_ann_chunk_index",
        "ck_ann_chunk_tokens",
    },
    "ops.source_heartbeat": {
        "ck_source_heartbeat_status",
        "ck_source_heartbeat_items",
    },
    "ops.etl_event_log": {
        "ck_etl_event_log_status",
        "ck_etl_event_log_attempts",
    },
    "ops.etl_dlq": {
        "ck_etl_dlq_status",
        "ck_etl_dlq_retry_count",
    },
    "ops.archive_job": {
        "ck_archive_job_status",
        "ck_archive_job_rows",
    },
}

INDEX_NAMES = {
    "ods.stock_raw_data": {
        "idx_ods_raw_event_id",
        "idx_ods_raw_symbol_time",
        "idx_ods_raw_type_time",
        "idx_ods_raw_status",
    },
    "dwd.stock_financial_snapshot": {
        "idx_fin_snapshot_symbol_date",
        "idx_fin_snapshot_symbol_statement",
        "idx_fin_snapshot_symbol_type_version",
    },
    "dwd.stock_announcement": {"idx_ann_symbol_date", "idx_ann_type_date"},
    "dwd.stock_kline": {"idx_kline_symbol_date"},
    "dwd.stock_risk_event": {"idx_risk_symbol_date", "idx_risk_severity_date"},
    "dwd.stock_quote_snapshot": {"idx_quote_symbol_date"},
    "dwd.stock_research_report": {"idx_research_symbol_date"},
    "dwd.northbound_flow": {"idx_northbound_date"},
    "dws.stock_agent_analysis_wide": {"idx_dws_wide_as_of"},
    "vector.announcement_chunk": {
        "idx_ann_chunk_filter",
        "idx_ann_chunk_ref",
        "idx_ann_chunk_embedding",
    },
    "ops.source_heartbeat": {"idx_source_heartbeat_last_seen"},
    "ops.etl_event_log": {"idx_etl_event_log_status", "idx_etl_event_log_type"},
    "ops.etl_dlq": {"idx_etl_dlq_status_created", "idx_etl_dlq_event_id"},
    "ops.archive_job": {"idx_archive_job_status", "idx_archive_job_symbol"},
}

MONEY_COLUMNS = {
    ("dwd.stock_financial_snapshot", "amount_revenue"): (20, 4),
    ("dwd.stock_financial_snapshot", "amount_net_profit"): (20, 4),
    ("dwd.stock_kline", "open"): (20, 4),
    ("dwd.stock_kline", "high"): (20, 4),
    ("dwd.stock_kline", "low"): (20, 4),
    ("dwd.stock_kline", "close"): (20, 4),
    ("dwd.stock_kline", "volume"): (24, 4),
    ("dwd.stock_kline", "amount"): (24, 4),
    ("dwd.stock_quote_snapshot", "close"): (20, 4),
    ("dwd.stock_quote_snapshot", "total_market_cap"): (24, 4),
    ("dwd.stock_quote_snapshot", "pe_ttm"): (20, 4),
    ("dwd.stock_research_report", "target_price"): (20, 4),
    ("dwd.northbound_flow", "net_buy_amount"): (24, 4),
    ("dwd.northbound_flow", "buy_amount"): (24, 4),
    ("dwd.northbound_flow", "sell_amount"): (24, 4),
}

COMPLIANCE_TABLES = {
    "ods.stock_raw_data",
    "dwd.stock_financial_snapshot",
    "dwd.stock_announcement",
    "dwd.stock_kline",
    "dwd.stock_risk_event",
    "dwd.stock_quote_snapshot",
    "dwd.stock_research_report",
    "dwd.northbound_flow",
    "dws.stock_agent_analysis_wide",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_metadata_declares_every_migration_table() -> None:
    assert set(Base.metadata.tables) == TABLES


def test_primary_keys_match_the_ddl() -> None:
    for name, expected in PRIMARY_KEYS.items():
        table = _table(name)
        assert tuple(column.name for column in table.primary_key.columns) == expected
        assert table.primary_key.name == f"{table.name}_pkey"


def test_unique_constraints_match_the_ddl() -> None:
    actual = {
        name: {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        for name, table in Base.metadata.tables.items()
    }
    actual = {name: value for name, value in actual.items() if value}
    assert actual == UNIQUE_CONSTRAINTS


def test_check_constraints_match_the_ddl() -> None:
    actual = {
        name: {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        for name, table in Base.metadata.tables.items()
    }
    actual = {name: value for name, value in actual.items() if value}
    assert actual == CHECK_NAMES


def test_explicit_indexes_match_the_ddl() -> None:
    actual = {
        name: {index.name for index in table.indexes}
        for name, table in Base.metadata.tables.items()
    }
    actual = {name: value for name, value in actual.items() if value}
    assert actual == INDEX_NAMES


def test_no_column_uses_float() -> None:
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


@pytest.mark.parametrize(("key", "precision_scale"), MONEY_COLUMNS.items())
def test_money_columns_are_exact_numeric(
    key: tuple[str, str], precision_scale: tuple[int, int]
) -> None:
    table_name, column_name = key
    column_type = _table(table_name).c[column_name].type

    assert isinstance(column_type, Numeric)
    assert (column_type.precision, column_type.scale) == precision_scale


def test_ods_is_range_partitioned_by_fetch_time() -> None:
    table = _table("ods.stock_raw_data")

    assert table.kwargs["postgresql_partition_by"] == "RANGE (fetch_time)"


def test_announcement_chunk_embedding_is_a_1024_dim_vector() -> None:
    column_type = _table("vector.announcement_chunk").c["embedding"].type

    assert isinstance(column_type, Vector)
    assert column_type.dimensions == 1024


def test_announcement_chunk_uses_an_hnsw_cosine_index() -> None:
    index = next(
        item
        for item in _table("vector.announcement_chunk").indexes
        if item.name == "idx_ann_chunk_embedding"
    )

    assert index.dialect_kwargs["postgresql_using"] == "hnsw"
    assert index.dialect_kwargs["postgresql_ops"] == {"embedding": "vector_cosine_ops"}
    assert index.dialect_kwargs["postgresql_with"] == {"m": 16, "ef_construction": 64}


def test_event_identity_is_unique_across_nulls() -> None:
    constraints = [
        constraint
        for constraint in _table("ops.etl_event_log").constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    identity = next(
        item for item in constraints if item.name == "uq_etl_event_identity"
    )

    assert identity.dialect_kwargs["postgresql_nulls_not_distinct"] is True


def test_compliance_columns_are_nullable_on_governed_tables() -> None:
    for name in COMPLIANCE_TABLES:
        table = _table(name)
        assert table.c.compliance_tier.nullable is True
        assert table.c.commercial_use.nullable is True
        tier_type = table.c.compliance_tier.type
        assert isinstance(tier_type, String)
        assert tier_type.length == 1
        assert isinstance(table.c.commercial_use.type, Boolean)


def test_announcement_chunk_references_announcement_id() -> None:
    foreign_keys = list(_table("vector.announcement_chunk").foreign_keys)

    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert foreign_key.name == "announcement_chunk_dwd_ref_id_fkey"
    assert foreign_key.column.table.fullname == "dwd.stock_announcement"
    assert foreign_key.column.name == "id"
    assert foreign_key.ondelete == "CASCADE"


def test_compiled_types_are_postgres_types() -> None:
    dialect = postgresql.dialect()
    compiled: dict[str, Any] = {
        "embedding": _table("vector.announcement_chunk").c.embedding.type.compile(
            dialect=dialect
        ),
        "checksum": _table("ops.schema_version").c.checksum.type.compile(dialect=dialect),
        "amount": _table("dwd.stock_kline").c.amount.type.compile(dialect=dialect),
    }

    assert compiled == {
        "embedding": "vector(1024)",
        "checksum": "CHAR(64)",
        "amount": "NUMERIC(24, 4)",
    }
