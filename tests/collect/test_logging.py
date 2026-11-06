import io
import logging
from collections.abc import Callable, Iterator

import pytest

from rebalance.collect import Settings, configure_logging

KEY = "TESTKEY123"


@pytest.fixture
def logging_setup(
    make_settings: Callable[..., Settings],
) -> Iterator[tuple[logging.Logger, io.StringIO, Settings]]:
    settings = make_settings(api_key=KEY)
    stream = io.StringIO()
    logger = configure_logging(settings, stream=stream)
    yield logger, stream, settings
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_logging_redacts_key_in_message(
    logging_setup: tuple[logging.Logger, io.StringIO, Settings],
) -> None:
    logger, stream, settings = logging_setup

    logger.info("request to https://x/?app_key=%s failed", KEY)

    assert "failed" in stream.getvalue()
    assert KEY not in stream.getvalue()
    assert KEY not in settings.log_path.read_text(encoding="utf-8")


def test_logging_redacts_key_in_traceback(
    logging_setup: tuple[logging.Logger, io.StringIO, Settings],
) -> None:
    logger, stream, settings = logging_setup

    try:
        raise RuntimeError(f"boom app_key={KEY}")
    except RuntimeError:
        logger.exception("poll failed")

    assert "Traceback" in stream.getvalue()  # a traceback really was formatted
    assert KEY not in stream.getvalue()
    assert KEY not in settings.log_path.read_text(encoding="utf-8")


def test_configure_logging_twice_does_not_duplicate_lines(
    logging_setup: tuple[logging.Logger, io.StringIO, Settings],
) -> None:
    logger, stream, settings = logging_setup
    configure_logging(settings, stream=stream)  # second call

    logger.info("only once")

    assert stream.getvalue().count("only once") == 1
