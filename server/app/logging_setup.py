"""Application-wide logging bootstrap for the API process.

Uvicorn only configures its own ``uvicorn.*`` loggers, which left every
``app.*`` logger at the root default (WARNING): INFO records such as the
request observability payloads in ``app.ops_metrics`` were silently dropped
and WARNING+ lines reached stderr as bare messages without timestamps. The
API process now calls :func:`configure_logging` once at import time so that
INFO and above reach stderr (journald / docker json-file) in a consistent,
timestamped format.
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_MARKER = "_video_replica_logging_handler"


def configure_logging(*, level: int = logging.INFO) -> None:
    """Configure the root logger once per process; safe to call repeatedly.

    A marker attribute distinguishes our handler from handlers installed by
    tests or embedding hosts, so repeated calls never stack outputs. An
    already-more-verbose root level (e.g. DEBUG during local debugging) is
    preserved.
    """
    root = logging.getLogger()
    handler = next((h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
