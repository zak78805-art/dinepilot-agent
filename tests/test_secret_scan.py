"""Tests for pre-publish secret and local-path leak scanner (`tools/secret_scan.py`)."""
import json
from pathlib import Path

from tools.secret_scan import ROOT, scan_repository


def test_secret_scanner_detects_keys_jwts_and_missing_ignores_without_echoing_secret(tmp_path):
    (tmp_path / '.gitignore').write_text('.env\nlogs/\n', encoding='utf-8')
    fake_sk = 'sk-' + 'A9b8C7d6E5f4G3h2I1j0K9L8'
    fake_jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxMn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'
    (tmp_path / 'leak.txt').write_text(
        f'key={fake_sk}\njwt={fake_jwt}\nDASHSCOPE_API_KEY=real_val_123\n',
        encoding='utf-8',
    )
    # Ignored .env must NOT be scanned
    (tmp_path / '.env').write_text(f'DASHSCOPE_API_KEY={fake_sk}\n', encoding='utf-8')

    report = scan_repository(tmp_path)
    assert report['passed'] is False
    assert set(report['missing_gitignore_entries']) == {'output/', '.venv/', 'deploy/backups/'}
    rules = {f['rule'] for f in report['findings']}
    assert {'api_key_sk', 'jwt_token', 'populated_dashscope_key'} <= rules
    dumped = json.dumps(report)
    assert fake_sk not in dumped
    assert fake_jwt not in dumped


def test_actual_repository_passes_secret_scan():
    report = scan_repository(ROOT)
    assert report['passed'] is True, f"Secret scan failed: {report['findings']}"
