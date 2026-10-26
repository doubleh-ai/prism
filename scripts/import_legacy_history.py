#!/usr/bin/env python3
"""Explicit offline import on operator-staged copies; never run at app startup."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.legacy_history import HistoryImportError, _open_directory, import_staged_history


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged-db", required=True, type=Path, help="existing offline SQLite copy")
    parser.add_argument(
        "--staged-project-root", required=True, type=Path, help="copied historical project root"
    )
    parser.add_argument("--username", help="restrict import to this existing owner")
    args = parser.parse_args(argv)
    directory = descriptor = None
    try:
        directory = _open_directory(args.staged_db.parent)
        descriptor = os.open(
            args.staged_db.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HistoryImportError("staged database must be a regular single-link file")
        with sqlite3.connect(args.staged_db.as_uri() + "?mode=rw", uri=True, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            named = os.stat(args.staged_db.name, dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
                raise HistoryImportError("staged database changed before import")
            result = import_staged_history(db, args.staged_project_root, username=args.username)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (HistoryImportError, OSError, sqlite3.Error) as exc:
        # Error classes only: paths, prompts and file contents stay private.
        print("staged history import failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        for fd in (descriptor, directory):
            if fd is not None:
                os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
