"""The three query paths over a real PostgreSQL and the local embedding server.

Rows are seeded through the DWD repositories, the DWS builder composes the wide
row, and the vector layer chunks and embeds one announcement. Every assertion
then goes through the REST surface, so the envelope, the SQL and the vector
search are exercised together on real data instead of fakes.

The module skips when PostgreSQL or the embedding server is unreachable, so the
suite stays green on a machine without either dependency. Symbols live in the
``93`` test range and every row is tagged with its own provider, keeping the
shared scratch database isolated from the other integration suites.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from financecoffer.api import create_app
from financecoffer.core.config import Config
from financecoffer.db.models.dwd import (
    StockAnnouncement,
    StockFinancialSnapshot,
    StockQuoteSnapshot,
    StockRiskEvent,
)
from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.models.ops import ArchiveJob, EtlEventLog
from financecoffer.db.repositories.dwd import (
    AnnouncementRepository,
    FinancialSnapshotRepository,
    QuoteSnapshotRepository,
    RiskEventRepository,
)
from financecoffer.db.repositories.ops import ArchiveJobRepository, EventLogRepository
from financecoffer.db.session import Database
from financecoffer.dws import DwsService
from financecoffer.query.service import MAX_RISK_LIMIT, MAX_TOP_K
from financecoffer.vector.service import VectorService

EMBEDDING_URL = os.environ.get("FC_EMBEDDING_URL", "http://127.0.0.1:8001/v1/embeddings")
UNREACHABLE_EMBEDDING_URL = "http://127.0.0.1:9/v1/embeddings"

PROVIDER = "t010-api"
SYMBOL = "930001"
MISSING_SYMBOL = "939999"
TODAY = date.today()
AS_OF = TODAY.isoformat()
FETCH = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)

ANNOUNCEMENT_TITLE = "Annual report on revenue growth and litigation risk"
ANNOUNCEMENT_BODY = (
    "The company reported revenue growth of twelve percent for the period and "
    "discussed litigation risk factors, supply constraints and the outlook. "
) * 60

#: Revenue of the newest fact version; it must survive as an exact string.
LATEST_REVENUE = "3456789.0123"
LATEST_PROFIT = "123456.7890"

#: One exposition sample: ``name{label="value",...} value``.
_LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"'
_SAMPLE = re.compile(rf"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{{{_LABEL}(,{_LABEL})*\}})? [^\s]+$")


@pytest.fixture(scope="module")
def live_embedding() -> str:
    """Skip the module when the model server is unavailable."""

    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                EMBEDDING_URL,
                json={"model": "BAAI/bge-m3", "input": ["ping"]},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"embedding service is not reachable at {EMBEDDING_URL}: {exc}")
    vectors = body.get("data") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or len(vectors) != 1:
        pytest.skip("embedding service returned an unexpected payload")
    return EMBEDDING_URL


def _event_id(kind: str, marker: str) -> str:
    return f"{PROVIDER}:{kind}:{SYMBOL}:{marker}"


def _seed(database: Database) -> int:
    """Write one symbol's facts, bookkeeping rows and announcement."""

    this_year = TODAY.year
    with database.session_scope() as session:
        financial = FinancialSnapshotRepository(session)
        for year, revenue, profit in (
            (this_year - 2, "1000000.1234", "100000.0001"),
            (this_year - 1, "2000000.5678", "200000.0002"),
        ):
            assert (
                financial.insert_version(
                    symbol=SYMBOL,
                    report_date=date(year, 12, 31),
                    report_type="FY",
                    statement="income",
                    amount_revenue=Decimal(revenue),
                    amount_net_profit=Decimal(profit),
                    quality_status="normal",
                    confidence="high",
                    fetch_time=FETCH,
                    event_id=_event_id("fin", f"{year}-FY"),
                    provider=PROVIDER,
                )
                is not None
            )

        # Two versions of the same business key: the wide row keeps the newest.
        report_date = date(this_year, 6, 30)
        for marker, revenue, profit in (
            ("v1", "3000000.0000", "100000.0000"),
            ("v2", LATEST_REVENUE, LATEST_PROFIT),
        ):
            assert (
                financial.insert_version(
                    symbol=SYMBOL,
                    report_date=report_date,
                    report_type="Q2",
                    statement="income",
                    amount_revenue=Decimal(revenue),
                    amount_net_profit=Decimal(profit),
                    quality_status="normal",
                    confidence="high",
                    fetch_time=FETCH,
                    event_id=_event_id("fin", f"{this_year}-Q2-{marker}"),
                    provider=PROVIDER,
                )
                is not None
            )

        assert (
            QuoteSnapshotRepository(session).insert_version(
                symbol=SYMBOL,
                trade_date=TODAY,
                close=Decimal("11.1100"),
                total_market_cap=Decimal("123456789.0100"),
                pe_ttm=Decimal("15.2000"),
                quality_status="normal",
                confidence="high",
                fetch_time=FETCH,
                event_id=_event_id("quote", AS_OF),
                provider=PROVIDER,
            )
            is not None
        )

        risks = RiskEventRepository(session)
        for index, (offset, severity) in enumerate(((0, "high"), (30, "medium"), (60, "low"))):
            occur = TODAY - timedelta(days=offset)
            assert (
                risks.insert_once(
                    symbol=SYMBOL,
                    occur_date=occur,
                    risk_type="litigation",
                    severity=severity,
                    summary=f"{severity} risk disclosed on {occur.isoformat()}",
                    source_announcement_ref="dwd:stock_announcement:0",
                    quality_status="normal",
                    confidence="medium",
                    fetch_time=FETCH,
                    event_id=_event_id("risk", f"{occur.isoformat()}-{index}"),
                    provider=PROVIDER,
                )
                is not None
            )

        announcement = AnnouncementRepository(session).insert_once(
            symbol=SYMBOL,
            ann_date=TODAY,
            ann_type="periodic",
            title=ANNOUNCEMENT_TITLE,
            content_hash=hashlib.sha256(ANNOUNCEMENT_BODY.encode("utf-8")).hexdigest(),
            clean_content=ANNOUNCEMENT_BODY,
            minio_url="s3://financecoffer/ann/t010.pdf",
            quality_status="normal",
            confidence="high",
            fetch_time=FETCH,
            event_id=_event_id("ann", AS_OF),
            provider=PROVIDER,
        )
        assert announcement is not None

        EventLogRepository(session).claim(
            _event_id("evt", AS_OF),
            provider=PROVIDER,
            data_type="financial",
            biz_key=f"{this_year}-06-30|Q2",
            symbol=SYMBOL,
            event_time=FETCH,
            trace_id="trace-t010-api",
        )
        ArchiveJobRepository(session).create(
            table_name="dwd.stock_kline",
            object_key="s3://financecoffer/archive/stock_kline.jsonl.gz",
            symbol=SYMBOL,
        )

        return announcement.id


