"""Run fixed offline checks. Never launches business servers or calls LLMs."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    name: str
    args: list[str]
    cwd: Path


def find_cached_maven(backend_root: Path, home: Path, windows: bool | None = None):
    """Never invoke mvnw here: its bootstrap may download Maven before -o is read."""
    config = backend_root / '.mvn/wrapper/maven-wrapper.properties'
    if not config.is_file():
        return None
    match = re.search(r'apache-maven-(\d+\.\d+\.\d+)-bin\.', config.read_text(encoding='utf-8'))
    if not match:
        return None
    binary = 'mvn.cmd' if (sys.platform == 'win32' if windows is None else windows) else 'mvn'
    cache = home / '.m2/wrapper/dists'
    candidates = sorted([*cache.glob(f'apache-maven-{match[1]}/**/bin/{binary}'),
                         *cache.glob(f'apache-maven-{match[1]}-bin/**/bin/{binary}')])
    return next((str(path) for path in candidates if path.is_file()), None)


def build_checks(root: Path, python: str, node: str | None, backend_root: Path | None, maven: str | None = 'mvn'):
    checks = [
        Check('python', [python, '-m', 'pytest', 'tests', '-q', f'--junitxml={root / "output/offline/python-tests.xml"}'], root),
        Check('frontend', [node, '--test', 'tests/web/core.test.mjs'] if node else [], root),
        Check('contracts', [python, 'tools/export_contracts.py'], root),
    ]
    if backend_root is not None:
        checks.append(Check('java-identity', [maven, '-o', '-q', '-pl', 'sky-server', '-am',
                     '-Dtest=CustomerAgentControllerTest,AgentControllerTest',
                     '-Dsurefire.failIfNoSpecifiedTests=false', 'test'] if maven else [], backend_root))
    return checks


_IDENTITY_CLASSES = ('com.sky.controller.admin.AgentControllerTest',
                     'com.sky.controller.user.CustomerAgentControllerTest')


def _report_snapshot(backend):
    result = {}
    for name in _IDENTITY_CLASSES:
        path = backend / f'sky-server/target/surefire-reports/TEST-{name}.xml'
        try:
            stat = path.stat()
            result[name] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            result[name] = None
    return result


def _java_evidence(backend, before):
    after = _report_snapshot(backend)
    count = 0
    try:
        for name in _IDENTITY_CLASSES:
            if after[name] is None or after[name] == before[name]:
                return None
            path = backend / f'sky-server/target/surefire-reports/TEST-{name}.xml'
            suite = ET.parse(path).getroot()
            tests = int(suite.attrib['tests'])
            if (suite.tag != 'testsuite' or suite.attrib.get('name') != name or tests <= 0
                    or any(int(suite.attrib.get(key, '0')) != 0 for key in ('skipped', 'errors', 'failures'))):
                return None
            count += tests
    except (OSError, ET.ParseError, KeyError, ValueError):
        return None
    return count


def run_checks(checks, executor=subprocess.run, timeout=180):
    results = []
    for check in checks:
        start = time.monotonic()
        code = None
        before = _report_snapshot(check.cwd) if check.name == 'java-identity' else None
        executed = None
        if not check.args:
            status = 'unavailable'
        else:
            try:
                result = executor(check.args, cwd=check.cwd, timeout=timeout, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                code = result.returncode
                status = 'passed' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                status = 'timeout'
            except OSError:
                status = 'unavailable'
        if status == 'passed' and check.name == 'java-identity':
            executed = _java_evidence(check.cwd, before)
            if executed is None:
                status = 'failed'
        results.append({'name': check.name, 'status': status, 'exit_code': code,
                        'executed_tests': executed,
                        'duration_seconds': round(time.monotonic() - start, 3)})
    return {'passed': bool(results) and all(r['status'] == 'passed' for r in results), 'checks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', help='Path to Node executable; otherwise discover it on PATH')
    parser.add_argument('--backend-root', type=Path, help='Optional Java repository; runs ONLY the two identity unit test classes with Maven -o')
    parser.add_argument('--maven', help='Optional installed Maven executable, never a wrapper bootstrap')
    args = parser.parse_args()
    output = ROOT / 'output/offline'
    output.mkdir(parents=True, exist_ok=True)
    maven = args.maven or shutil.which('mvn')
    if not maven and args.backend_root:
        maven = find_cached_maven(args.backend_root, Path.home())
    checks = build_checks(ROOT, sys.executable, args.node or shutil.which('node'), args.backend_root, maven=maven)
    report = run_checks(checks)
    report.update(generated_at=datetime.now(timezone.utc).isoformat(),
                  scope='offline contracts and unit tests; not model quality or integration',
                  java_requested=args.backend_root is not None)
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for result in report['checks']:
        print(f'{result["name"]}: {result["status"]} (exit={result["exit_code"]})')
    if args.backend_root is None:
        print('java-identity: not requested')
    print(f'Report: {output / "summary.json"}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
