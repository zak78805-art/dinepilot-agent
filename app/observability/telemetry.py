"""Append-only, PII-free, credential-free JSONL telemetry for Agent turns and reviews."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

from app.observability.context import get_current_trace_id

_BEIJING_TZ = timezone(timedelta(hours=8))
_DEFAULT_TELEMETRY_PATH = Path(__file__).resolve().parents[2] / 'logs' / 'telemetry.jsonl'
_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_]{1,64}$')


def _safe_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if _SAFE_ID_RE.fullmatch(cleaned) else '[INVALID_ID]'


def _safe_names(items: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for item in items[:32]:
        if isinstance(item, str) and _SAFE_NAME_RE.fullmatch(item):
            out.append(item)
    return out


def extract_reply_token_usage(message: Any) -> tuple[int | None, int | None, int | None]:
    """Extract `(input_tokens, output_tokens, total_tokens)` from an AgentScope Msg if present."""
    usage = getattr(message, 'usage', None)
    if usage is None:
        return None, None, None
    inp = getattr(usage, 'input_tokens', None)
    out = getattr(usage, 'output_tokens', None)
    in_tok = int(inp) if type(inp) is int and inp >= 0 else None
    out_tok = int(out) if type(out) is int and out >= 0 else None
    if in_tok is None and out_tok is None:
        return None, None, None
    total = (in_tok or 0) + (out_tok or 0)
    return in_tok, out_tok, total


def _append_jsonl(entry: dict[str, Any], log_path: Path | None = None) -> bool:
    target = log_path or _DEFAULT_TELEMETRY_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return True
    except Exception:
        try:
            sys.stderr.write('Telemetry write unavailable; record not confirmed\n')
        except Exception:
            pass
        return False


def record_chat_turn_telemetry(
    *,
    agent_role: str,
    operator_id: int,
    session_id: str,
    stream: bool,
    succeeded: bool,
    ttft_ms: float | None,
    total_ms: float,
    tool_calls: list[str],
    compacted: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    approval_created: bool = False,
    review_submitted: bool = False,
    trace_id: str | None = None,
    log_path: Path | None = None,
) -> bool:
    """Write a strictly de-identified per-turn telemetry record."""
    resolved_trace = _safe_id(trace_id or get_current_trace_id())
    safe_tools = _safe_names(tool_calls)
    entry = {
        'timestamp': datetime.now(_BEIJING_TZ).isoformat(timespec='milliseconds'),
        'event': 'chat_turn',
        'trace_id': resolved_trace,
        'agent_role': 'customer' if agent_role == 'customer' else 'merchant',
        'operator_id': int(operator_id) if type(operator_id) is int else None,
        'session_id': _safe_id(session_id),
        'stream': bool(stream),
        'succeeded': bool(succeeded),
        'ttft_ms': round(float(ttft_ms), 1) if isinstance(ttft_ms, (int, float)) else None,
        'total_ms': round(float(total_ms), 1),
        'tool_calls': safe_tools,
        'tool_count': len(safe_tools),
        'compacted': bool(compacted),
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': total_tokens,
        'approval_created': bool(approval_created),
        'review_submitted': bool(review_submitted),
    }
    return _append_jsonl(entry, log_path=log_path)


def record_review_telemetry(
    *,
    operator_id: int,
    session_id: str,
    review_id: str,
    verdict: str,
    precheck_hit: bool,
    issue_codes: list[str] | tuple[str, ...],
    failure: str | None,
    elapsed_ms: float,
    trace_id: str | None = None,
    log_path: Path | None = None,
) -> bool:
    """Write a strictly de-identified async report review completion record."""
    entry = {
        'timestamp': datetime.now(_BEIJING_TZ).isoformat(timespec='milliseconds'),
        'event': 'report_review_completed',
        'trace_id': _safe_id(trace_id or get_current_trace_id()),
        'operator_id': int(operator_id) if type(operator_id) is int else None,
        'session_id': _safe_id(session_id),
        'review_id': _safe_id(review_id),
        'verdict': verdict if verdict in ('pass', 'revise', 'unavailable') else 'unavailable',
        'precheck_hit': bool(precheck_hit),
        'issue_codes': _safe_names(issue_codes),
        'failure': _safe_id(failure) if failure else None,
        'elapsed_ms': round(float(elapsed_ms), 1),
    }
    return _append_jsonl(entry, log_path=log_path)
