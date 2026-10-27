"""SQLAlchemy 2.0 models mirroring the migration-managed schema."""

from financecoffer.db.models.base import Base
from financecoffer.db.models.dwd import (
    NorthboundFlow,
    StockAnnouncement,
    StockFinancialSnapshot,
    StockKline,
    StockQuoteSnapshot,
    StockResearchReport,
    StockRiskEvent,
)
from financecoffer.db.models.dws import StockAgentAnalysisWide
from financecoffer.db.models.ods import StockRawData
from financecoffer.db.models.ops import (
    ArchiveJob,
    EtlConsumerOffset,
    EtlDlq,
    EtlEventLog,
    SchemaVersion,
    SourceHeartbeat,
)
from financecoffer.db.models.vector import AnnouncementChunk

__all__ = [
    "AnnouncementChunk",
    "ArchiveJob",
    "Base",
    "EtlConsumerOffset",
    "EtlDlq",
    "EtlEventLog",
    "NorthboundFlow",
    "SchemaVersion",
    "SourceHeartbeat",
    "StockAgentAnalysisWide",
    "StockAnnouncement",
    "StockFinancialSnapshot",
    "StockKline",
    "StockQuoteSnapshot",
    "StockRawData",
    "StockResearchReport",
    "StockRiskEvent",
]
