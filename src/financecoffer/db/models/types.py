"""Column types that SQLAlchemy does not ship (pgvector ``vector(n)``)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """Fixed-width pgvector column stored as the text representation ``[a,b]``.

    Keeping the wire format local avoids making ``pgvector`` a hard dependency
    of the ``db`` extra; the Python adapter is not required to read or write.
    """

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        if dimensions <= 0:
            raise ValueError("vector dimensions must be positive")
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Sequence[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(str(float(item)) for item in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: object) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, str):
                body = value.strip().strip("[]")
                return [float(item) for item in body.split(",") if item]
            return [float(item) for item in cast(Sequence[float], value)]

        return process
