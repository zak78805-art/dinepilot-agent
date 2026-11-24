"""Synthetic fixtures only: no real credentials or business data in log tests."""
import asyncio
import io
import logging
import sys

import pytest

from app import audit
from app.agent import merchant_agent as runtime


@pytest.fixture
def audit_output(monkeypatch):
    output = io.StringIO()
    logger = logging.Logger('test.audit')
    logger.addHandler(logging.StreamHandler(output))
    monkeypatch.setattr(audit, 'audit_logger', lambda: logger)
    return output


def test_tool_audit_hides_personal_filters_but_keeps_safe_query_context(audit_output):
    arguments = {'phone': '13900009999', 'order_number': 'synthetic-order-123',
                 'begin': '2026-08-01', 'end': '2026-08-07', 'status': 5, 'limit': 10}

    async def tool(**received):
        assert received == arguments  # Redaction must not change tool inputs.
        return {'total': 2}

    assert asyncio.run(runtime._run_tool('get_order_list', tool, **arguments)) == {'total': 2}
    text = audit_output.getvalue()
    assert '13900009999' not in text
    assert 'synthetic-order-123' not in text
    assert '[REDACTED]' in text
    assert '2026-08-01' in text and '2026-08-07' in text
    assert 'get_order_list' in text and 'success=True' in text


def test_tool_audit_drops_unknown_argument_names_and_values(audit_output):
    async def tool(**arguments):
        return 'ok'

    asyncio.run(runtime._run_tool('get_order_list', tool,
                                 **{'synthetic-secret-in-key': {'token': 'synthetic-token-value'},
                                    'begin': '2026-08-01\nforged-log-entry',
                                    'status': 'synthetic-status-secret'}))
    text = audit_output.getvalue()
    for private in ('synthetic-secret-in-key', 'synthetic-token-value',
                    'forged-log-entry', 'synthetic-status-secret'):
        assert private not in text
    assert 'omitted_fields' in text
    assert len(text.splitlines()) == 1


@pytest.mark.parametrize('failed_tool', [False, True])
def test_audit_failure_never_replaces_tool_outcome(monkeypatch, capsys, failed_tool):
    class BrokenLogger:
        def info(self, *args, **kwargs):
            raise OSError('synthetic-private-log-path')

    monkeypatch.setattr(audit, 'audit_logger', lambda: BrokenLogger())
    original_error = ValueError('synthetic-tool-error')

    async def tool():
        if failed_tool:
            raise original_error
        return 'business-result'

    if failed_tool:
        with pytest.raises(ValueError) as error:
            asyncio.run(runtime._run_tool('get_my_orders', tool))
        assert error.value is original_error
    else:
        assert asyncio.run(runtime._run_tool('get_my_orders', tool)) == 'business-result'
    diagnostic = capsys.readouterr().err
    assert 'audit' in diagnostic.lower()
    assert 'synthetic-private-log-path' not in diagnostic
    assert 'synthetic-tool-error' not in diagnostic


def test_failed_customer_tool_is_audited_with_identity_without_result_or_error_text(audit_output):
    async def tool(order_id):
        raise ValueError('synthetic-private-error-detail')

    role = runtime._actor_role.set('customer')
    operator = runtime._operator.set(7)
    try:
        with pytest.raises(ValueError):
            asyncio.run(runtime._run_tool('get_my_order_detail', tool, order_id=12))
    finally:
        runtime._operator.reset(operator)
        runtime._actor_role.reset(role)
    text = audit_output.getvalue()
    assert 'role=customer' in text and 'operator=7' in text
    assert 'get_my_order_detail' in text and 'success=False' in text
    assert 'elapsed_ms=' in text
    assert 'synthetic-private-error-detail' not in text


@pytest.mark.parametrize('name,value', [
    ('begin', '2026-02-30'), ('end', 'synthetic-private-date'),
    ('status', True), ('limit', 10**100), ('item_type', 'synthetic-private-type'),
    ('max_price', float('nan')), ('max_price', float('inf')),
    ('category_id', -1), ('order_id', 'synthetic-private-id'),
])
def test_invalid_safe_field_is_redacted_before_formatting(audit_output, name, value):
    async def tool(**arguments):
        return 'ok'

    asyncio.run(runtime._run_tool('get_customer_menu', tool, **{name: value}))
    text = audit_output.getvalue()
    assert f"'{name}': '[REDACTED]'" in text
    assert 'synthetic-private-' not in text


