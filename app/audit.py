import logging
import math
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from functools import lru_cache
from pathlib import Path

from app.observability.context import get_current_trace_id


_REDACTED = '[REDACTED]'
_PRIVATE_FIELDS = {'phone', 'number', 'order_number', 'token', 'authentication',
                   'authorization', 'api_key', 'password'}
_QUERY_FIELDS = {'begin', 'end', 'status', 'limit', 'item_type', 'max_price',
                 'category_id', 'setmeal_id', 'order_id', 'dish_id', 'target_status'}


class _AuditFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # The standard handler prints exception details to stderr in debug mode.
        # Let record_tool_call emit only its fixed, non-sensitive warning instead.
        raise


def _safe_query_value(name, value):
    """Only typed, bounded query metadata is suitable for an audit record."""
    if value is None:
        return None
    if name in ('begin', 'end') and type(value) is str and len(value) == 10:
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    if name == 'item_type' and type(value) is str and value in ('dish', 'setmeal'):
        return value
    if type(value) is int:
        if name == 'status' and 0 <= value <= 6:
            return value
        if name == 'target_status' and value in (0, 1):
            return value
        if name == 'limit' and 1 <= value <= 100:
            return value
        if name in ('category_id', 'setmeal_id', 'order_id', 'dish_id') and 1 <= value <= 9223372036854775807:
            return value
    if (name == 'max_price' and type(value) in (int, float)
            and 0 <= value <= 1000000 and math.isfinite(value)):
        return value
    return _REDACTED


def _safe_arguments(arguments):
    # Do not stringify unknown names/objects: either could contain private text.
    safe = {}
    omitted = 0
    for name, value in arguments.items():
        if name in _PRIVATE_FIELDS:
            safe[name] = _REDACTED
        elif name in _QUERY_FIELDS:
            safe[name] = _safe_query_value(name, value)
        else:
            omitted += 1
    if omitted:
        safe['omitted_fields'] = omitted
    return safe


def record_tool_call(*, role, operator_id, tool_name, arguments, succeeded, elapsed_ms):
    """Best-effort audit; never replace the original tool result or exception."""
    try:
        trace_id = get_current_trace_id()
        if trace_id:
            audit_logger().info(
                'trace_id=%s role=%s operator=%s tool=%s arguments=%s success=%s elapsed_ms=%.1f',
                trace_id, role, operator_id, tool_name, _safe_arguments(arguments), succeeded, elapsed_ms)
        else:
            audit_logger().info(
                'role=%s operator=%s tool=%s arguments=%s success=%s elapsed_ms=%.1f',
                role, operator_id, tool_name, _safe_arguments(arguments), succeeded, elapsed_ms)
        return True
    except Exception:
        # Do not call logging here: a broken fallback handler can print the
        # active private exception through logging.handleError itself.
        try:
            sys.stderr.write('Tool audit unavailable; record not confirmed\n')
        except Exception:
            pass
        return False


@lru_cache
def audit_logger():
    logger = logging.getLogger('agent.audit')
    directory = Path(__file__).resolve().parent.parent / 'logs'
    directory.mkdir(exist_ok=True)
    handler = _AuditFileHandler(directory / 'tool_calls.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
