"""Package only reviewed source and evidence, with an archive-local checksum list."""
from __future__ import annotations
import hashlib
from pathlib import Path
import zipfile
from check_release import ROOT, check

def main():
    paths = check()
    destination = ROOT / ".runtime/release"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "echo_shop_submission.zip"
    manifest = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(ROOT).as_posix() + "\n" for p in paths)
    def add(archive, name, data):
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            add(archive, "dify/" + path.relative_to(ROOT).as_posix(), path.read_bytes())
        add(archive, "dify/checksums.sha256", manifest.encode("utf-8"))
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for line in archive.read("dify/checksums.sha256").decode().splitlines():
            expected, path = line.split("  ", 1)
            assert hashlib.sha256(archive.read("dify/" + path)).hexdigest() == expected
    print(f"PASS: {len(paths)} assets packaged in .runtime/release/echo_shop_submission.zip")
    print("sha256 " + hashlib.sha256(output.read_bytes()).hexdigest())

if __name__ == "__main__":
    main()