def _clean(database: Database) -> None:
    with database.session_scope() as session:
        session.execute(
            delete(StockAgentAnalysisWide).where(StockAgentAnalysisWide.symbol == SYMBOL)
        )
        # Deleting the announcement cascades to its vector chunks.
        session.execute(delete(StockAnnouncement).where(StockAnnouncement.symbol == SYMBOL))
        session.execute(
            delete(StockFinancialSnapshot).where(StockFinancialSnapshot.symbol == SYMBOL)
        )
        session.execute(delete(StockQuoteSnapshot).where(StockQuoteSnapshot.symbol == SYMBOL))
        session.execute(delete(StockRiskEvent).where(StockRiskEvent.symbol == SYMBOL))
        session.execute(delete(ArchiveJob).where(ArchiveJob.symbol == SYMBOL))
        session.execute(delete(EtlEventLog).where(EtlEventLog.provider == PROVIDER))


@pytest.fixture(scope="module")
def warehouse(migrated_warehouse: str, live_embedding: str) -> Iterator[dict[str, Any]]:
    """Seed, build DWS, embed the announcement, then hand over the DSN."""

    database = Database(migrated_warehouse)
    config = Config(db_url=migrated_warehouse, embedding_url=live_embedding)
    try:
        announcement_id = _seed(database)

        with DwsService(config=config, dsn=migrated_warehouse) as dws:
            report = dws.build([SYMBOL], as_of=AS_OF, fiscal_years=3)
        assert report.counts["built"] == 1, report.payload()

        with VectorService(config=config, dsn=migrated_warehouse) as vectors:
            embedded = vectors.embed(SYMBOL, pending=True)
        assert embedded.failed == 0, embedded.payload()
        assert embedded.chunks >= 2, embedded.payload()

        yield {
            "dsn": migrated_warehouse,
            "embedding_url": live_embedding,
            "announcement_id": announcement_id,
            "chunks": embedded.chunks,
        }
    finally:
        _clean(database)
        database.dispose()


