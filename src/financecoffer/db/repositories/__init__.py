"""Repositories for the core tables.

Each repository takes a :class:`sqlalchemy.orm.Session` and only flushes;
commit and rollback stay in the service layer.
"""

from financecoffer.db.repositories.base import BaseRepository
from financecoffer.db.repositories.dwd import (
    AnnouncementRepository,
    FinancialSnapshotRepository,
    KlineRepository,
    NorthboundFlowRepository,
    QuoteSnapshotRepository,
    ResearchReportRepository,
    RiskEventRepository,
    VersionedFactRepository,
)
from financecoffer.db.repositories.dws import AgentAnalysisWideRepository
from financecoffer.db.repositories.ods import RawDataRepository
from financecoffer.db.repositories.ops import (
    ArchiveJobRepository,
    ConsumerOffsetRepository,
    DlqRepository,
    EventLogRepository,
    HeartbeatRepository,
)
from financecoffer.db.repositories.vector import AnnouncementChunkRepository

__all__ = [
    "AgentAnalysisWideRepository",
    "AnnouncementChunkRepository",
    "AnnouncementRepository",
    "ArchiveJobRepository",
    "BaseRepository",
    "ConsumerOffsetRepository",
    "DlqRepository",
    "EventLogRepository",
    "FinancialSnapshotRepository",
    "HeartbeatRepository",
    "KlineRepository",
    "NorthboundFlowRepository",
    "QuoteSnapshotRepository",
    "RawDataRepository",
    "ResearchReportRepository",
    "RiskEventRepository",
    "VersionedFactRepository",
]
