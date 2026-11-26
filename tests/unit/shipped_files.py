# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The files of this repository that are published: every file that .gitignore does not exclude.

Tests that look for text or code that must not be in the repository read the files that ship, not local
scratch files. The list is built from .gitignore, without calling git, so that these tests also run on a
copy of the repository that has no .git directory.
"""
import fnmatch
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ignored_names():
    """The patterns of .gitignore. Each of them names a file or a directory at any depth (none has a slash
    inside, which this function checks, and none is negated)."""
    patterns = [".git"]
    for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.rstrip("/")
        assert "/" not in pattern and not pattern.startswith("!"), (
            f".gitignore pattern {line!r} needs more than a match on a file or directory name; "
            f"extend shipped_files() for it")
        patterns.append(pattern)
    return patterns


def shipped_files():
    """Paths, relative to the repository root, of all files that .gitignore does not exclude."""
    patterns = _ignored_names()

    def ignored(name):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    files = []
    for directory, directory_names, file_names in os.walk(REPO_ROOT):
        # Do not descend into ignored directories
        directory_names[:] = sorted(name for name in directory_names if not ignored(name))
        files += [(Path(directory) / name).relative_to(REPO_ROOT) for name in sorted(file_names) if not ignored(name)]
    return files
