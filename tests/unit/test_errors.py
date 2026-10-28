import pytest

from financecoffer.core.errors import (
    AuthError,
    ConfigError,
    DependencyError,
    ErrorCode,
    ExitCode,
    FinanceCofferError,
    InternalError,
    InvalidArgumentError,
    MethodNotAllowedError,
    NotFoundError,
    NotImplementedYetError,
    PartialFailureError,
    RateLimitedError,
    StorageError,
    exit_code_for,
)


def test_error_codes_are_stable() -> None:
    assert {code.value for code in ErrorCode} == {
        "E_INTERNAL",
        "E_INVALID_ARGUMENT",
        "E_CONFIG",
        "E_STORAGE",
        "E_DEPENDENCY",
        "E_AUTH",
        "E_PARTIAL_FAILURE",
        "E_NOT_FOUND",
        "E_METHOD_NOT_ALLOWED",
        "E_NOT_IMPLEMENTED",
        "E_INTERRUPTED",
        "E_RATE_LIMITED",
    }


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (ErrorCode.E_INTERNAL, 1),
        (ErrorCode.E_INVALID_ARGUMENT, 2),
        (ErrorCode.E_CONFIG, 3),
        (ErrorCode.E_STORAGE, 4),
        (ErrorCode.E_DEPENDENCY, 5),
        (ErrorCode.E_AUTH, 6),
        (ErrorCode.E_PARTIAL_FAILURE, 8),
        (ErrorCode.E_NOT_FOUND, 1),
        (ErrorCode.E_METHOD_NOT_ALLOWED, 1),
        (ErrorCode.E_NOT_IMPLEMENTED, 1),
        (ErrorCode.E_INTERRUPTED, 130),
        (ErrorCode.E_RATE_LIMITED, 1),
    ],
)
def test_error_code_maps_to_cli_exit_code(error_code: ErrorCode, expected: int) -> None:
    assert exit_code_for(error_code) == expected
    assert exit_code_for(error_code.value) == expected


def test_unknown_error_code_falls_back_to_general_error() -> None:
    assert exit_code_for("E_SOMETHING_ELSE") == int(ExitCode.ERROR)


@pytest.mark.parametrize(
    ("exception_class", "code"),
    [
        (InternalError, ErrorCode.E_INTERNAL),
        (InvalidArgumentError, ErrorCode.E_INVALID_ARGUMENT),
        (ConfigError, ErrorCode.E_CONFIG),
        (StorageError, ErrorCode.E_STORAGE),
        (DependencyError, ErrorCode.E_DEPENDENCY),
        (AuthError, ErrorCode.E_AUTH),
        (PartialFailureError, ErrorCode.E_PARTIAL_FAILURE),
        (NotImplementedYetError, ErrorCode.E_NOT_IMPLEMENTED),
        (NotFoundError, ErrorCode.E_NOT_FOUND),
        (MethodNotAllowedError, ErrorCode.E_METHOD_NOT_ALLOWED),
        (RateLimitedError, ErrorCode.E_RATE_LIMITED),
    ],
)
def test_exception_subclasses_expose_their_code(
    exception_class: type[FinanceCofferError], code: ErrorCode
) -> None:
    error = exception_class("boom", details={"k": "v"})

    assert error.code is code
    assert error.exit_code == exit_code_for(code)
    assert str(error) == "boom"
    assert error.details == {"k": "v"}


def test_blank_message_gets_a_fallback() -> None:
    assert str(InternalError("   ")) == "unexpected error"
