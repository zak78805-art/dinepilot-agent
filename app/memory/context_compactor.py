"""Sliding-window + structured summary compaction for AgentScope 2 AgentState.

Prevents quadratic token growth, context-window overflow, and stale-number pollution
in long multi-turn merchant/customer sessions while preserving turn boundaries.
"""
from collections.abc import Awaitable, Callable
import json
import re
from typing import Any

_SUMMARY_OPEN = '<conversation-summary>'
_SUMMARY_CLOSE = '</conversation-summary>'
_SUMMARY_HEADER = (
    '以下为本会话早期轮次的结构化摘要（仅用于多轮指代与话题延续；'
    '涉及营业额、订单、库存或菜品状态等实时数据时，必须重新调用本轮工具核实，'
    '严禁直接把历史摘要中的旧数值当作当前事实）：'
)
_DATE_CONTEXT_PREFIX_RE = re.compile(r'^当前北京时间日期：\d{4}-\d{2}-\d{2}[^\n]*\n')

SummarizerFn = Callable[[str, list[Any]], Awaitable[str]]


def estimate_message_chars(msg: Any) -> int:
    """Estimate serialized payload size of an AgentScope Msg across text and tool blocks."""
    content = getattr(msg, 'content', None)
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        btype = getattr(block, 'type', '')
        if btype == 'text' or hasattr(block, 'text'):
            total += len(getattr(block, 'text', '') or '')
        elif btype == 'tool_call':
            total += len(getattr(block, 'name', '') or '')
            raw_in = getattr(block, 'input', None)
            if isinstance(raw_in, str):
                total += len(raw_in)
            elif raw_in is not None:
                total += len(json.dumps(raw_in, ensure_ascii=False, default=str))
        elif btype == 'tool_result':
            total += len(getattr(block, 'name', '') or '')
            raw_out = getattr(block, 'output', None)
            if isinstance(raw_out, str):
                total += len(raw_out)
            elif isinstance(raw_out, list):
                for sub in raw_out:
                    total += len(getattr(sub, 'text', '') or str(sub))
            elif raw_out is not None:
                total += len(json.dumps(raw_out, ensure_ascii=False, default=str))
    return total


def _clean_one_line(text: str, limit: int) -> str:
    stripped = _DATE_CONTEXT_PREFIX_RE.sub('', text or '')
    compact = ' '.join(stripped.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)] + '…'


