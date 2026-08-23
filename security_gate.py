from __future__ import annotations
from pathlib import Path
import re

FORBIDDEN = {
    'TLS certificate verification disabled': (
        r'ssl\.CERT_NONE',
        r'check_hostname\s*=\s*False',
        r'_create_unverified_context',
        r'(?<![A-Za-z])verify\s*=\s*False',
    ),
    'destructive command': (
        r'\brm\s+-rf\b',
        r'\bchmod\s+777\b',
        r'\bDROP\s+TABLE\b',
    ),
}
SCAN_SUFFIXES = {'.py', '.ps1', '.bat', '.vbs', '.yml', '.yaml'}
EXCLUDED = {
    Path('.github/workflows/security-hardening-once.yml'),
    Path('security_gate.py'),
}
violations = []
for path in Path('.').rglob('*'):
    if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES or path in EXCLUDED:
        continue
    text = path.read_text(encoding='utf-8', errors='replace')
    for label, patterns in FORBIDDEN.items():
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                violations.append(f'{path}: {label}')
if violations:
    raise SystemExit('Security policy violation(s):\n' + '\n'.join(sorted(set(violations))))
print('Zero-tolerance static security gate passed.')
