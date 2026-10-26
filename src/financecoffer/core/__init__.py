"""Core building blocks: configuration model, errors, logging and envelopes."""

from financecoffer.core.config import Config
from financecoffer.core.envelope import Envelope, ErrorBody, Meta
from financecoffer.core.errors import (
    AuthError,
    ConfigError,
    DependencyError,
    ErrorCode,
    ExitCode,
    FinanceCofferError,
    InternalError,
    InterruptedError_,
    InvalidArgumentError,
    NotImplementedYetError,
    NotFoundError,
    PartialFailureError,
    StorageError,
    exit_code_for,
)
from financecoffer.core.logging import configure_logging, get_logger
from financecoffer.core.trace import new_trace_id, normalize_trace_id

__all__ = [
    "AuthError",
    "Config",
    "ConfigError",
    "DependencyError",
    "Envelope",
    "ErrorBody",
    "ErrorCode",
    "ExitCode",
    "FinanceCofferError",
    "InternalError",
    "InterruptedError_",
    "InvalidArgumentError",
    "Meta",
    "NotImplementedYetError",
    "NotFoundError",
    "PartialFailureError",
    "StorageError",
    "configure_logging",
    "exit_code_for",
    "get_logger",
    "new_trace_id",
    "normalize_trace_id",
]