def _extract_msg_text(msg: Any) -> str:
    content = getattr(msg, 'content', None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [getattr(b, 'text', '') for b in content if getattr(b, 'text', None)]
        return ' '.join(parts)
    return ''


def _extract_tool_names(msg: Any) -> list[str]:
    content = getattr(msg, 'content', None)
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if getattr(block, 'type', '') in ('tool_call', 'tool_result'):
            name = getattr(block, 'name', '')
            if name and name not in names:
                names.append(name)
    return names


def _unwrap_prior_bullets(raw_summary: Any) -> list[str]:
    if not isinstance(raw_summary, str) or not raw_summary.strip():
        return []
    body = raw_summary.strip()
    if body.startswith(_SUMMARY_OPEN) and body.endswith(_SUMMARY_CLOSE):
        body = body[len(_SUMMARY_OPEN):-len(_SUMMARY_CLOSE)].strip()
    lines: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s == _SUMMARY_HEADER:
            continue
        lines.append(s if s.startswith('- ') else f'- {s}')
    return lines


def format_Turns_to_bullets(messages: list[Any]) -> list[str]:
    """Summarize evicted messages into compact `- 用户：...｜工具：...｜回复：...` bullets."""
    bullets: list[str] = []
    idx = 0
    n = len(messages)
    while idx < n:
        msg = messages[idx]
        role = getattr(msg, 'role', '')
        if role == 'user':
            user_text = _clean_one_line(_extract_msg_text(msg), 80)
            tools: list[str] = []
            reply_text = ''
            idx += 1
            while idx < n and getattr(messages[idx], 'role', '') != 'user':
                amsg = messages[idx]
                for tname in _extract_tool_names(amsg):
                    if tname not in tools:
                        tools.append(tname)
                atext = _extract_msg_text(amsg)
                if atext.strip():
                    reply_text = _clean_one_line(atext, 100)
                idx += 1
            parts = [f'用户：{user_text or "（空）"}']
            if tools:
                parts.append(f'工具：{",".join(tools[:5])}')
            if reply_text:
                parts.append(f'结论：{reply_text}')
            bullets.append('- ' + '｜'.join(parts))
        else:
            other_text = _clean_one_line(_extract_msg_text(msg), 100)
            tools = _extract_tool_names(msg)
            if other_text or tools:
                parts = []
                if tools:
                    parts.append(f'工具：{",".join(tools[:5])}')
                if other_text:
                    parts.append(f'结论：{other_text}')
                bullets.append('- ' + '｜'.join(parts))
            idx += 1
    return bullets


def build_bounded_summary(prior_summary: Any, older_messages: list[Any], *, max_chars: int = 1600) -> str:
    """Combine prior summary bullets with newly evicted turns and bound total character length."""
    all_bullets = _unwrap_prior_bullets(prior_summary) + format_Turns_to_bullets(older_messages)
    if not all_bullets:
        return ''
    wrapper_overhead = len(_SUMMARY_OPEN) + len(_SUMMARY_CLOSE) + len(_SUMMARY_HEADER) + 6
    budget = max(120, max_chars - wrapper_overhead)

    kept_reversed: list[str] = []
    used = 0
    for bullet in reversed(all_bullets):
        clipped = bullet[: max(40, min(len(bullet), budget))]
        added = len(clipped) + (1 if kept_reversed else 0)
        if used + added > budget:
            if not kept_reversed:
                kept_reversed.append(clipped[: max(20, budget - 1)] + '…')
            break
        kept_reversed.append(clipped)
        used += added

    kept_bullets = list(reversed(kept_reversed))
    return f'{_SUMMARY_OPEN}\n{_SUMMARY_HEADER}\n' + '\n'.join(kept_bullets) + f'\n{_SUMMARY_CLOSE}'


def _find_clean_cut_index(context: list[Any], keep_recent_messages: int) -> int:
    """Find a split index > 0 such that `context[cut]` starts on a `user` turn boundary."""
    target = max(1, len(context) - keep_recent_messages)
    # Prefer moving backward slightly so we keep at least `keep_recent_messages` and start at a user msg.
    for idx in range(target, 0, -1):
        if getattr(context[idx], 'role', '') == 'user':
            return idx
    # Otherwise search forward, leaving at least the newest user turn intact.
    for idx in range(target + 1, len(context)):
        if getattr(context[idx], 'role', '') == 'user':
            return idx
    return 0


async def compact_agent_context_if_needed(
    agent: Any,
    *,
    max_messages: int = 12,
    keep_recent_messages: int = 6,
    max_chars: int = 12000,
    summary_max_chars: int = 1600,
    summarizer: SummarizerFn | None = None,
) -> bool:
    """Compact `agent.state` in-place when message count or char budget exceeds thresholds.

    Returns True if compaction occurred, False otherwise.
    """
    state = getattr(agent, 'state', None)
    context = getattr(state, 'context', None)
    if state is None or not isinstance(context, list) or len(context) <= keep_recent_messages:
        return False

    total_chars = sum(estimate_message_chars(m) for m in context)
    if len(context) <= max_messages and total_chars <= max_chars:
        return False

    cut = _find_clean_cut_index(context, keep_recent_messages)
    if cut <= 0 or cut >= len(context):
        return False

    older = list(context[:cut])
    recent = list(context[cut:])
    prior_summary = getattr(state, 'summary', '') or ''

    if summarizer is not None:
        raw = await summarizer(str(prior_summary), older)
        new_summary = raw[:summary_max_chars]
    else:
        new_summary = build_bounded_summary(prior_summary, older, max_chars=summary_max_chars)

    state.summary = new_summary
    state.context = recent
    return True
