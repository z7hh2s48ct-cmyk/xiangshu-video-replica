"""Structured logging configuration with request context propagation.

This module implements structured logging with request ID, user context,
and JSON output format for Loki integration.

Version: V1.0 (2026-09-20)
Author: AI Agent (Qoder)
Status: Ready for Development
"""

import structlog
from datetime import datetime


def configure_logging(level="INFO", format_type="json"):
    """Configure structured logging with context extraction.
    
    Args:
        level: Log level (DEBUG/INFO/WARN/ERROR)
        format_type: Output format ('json' or 'console')
        
    Usage:
        configure_logging(level="INFO")
        
        log = structlog.get_logger()
        log.info("message", key="value")
    """
    
    common_processors = [
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        add_request_context,
        add_user_context,
        timestamp_processor,
    ]
    
    if format_type == "json":
        structlog.configure(
            processors=[*common_processors, structlog.processors.JSONRenderer()],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[*common_processors, structlog.dev.ConsoleFormatter()],
            wrapper_class=structlog.make_filtering_bound_logger(level),
        )


def add_request_context(event_dict):
    """Extract request_id from ASGI scope if present."""
    try:
        from starlette.requests import Request
        from structlog.threadlocal import get
        
        # Try to get from ASGI scope
        scope = getattr(get(), 'scope', None)
        if scope and hasattr(scope, 'request_id'):
            event_dict['request_id'] = str(scope.request_id)
    except Exception:
        pass
    
    return event_dict


def add_user_context(event_dict):
    """Add current user_id if authenticated."""
    try:
        # Try to get current user from session/context
        from app.auth import get_current_user
        
        user = get_current_user()
        if user and hasattr(user, 'id'):
            event_dict['user_id'] = str(user.id)
    except Exception:
        pass  # No user session, skip adding user_id
    
    return event_dict


def timestamp_processor(event_dict, logger_name, event_time):
    """Add ISO-formatted timestamp."""
    if isinstance(event_time, (int, float)):
        event_dict['timestamp'] = datetime.utcfromtimestamp(event_time).isoformat() + 'Z'
    elif isinstance(event_time, datetime):
        event_dict['timestamp'] = event_time.isoformat() + 'Z'
    else:
        event_dict['timestamp'] = str(event_time)
    
    return event_dict


class RequestContext:
    """Context manager for request lifecycle."""
    
    def __init__(self, request_id: str, user_id: str = None):
        self.request_id = request_id
        self.user_id = user_id
        self.logger = structlog.get_logger()
    
    def start(self):
        """Start request context."""
        self.logger = self.logger.bind(
            request_id=self.request_id,
            user_id=self.user_id,
        )
    
    def info(self, message, **kwargs):
        """Log info with context."""
        self.logger.info(message, **kwargs)
    
    def error(self, message, **kwargs):
        """Log error with context."""
        self.logger.error(message, **kwargs)
    
    def cleanup(self):
        """Clean up context."""
        self.logger = structlog.reset_defaults()


# Convenience function for quick usage
def log_info(message, **kwargs):
    """Quick info logging with context."""
    log = structlog.get_logger()
    log.info(message, **kwargs)


def log_error(message, **kwargs):
    """Quick error logging with context."""
    log = structlog.get_logger()
    log.error(message, **kwargs)
