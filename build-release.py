#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ljc
"""Package only reviewed source/docs, never live diagnostic JSON or credentials."""
import hashlib
from pathlib import Path
import tarfile

HERE = Path(__file__).resolve().parent
VERSION = "0.1.0"
FILES = ["rtl8822bu_setup.py", "install.sh", "check.sh", "uninstall.sh", "README.md",
         "LICENSE", ".gitignore", "build-release.py", "tests/test_setup.py",
         "tests/integration_netns.py", "docs/PROCESS.md", "docs/UBUNTU26_MIGRATION.md",
         "docs/TESTING.md", "docs/SOURCES.md", ".github/workflows/test.yml"]
archive = HERE.parent / f"rtl8822bu-setup-v{VERSION}.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    for relative in FILES:
        path = HERE / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit("Not a reviewed regular file: " + relative)
        info = tar.gettarinfo(str(path), arcname="rtl8822bu-setup/" + relative)
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        with path.open("rb") as stream:
            tar.addfile(info, stream)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
checksum = archive.with_name(archive.name.removesuffix(".tar.gz") + ".sha256")
checksum.write_text(f"{digest}  {archive.name}\n")
print(archive)
print(checksum)
