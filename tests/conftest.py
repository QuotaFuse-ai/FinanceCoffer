import logging

import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real ``.env`` out of the test suite.

    CLI commands read the repo ``.env`` when one exists (README precedence), so a
    configured checkout would otherwise turn "unconfigured" assertions green and
    make the suite pass or fail depending on whose machine it runs on. Tests that
    exercise the discovery itself stub ``default_env_file`` explicitly.
    """

    from financecoffer.config import loader

    monkeypatch.setattr(loader, "default_env_file", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def _restore_logging() -> None:
    """Keep CLI/logging tests from leaking handlers into other tests."""

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
