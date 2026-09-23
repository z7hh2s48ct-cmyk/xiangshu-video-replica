from __future__ import annotations

import io
import logging

from app.logging_setup import _HANDLER_MARKER, LOG_FORMAT, configure_logging


def _marked_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]


def test_configure_logging_sets_root_info_level() -> None:
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_adds_single_marked_formatted_handler() -> None:
    configure_logging()
    handlers = _marked_handlers()
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.formatter is not None
    assert handler.formatter._fmt == LOG_FORMAT


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    before = len(logging.getLogger().handlers)
    configure_logging()
    assert len(logging.getLogger().handlers) == before
    assert len(_marked_handlers()) == 1


def test_info_records_render_with_timestamp_level_and_logger_name() -> None:
    configure_logging()
    handler = _marked_handlers()[0]
    buffer = io.StringIO()
    original = handler.stream
    handler.stream = buffer
    try:
        logging.getLogger("app.demo").info("observability-smoke")
    finally:
        handler.stream = original
    text = buffer.getvalue()
    assert "observability-smoke" in text
    assert " INFO " in text
    assert "app.demo" in text


def test_app_logger_info_enabled_after_configuration() -> None:
    configure_logging()
    # Root default is WARNING; without the bootstrap every app INFO record is
    # dropped. The assertion pins the behavior that request observability and
    # task-level INFO logs actually pass the level filter.
    assert logging.getLogger("app.analysis").isEnabledFor(logging.INFO)
