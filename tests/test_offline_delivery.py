import asyncio
import json
import socket
import _socket

import pytest

from tools.export_contracts import build_contracts
from tools.offline_check import build_checks, run_checks, find_cached_maven


def test_default_test_session_blocks_real_connections():
    with socket.socket() as client:
        with pytest.raises(RuntimeError, match='offline'):
            client.connect(('127.0.0.1', 8080))


def test_default_test_session_blocks_dns():
    with pytest.raises(RuntimeError, match='offline'):
        socket.getaddrinfo('should-not-resolve.invalid', 443)


@pytest.mark.parametrize('name', ['gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'])
def test_other_dns_entrypoints_are_blocked_without_native_resolution(name):
    function = getattr(socket, name)
    # Safety precondition: never call the native resolver in a failing test.
    assert function is not getattr(_socket, name), 'native DNS entrypoint has no offline guard'
    with pytest.raises(RuntimeError, match='offline'):
        function('should-not-resolve.invalid')


def test_reverse_nameinfo_is_blocked_without_native_resolution():
    assert socket.getnameinfo is not _socket.getnameinfo, 'native reverse resolver has no offline guard'
    with pytest.raises(RuntimeError, match='offline'):
        socket.getnameinfo(('127.0.0.1', 8080), 0)


def test_contracts_export_actual_roles_and_readonly_tools_without_model_key(monkeypatch):
    import app.agent.merchant_agent as module
    monkeypatch.setattr(module.MerchantAgent, '_build_agent', lambda *a, **k: pytest.fail('must not build model'))
    contracts = asyncio.run(build_contracts())
    paths = contracts['openapi']['paths']
    assert '/api/agent/customer/chat' in paths
    assert '/api/agent/merchant/chat' in paths and '/api/agent/chat' in paths
    tools = contracts['tools']
    assert len(tools['merchant']) == 11 and len(tools['customer']) == 6
    assert all(item['read_only'] for group in tools.values() for item in group)
    merchant_names = {i['name'] for i in tools['merchant']}
    customer_names = {i['name'] for i in tools['customer']}
    assert 'request_dish_status_change' in merchant_names
    assert 'get_period_comparison_report' in merchant_names
    assert 'get_my_orders' in customer_names and 'get_order_list' not in customer_names
    assert 'request_dish_status_change' not in customer_names
    assert 'get_period_comparison_report' not in customer_names
    json.dumps(contracts, ensure_ascii=False)


def test_checker_plans_only_allowlisted_offline_commands(tmp_path):
    checks = build_checks(tmp_path, python='python-local', node='node-local', backend_root=None)
    assert [c.name for c in checks] == ['python', 'frontend', 'contracts']
    assert checks[0].args[:4] == ['python-local', '-m', 'pytest', 'tests']
    assert checks[1].args == ['node-local', '--test', 'tests/web/core.test.mjs']
    assert checks[2].args[1] == 'tools/export_contracts.py'
    assert all(c.cwd == tmp_path for c in checks)


def test_java_step_is_explicit_and_offline(tmp_path):
    java = build_checks(tmp_path, python='py', node='node', backend_root=tmp_path / 'backend')[-1]
    assert java.name == 'java-identity'
    assert '-o' in java.args
    assert '-Dtest=CustomerAgentControllerTest,AgentControllerTest' in java.args
    assert 'spring-boot:run' not in java.args


def test_missing_cached_maven_does_not_bootstrap_or_download(tmp_path):
    assert find_cached_maven(tmp_path, tmp_path / 'home') is None
    check = build_checks(tmp_path, 'py', 'node', tmp_path / 'backend', maven=None)[-1]
    assert check.args == []


def test_cached_maven_matches_project_pinned_distribution(tmp_path):
    backend = tmp_path / 'backend'
    config = backend / '.mvn/wrapper/maven-wrapper.properties'
    config.parent.mkdir(parents=True)
    config.write_text('distributionUrl=https://unused.invalid/apache-maven-3.9.9-bin.zip', encoding='utf-8')
    binary = tmp_path / 'home/.m2/wrapper/dists/apache-maven-3.9.9-bin/hash/apache-maven-3.9.9/bin/mvn.cmd'
    binary.parent.mkdir(parents=True)
    binary.write_text('unit-test-placeholder', encoding='utf-8')
    assert find_cached_maven(backend, tmp_path / 'home', windows=True) == str(binary)


def test_neighboring_cached_maven_version_is_not_accepted(tmp_path):
    backend = tmp_path / 'backend'
    config = backend / '.mvn/wrapper/maven-wrapper.properties'
    config.parent.mkdir(parents=True)
    config.write_text('distributionUrl=https://unused.invalid/apache-maven-3.9.9-bin.zip', encoding='utf-8')
    binary = tmp_path / 'home/.m2/wrapper/dists/apache-maven-3.9.99/bin/mvn.cmd'
    binary.parent.mkdir(parents=True)
    binary.write_text('unit-test-placeholder', encoding='utf-8')
    assert find_cached_maven(backend, tmp_path / 'home', windows=True) is None


@pytest.mark.parametrize('mode', ['missing', 'stale', 'skipped', 'failed', 'fresh'])
def test_java_exit_zero_requires_both_fresh_executed_identity_reports(tmp_path, mode):
    import subprocess
    from tools.offline_check import Check
    reports = tmp_path / 'sky-server/target/surefire-reports'
    reports.mkdir(parents=True)
    names = ['com.sky.controller.admin.AgentControllerTest', 'com.sky.controller.user.CustomerAgentControllerTest']
    def write(name):
        skipped = 1 if mode == 'skipped' else 0
        failures = 1 if mode == 'failed' else 0
        (reports / f'TEST-{name}.xml').write_text(f'<testsuite name="{name}" tests="1" skipped="{skipped}" failures="{failures}" errors="0"><testcase name="sample"/></testsuite>', encoding='utf-8')
    if mode == 'stale':
        for name in names:
            write(name)
    def execute(args, **kwargs):
        if mode in ('fresh', 'skipped', 'failed'):
            for name in names:
                write(name)
        elif mode == 'missing':
            write(names[0])
        return subprocess.CompletedProcess(args, returncode=0)
    report = run_checks([Check('java-identity', ['mvn', '-o'], tmp_path)], executor=execute)
    assert report['passed'] is (mode == 'fresh')


def test_checker_failure_and_timeout_are_not_reported_as_pass(tmp_path):
    import subprocess
    checks = build_checks(tmp_path, python='py', node='node', backend_root=None)
    def execute(args, **kwargs):
        if args[0] == 'node':
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, returncode=1 if '-m' in args else 0)
    report = run_checks(checks, executor=execute)
    assert report['passed'] is False
    assert [r['status'] for r in report['checks']] == ['failed', 'timeout', 'passed']
    assert all('stdout' not in result for result in report['checks'])
