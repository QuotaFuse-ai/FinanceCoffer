"""Stable error codes, exit-code mapping and the exception hierarchy."""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any, Mapping


class ExitCode(IntEnum):
    """Process exit codes, aligned with ``docs/specs/cli.md`` section 1."""

    OK = 0
    ERROR = 1
    INVALID_ARGUMENT = 2
    CONFIG = 3
    STORAGE = 4
    DEPENDENCY = 5
    AUTH = 6
    PARTIAL_FAILURE = 8
    INTERRUPTED = 130


class ErrorCode(str, Enum):
    """Stable machine-readable error codes.

    Codes are part of the public contract (CLI, REST, MCP) and must never be
    renamed. Later tasks add domain codes here (for example event validation or
    ETL DLQ errors) instead of inventing ad-hoc strings at call sites.
    """

    E_INTERNAL = "E_INTERNAL"
    E_INVALID_ARGUMENT = "E_INVALID_ARGUMENT"
    E_CONFIG = "E_CONFIG"
    E_STORAGE = "E_STORAGE"
    E_DEPENDENCY = "E_DEPENDENCY"
    E_AUTH = "E_AUTH"
    E_PARTIAL_FAILURE = "E_PARTIAL_FAILURE"
    E_NOT_IMPLEMENTED = "E_NOT_IMPLEMENTED"
    E_INTERRUPTED = "E_INTERRUPTED"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_RATE_LIMITED = "E_RATE_LIMITED"


_EXIT_CODES: Mapping[ErrorCode, ExitCode] = {
    ErrorCode.E_INTERNAL: ExitCode.ERROR,
    ErrorCode.E_INVALID_ARGUMENT: ExitCode.INVALID_ARGUMENT,
    ErrorCode.E_CONFIG: ExitCode.CONFIG,
    ErrorCode.E_STORAGE: ExitCode.STORAGE,
    ErrorCode.E_DEPENDENCY: ExitCode.DEPENDENCY,
    ErrorCode.E_AUTH: ExitCode.AUTH,
    ErrorCode.E_PARTIAL_FAILURE: ExitCode.PARTIAL_FAILURE,
    ErrorCode.E_NOT_IMPLEMENTED: ExitCode.ERROR,
    ErrorCode.E_INTERRUPTED: ExitCode.INTERRUPTED,
    ErrorCode.E_NOT_FOUND: ExitCode.ERROR,
    ErrorCode.E_RATE_LIMITED: ExitCode.ERROR,
}


def exit_code_for(code: ErrorCode | str) -> int:
    """Return the process exit code for an error code (unknown codes map to 1)."""

    try:
        return int(_EXIT_CODES[ErrorCode(code)])
    except ValueError:
        return int(ExitCode.ERROR)


class FinanceCofferError(Exception):
    """Base class for every expected FinanceCoffer failure.

    Call sites raise a subclass (or set ``code``) so the CLI/API/MCP layers can
    render a stable envelope without parsing messages.
    """

    code: ErrorCode = ErrorCode.E_INTERNAL

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.message = str(message).strip() or "unexpected error"
        self.details: dict[str, Any] | None = dict(details) if details else None
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)

    def __str__(self) -> str:
        return self.message


class InvalidArgumentError(FinanceCofferError):
    code = ErrorCode.E_INVALID_ARGUMENT


class InternalError(FinanceCofferError):
    code = ErrorCode.E_INTERNAL


class ConfigError(FinanceCofferError):
    code = ErrorCode.E_CONFIG


class StorageError(FinanceCofferError):
    code = ErrorCode.E_STORAGE


class DependencyError(FinanceCofferError):
    code = ErrorCode.E_DEPENDENCY


class AuthError(FinanceCofferError):
    code = ErrorCode.E_AUTH


class PartialFailureError(FinanceCofferError):
    code = ErrorCode.E_PARTIAL_FAILURE


class NotImplementedYetError(FinanceCofferError):
    code = ErrorCode.E_NOT_IMPLEMENTED


class InterruptedError_(FinanceCofferError):
    code = ErrorCode.E_INTERRUPTED


class NotFoundError(FinanceCofferError):
    code = ErrorCode.E_NOT_FOUND


class RateLimitedError(FinanceCofferError):
    code = ErrorCode.E_RATE_LIMITED