def test_safe_menu_metadata_remains_available_without_logging_result(audit_output):
    async def tool(**arguments):
        return {'description': 'synthetic-private-result'}

    asyncio.run(runtime._run_tool('get_customer_menu', tool, category_id=3,
                                 item_type='dish', max_price=28.5, limit=20))
    text = audit_output.getvalue()
    assert "'category_id': 3" in text
    assert "'item_type': 'dish'" in text
    assert "'max_price': 28.5" in text
    assert "'limit': 20" in text
    assert 'synthetic-private-result' not in text


def test_audit_never_formats_unknown_objects(audit_output):
    class PrivateObject:
        def __repr__(self):
            raise AssertionError('private objects must not be formatted')

    async def tool(**arguments):
        return 'ok'

    assert asyncio.run(runtime._run_tool('get_order_list', tool,
                                       phone=PrivateObject(), extra=PrivateObject())) == 'ok'
    assert 'success=True' in audit_output.getvalue()


def test_audit_initialization_failure_does_not_change_result(monkeypatch, capsys):
    def unavailable():
        raise PermissionError('synthetic-private-directory')

    monkeypatch.setattr(audit, 'audit_logger', unavailable)

    async def tool():
        return 123

    assert asyncio.run(runtime._run_tool('get_today_sales', tool)) == 123
    diagnostic = capsys.readouterr().err
    assert 'audit unavailable' in diagnostic.lower()
    assert 'synthetic-private-directory' not in diagnostic


def test_real_file_handler_failure_does_not_print_internal_exception(monkeypatch, tmp_path, capsys):
    logger = logging.Logger('test.audit.file')
    get_logger = logging.getLogger
    monkeypatch.setattr(logging, 'getLogger', lambda name=None:
                        logger if name == 'agent.audit' else get_logger(name))
    monkeypatch.setattr(audit, '__file__', str(tmp_path / 'app/audit.py'))
    audit.audit_logger.cache_clear()
    handler = audit.audit_logger().handlers[0]
    handler.stream.close()
    handler.stream = None

    def cannot_open():
        raise PermissionError('synthetic-private-log-filename')

    monkeypatch.setattr(handler, '_open', cannot_open)

    async def tool():
        return 'ok'

    try:
        assert asyncio.run(runtime._run_tool('get_today_sales', tool)) == 'ok'
        diagnostic = capsys.readouterr().err
        assert 'synthetic-private-log-filename' not in diagnostic
        assert 'audit unavailable' in diagnostic.lower()
    finally:
        handler.close()
        audit.audit_logger.cache_clear()


def test_broken_fallback_logging_cannot_disclose_either_exception(monkeypatch, capsys):
    class BrokenAudit:
        def info(self, *args):
            raise PermissionError('synthetic-private-original-path')

    class BrokenStream:
        def write(self, text):
            raise OSError('synthetic-private-fallback-path')

    fallback = logging.Logger('test.broken.fallback')
    fallback.addHandler(logging.StreamHandler(BrokenStream()))
    monkeypatch.setattr(audit, 'audit_logger', lambda: BrokenAudit())
    get_logger = logging.getLogger
    monkeypatch.setattr(logging, 'getLogger', lambda name=None:
                        fallback if name == 'app.audit' else get_logger(name))

    async def tool():
        return 'ok'

    assert asyncio.run(runtime._run_tool('get_today_sales', tool)) == 'ok'
    diagnostic = capsys.readouterr().err
    assert 'synthetic-private-original-path' not in diagnostic
    assert 'synthetic-private-fallback-path' not in diagnostic
    assert 'audit unavailable' in diagnostic.lower()


@pytest.mark.parametrize('failed_tool', [False, True])
def test_unavailable_stderr_never_changes_tool_outcome(monkeypatch, failed_tool):
    class BrokenAudit:
        def info(self, *args):
            raise PermissionError('synthetic-private-audit-error')

    class BrokenStderr:
        def write(self, text):
            raise OSError('synthetic-private-stderr-error')

    monkeypatch.setattr(audit, 'audit_logger', lambda: BrokenAudit())
    original_error = ValueError('synthetic-original-error')

    async def tool():
        if failed_tool:
            raise original_error
        return 'ok'

    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stderr', BrokenStderr())
        if failed_tool:
            with pytest.raises(ValueError) as error:
                asyncio.run(runtime._run_tool('get_today_sales', tool))
            assert error.value is original_error
        else:
            assert asyncio.run(runtime._run_tool('get_today_sales', tool)) == 'ok'
