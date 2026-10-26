import logging

import pytest


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