@pytest.fixture(scope="module")
def client(warehouse: dict[str, Any]) -> TestClient:
    config = Config(
        db_url=warehouse["dsn"],
        embedding_url=warehouse["embedding_url"],
        rate_limit_enabled=False,
    )
    return TestClient(create_app(config=config, dsn=warehouse["dsn"]))


def _down_client(warehouse: dict[str, Any]) -> TestClient:
    """A client whose embedding endpoint is closed, everything else real."""

    config = Config(
        db_url=warehouse["dsn"],
        embedding_url=UNREACHABLE_EMBEDDING_URL,
        rate_limit_enabled=False,
    )
    return TestClient(create_app(config=config, dsn=warehouse["dsn"]))


def _years(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["year"]): item for item in data["years"]}


# -- structured ---------------------------------------------------------------


def test_structured_path_returns_the_latest_version_as_exact_strings(
    client: TestClient, warehouse: dict[str, Any]
) -> None:
    response = client.get(f"/v1/structured/{SYMBOL}", params={"metric": "revenue"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["meta"]["source"] == "dws"

    data = body["data"]
    assert data["symbol"] == SYMBOL
    assert data["metric"] == "revenue"
    assert data["fiscal_years"] == 3

    years = _years(data)
    assert sorted(years) == [str(TODAY.year - 2), str(TODAY.year - 1), str(TODAY.year)]

    latest = years[str(TODAY.year)]
    assert latest["revenue"] == LATEST_REVENUE
    assert latest["net_profit"] == LATEST_PROFIT
    assert isinstance(latest["revenue"], str)
    assert latest["pe"] == 15.2

    previous = years[str(TODAY.year - 1)]
    assert previous["revenue"] == "2000000.5678"

    series = {str(item["year"]): item["value"] for item in data["series"]}
    assert series[str(TODAY.year)] == LATEST_REVENUE
    assert series[str(TODAY.year - 1)] == "2000000.5678"

    # The amount above is the newest fact version, not the first one written.
    config = Config(db_url=warehouse["dsn"])
    with DwsService(config=config, dsn=warehouse["dsn"]) as dws:
        stored = dws.show(SYMBOL)
    stored_years = {str(item["year"]): item for item in stored["metrics_json"]["years"]}
    assert stored_years[str(TODAY.year)]["version"] == 2
    assert stored_years[str(TODAY.year)]["report_date"] == (f"{TODAY.year}-06-30")
    assert stored_years[str(TODAY.year)]["revenue"] == LATEST_REVENUE


def test_structured_path_rejects_an_unknown_symbol_with_the_envelope(
    client: TestClient,
) -> None:
    response = client.get(f"/v1/structured/{MISSING_SYMBOL}")

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "E_NOT_FOUND"


# -- risk ---------------------------------------------------------------------


def test_risk_path_is_newest_first_and_honours_the_limit(client: TestClient) -> None:
    full = client.get(f"/v1/risk/{SYMBOL}", params={"limit": 50})

    assert full.status_code == 200, full.text
    body = full.json()
    assert body["ok"] is True
    assert body["meta"]["source"] == "dwd"

    events = body["data"]["events"]
    assert body["data"]["count"] == 3
    dates = [event["occur_date"] for event in events]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == TODAY.isoformat()
    assert [event["severity"] for event in events] == ["high", "medium", "low"]

    bounded = client.get(f"/v1/risk/{SYMBOL}", params={"limit": 2})
    assert bounded.status_code == 200, bounded.text
    bounded_body = bounded.json()
    assert bounded_body["data"]["count"] == 2
    assert [event["occur_date"] for event in bounded_body["data"]["events"]] == dates[:2]


def test_risk_path_rejects_a_limit_above_the_cap(client: TestClient) -> None:
    response = client.get(f"/v1/risk/{SYMBOL}", params={"limit": MAX_RISK_LIMIT + 1})

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_INVALID_ARGUMENT"
    assert body["error"]["details"]["errors"][0]["field"] == "limit"


# -- announcements ------------------------------------------------------------


def test_search_path_returns_chunks_traceable_to_the_announcement(
    client: TestClient, warehouse: dict[str, Any]
) -> None:
    response = client.post(
        "/v1/search/announcements",
        json={
            "symbol": SYMBOL,
            "query_text": "litigation risk and revenue growth",
            "top_k": 2,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["meta"]["source"] == "vector"

    data = body["data"]
    assert data["symbol"] == SYMBOL
    assert data["top_k"] == 2
    results = data["results"]
    assert 1 <= len(results) <= 2
    assert data["count"] == len(results)

    # Ranked by similarity, so the chunk order is not the document order.
    indexes = [item["chunk_index"] for item in results]
    assert len(set(indexes)) == len(indexes)
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)

    for item in results:
        assert item["dwd_ref_id"] == warehouse["announcement_id"]
        assert item["symbol"] == SYMBOL
        assert item["ann_date"] == TODAY.isoformat()
        assert item["title"] == ANNOUNCEMENT_TITLE
        assert item["chunk_text"]
        assert -1.0 <= item["score"] <= 1.0

    # The returned reference resolves to the DWD row it came from.
    with Database(warehouse["dsn"]).session() as session:
        traced = session.scalars(
            select(StockAnnouncement).where(StockAnnouncement.id == results[0]["dwd_ref_id"])
        ).one()
        assert traced.title == ANNOUNCEMENT_TITLE
        assert traced.symbol == SYMBOL

    single = client.post(
        "/v1/search/announcements",
        json={"symbol": SYMBOL, "query_text": "risk factors", "top_k": 1},
    )
    assert single.status_code == 200, single.text
    assert single.json()["data"]["count"] == 1


def test_search_without_a_symbol_is_rejected_before_any_work(
    warehouse: dict[str, Any],
) -> None:
    # The embedding endpoint is closed: a 400 here proves validation ran first.
    down = _down_client(warehouse)

    missing = down.post("/v1/search/announcements", json={"query_text": "risk"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert missing.json()["error"]["details"]["errors"][0]["field"] == "symbol"

    over_k = down.post(
        "/v1/search/announcements",
        json={"symbol": SYMBOL, "query_text": "risk", "top_k": MAX_TOP_K + 1},
    )
    assert over_k.status_code == 400
    assert over_k.json()["error"]["code"] == "E_INVALID_ARGUMENT"
    assert over_k.json()["error"]["details"]["errors"][0]["field"] == "top_k"

    extra = down.post(
        "/v1/search/announcements",
        json={"symbol": SYMBOL, "query_text": "risk", "unknown": True},
    )
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "E_INVALID_ARGUMENT"

    blank = down.post("/v1/search/announcements", json={"symbol": SYMBOL, "query_text": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "E_INVALID_ARGUMENT"


def test_an_invalid_symbol_never_reaches_the_warehouse(client: TestClient) -> None:
    response = client.get("/v1/structured/12ab")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "E_INVALID_ARGUMENT"
    assert body["error"]["details"]["field"] == "symbol"

    search = client.post(
        "/v1/search/announcements",
        json={"symbol": "12ab", "query_text": "risk"},
    )
    assert search.status_code == 400
    assert search.json()["error"]["details"]["field"] == "symbol"


# -- health and metrics -------------------------------------------------------


def test_health_reports_each_component(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "ok"
    checks = body["data"]["checks"]
    assert checks["database"] == "ok"
    assert checks["embedding"] == "ok"
    # Redis is reported for visibility and does not take part in the aggregate
    # status yet, so its value is asserted by shape only.
    assert set(checks["redis"]) == {"status", "elapsed_ms"}
    # The etl probe shares its definition with fc_etl_consume_lag_seconds and,
    # like redis, is reported for visibility only.
    assert set(checks["etl"]) == {"status", "lag_seconds", "pending"}


def test_health_degrades_without_crashing_when_embedding_is_down(
    warehouse: dict[str, Any],
) -> None:
    down = _down_client(warehouse)

    response = down.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"]["database"] == "ok"
    assert body["data"]["checks"]["embedding"] == "unreachable"

    # The process keeps serving reads that do not need the model.
    assert down.get(f"/v1/structured/{SYMBOL}").status_code == 200
    assert down.get(f"/v1/risk/{SYMBOL}").status_code == 200


def test_metrics_expose_the_specified_families(client: TestClient) -> None:
    searched = client.post(
        "/v1/search/announcements",
        json={"symbol": SYMBOL, "query_text": "revenue growth", "top_k": 1},
    )
    assert searched.status_code == 200, searched.text

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text

    assert "# TYPE fc_api_requests_total counter" in text
    assert "# TYPE fc_vector_search_seconds histogram" in text
    assert "fc_vector_search_seconds_count" in text
    assert "# TYPE fc_missing_chunk_total counter" in text

    # Families sampled from the seeded ops rows, with the specification labels.
    assert 'fc_etl_events_total{status="received"}' in text
    assert 'fc_archive_job_total{status="pending"}' in text
    assert "# TYPE fc_etl_consume_lag_seconds gauge" in text

    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert _SAMPLE.match(line), line
