"""Declarative base for the warehouse models.

The SQL migrations are authoritative (DEC-0008); this metadata mirrors them so
the ORM can read and write. No runtime path calls ``metadata.create_all``.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Constraint names here match what PostgreSQL itself would generate for the
# explicit DDL, so the metadata/schema parity tests can compare names one-to-one.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "%(constraint_name)s",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
