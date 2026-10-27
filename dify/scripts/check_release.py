"""Reject machine-specific paths, credentials and unnecessary binary distributions."""
from __future__ import annotations
import re
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = {"backend", "deploy", "docs", "dsl", "evidence", "scripts", "tests"}
FILES = {"README.md", "LICENSE", ".env.example", ".gitignore", ".gitattributes", "requirements.txt", "requirements-dev.txt"}
PRIVATE_PARTS = {".env", ".venv", ".runtime", ".cache", ".tmp", "__pycache__", ".pytest_cache", "private"}
PATH_PATTERNS = [
    re.compile(rb"(?<![A-Za-z0-9])(?:[A-Za-z]):[\\/](?!/)"),
    re.compile(rb"/(?:mnt/[A-Za-z]|home/[^/\s]+|Users/[^/\s]+)/"),
]

def files():
    candidates = [ROOT / name for name in FILES]
    for folder in FOLDERS:
        candidates.extend((ROOT / folder).rglob("*"))
    for path in sorted(candidates, key=lambda p: p.relative_to(ROOT).as_posix()):
        if path.is_file() and not path.is_symlink() and not any(part in PRIVATE_PARTS for part in path.relative_to(ROOT).parts):
            if path.suffix not in {".pyc", ".zip", ".whl", ".gz"}:
                yield path

def check():
    config = dotenv_values(ROOT / ".env")
    secrets = [value.encode() for key, value in config.items() if value and len(value) >= 24
               and any(word in key for word in ("TOKEN", "SECRET", "KEY", "PASSWORD")) and not value.startswith("REPLACE_")]
    paths = list(files())
    for path in paths:
        data = path.read_bytes()
        assert not any(secret in data for secret in secrets), "Credentials found in " + str(path.relative_to(ROOT))
        if path.suffix != ".png":
            assert not any(pattern.search(data) for pattern in PATH_PATTERNS), "Machine path found in " + str(path.relative_to(ROOT))
    print(f"PASS: {len(paths)} release files; no machine-specific paths or configured credentials.")
    return paths

if __name__ == "__main__":
    check()
