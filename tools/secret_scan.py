"""Pre-publish secret & sensitive-artifact scanner for D:/sky-agent.

Verifies before `git add` / `git push`:
1. `.env`, `logs/`, `.venv/`, `output/`, `deploy/backups/` are excluded from tracking.
2. No trackable text file contains API keys (`sk-...`), JWTs (`eyJ...`), private keys,
   populated `DASHSCOPE_API_KEY=...`, or local user home paths (`C:/Users/<username>`).
"""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

_EXCLUDED_DIRS = frozenset({
    '.git',
    '.venv',
    'venv',
    '__pycache__',
    '.pytest_cache',
    '.idea',
    '.vscode',
    'logs',
    'output',
})
_EXCLUDED_REL_PREFIXES = (
    'deploy/backups/',
)
_EXCLUDED_FILENAMES = frozenset({'.env', '.DS_Store', 'Thumbs.db'})
_BINARY_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf', '.woff', '.woff2'})

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('api_key_sk', re.compile(r'\bsk-[a-zA-Z0-9_-]{20,}\b')),
    ('jwt_token', re.compile(r'\beyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{12,}\b')),
    ('private_key_pem', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('populated_dashscope_key', re.compile(r'^[ \t]*DASHSCOPE_API_KEY[ \t]*=[ \t]*(?!$|你的|your_|<|""|\'\')\S+', re.MULTILINE)),
    ('windows_user_home_path', re.compile(r'[cC]:[/\\][uU]sers[/\\][a-zA-Z0-9_.-]+[/\\]')),
)


def iter_trackable_files(root: Path) -> list[Path]:
    """Return sorted list of repository files that would be eligible for Git tracking."""
    files: list[Path] = []
    for path in root.rglob('*'):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
        rel_posix = path.relative_to(root).as_posix()
        if any(rel_posix.startswith(prefix) for prefix in _EXCLUDED_REL_PREFIXES):
            continue
        name = path.name
        if name in _EXCLUDED_FILENAMES:
            continue
        if name.startswith('.env.') and name != '.env.example':
            continue
        if name.endswith(('.pyc', '.pyo', '.pyd', '.log', '.jsonl')):
            continue
        files.append(path)
    return sorted(files)


def scan_repository(root: Path) -> dict:
    gitignore_path = root / '.gitignore'
    gitignore_text = gitignore_path.read_text(encoding='utf-8') if gitignore_path.is_file() else ''
    required_ignores = ('.env', 'logs/', 'output/', '.venv/', 'deploy/backups/')
    missing_ignores = [item for item in required_ignores if item not in gitignore_text]

    findings: list[dict] = []
    scanned_count = 0
    for file_path in iter_trackable_files(root):
        rel = file_path.relative_to(root).as_posix()
        if file_path.suffix.lower() in _BINARY_SUFFIXES:
            scanned_count += 1
            continue
        try:
            content = file_path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        scanned_count += 1
        # Skip synthetic pattern definitions inside this scanner or its dedicated unit test.
        if rel in ('tools/secret_scan.py', 'tests/test_secret_scan.py'):
            continue
        for rule_name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(content):
                line_no = content.count('\n', 0, match.start()) + 1
                # Never include the matched secret value in findings output!
                findings.append({
                    'file': rel,
                    'line': line_no,
                    'rule': rule_name,
                })

    passed = not missing_ignores and not findings
    return {
        'passed': passed,
        'scanned_files': scanned_count,
        'missing_gitignore_entries': missing_ignores,
        'findings_count': len(findings),
        'findings': findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Scan repository for secrets and local-path leaks')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()

    report = scan_repository(args.root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
