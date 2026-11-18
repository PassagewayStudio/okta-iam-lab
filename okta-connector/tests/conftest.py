import logging

import pytest


@pytest.fixture(autouse=True)
def _exercise_all_log_calls(caplog):
    """Run every test with DEBUG logging enabled.

    By default pytest leaves the root logger at WARNING, so `log.info(..., extra={...})` calls
    never build a LogRecord and a bad `extra` key (one that collides with a built-in LogRecord
    attribute such as `created`, `name`, `args`) goes unnoticed until it crashes for real.
    """
    caplog.set_level(logging.DEBUG)
