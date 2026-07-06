"""Lightweight committed-file secret scanner for CI and local pre-push checks."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)\b(?:APP_TOKEN|GEMINI_API_KEY)\s*=\s*([^\s#]+)"),
]
ALLOW_VALUE_MARKERS = (
    "replace-with",
    "your-",
    "test-api-key",
    "dummy",
    "example",
    "placeholder",
    "llm",
    "你的",
)
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    "node_modules",
    "dist",
    "backups",
    "tmp",
}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
    )
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _is_allowed_assignment(match: re.Match[str]) -> bool:
    if not match.groups():
        return False
    value = match.group(1).strip().strip('"\'')
    if not value:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in ALLOW_VALUE_MARKERS)


def scan_file(path: Path) -> list[str]:
    if any(part in SKIP_PARTS for part in path.parts):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                if _is_allowed_assignment(match):
                    continue
                findings.append(f"{path}:{line_number}: potential secret: {match.group(0)}")
    return findings


def main() -> int:
    findings: list[str] = []
    for path in _tracked_files():
        findings.extend(scan_file(path))
    if findings:
        print("Secret scan failed:")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print("Secret scan passed: no committed secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
