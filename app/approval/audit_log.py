"""Append-only structured JSONL audit log for guarded write-approval decisions."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from app.observability.context import get_current_trace_id


_BEIJING_TZ = timezone(timedelta(hours=8))
_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / 'logs' / 'approval_audit.jsonl'


def _safe_reason(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = ' '.join(value.split())
    return cleaned[:200] if cleaned else None


def record_approval_event(
    *,
    event: str,
    approval_id: str,
    operator_id: int,
    session_id: str,
    action: str,
    dish_id: int,
    before_status: int,
    after_status: int,
    outcome: str,
    reason: str | None = None,
    elapsed_ms: float | None = None,
    trace_id: str | None = None,
    log_path: Path | None = None,
) -> bool:
    """Persist structured, token-free approval audit record (append-only JSONL)."""
    entry = {
        'timestamp': datetime.now(_BEIJING_TZ).isoformat(timespec='milliseconds'),
        'trace_id': trace_id or get_current_trace_id(),
        'event': event,
        'approval_id': approval_id,
        'operator_id': operator_id,
        'session_id': session_id,
        'action': action,
        'dish_id': dish_id,
        'before_status': before_status,
        'after_status': after_status,
        'outcome': outcome,
        'reason': _safe_reason(reason),
        'elapsed_ms': round(elapsed_ms, 1) if elapsed_ms is not None else None,
    }
    target = log_path or _DEFAULT_LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return True
    except Exception:
        try:
            sys.stderr.write('Approval audit log write failed; record not confirmed\n')
        except Exception:
            pass
        return False
