#!/usr/bin/env python3
"""Seal the pinned pure-Python jsonpath sdist as a wheel, without executing it.

jsonpath 0.82.2 has no upstream wheel. Copy only its module and published package
metadata from the hash-verified PyPI sdist; never run setup.py or import jsonpath.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

SOURCE_SHA256 = "d87ef2bcbcded68ee96bc34c1809b69457ecec9b0c4dd471658a12bd391002d1"
NAME = "jsonpath-0.82.2-py3-none-any.whl"
DIST = "jsonpath-0.82.2.dist-info"


def build(source: Path, output: Path) -> dict:
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError("Pinned PyPI source digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = set()
        for member in archive:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.name in names
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe source archive")
            names.add(member.name)
        module = archive.extractfile("jsonpath-0.82.2/jsonpath.py").read()
        metadata = archive.extractfile("jsonpath-0.82.2/PKG-INFO").read()
    if b"Name: jsonpath\n" not in metadata or b"Version: 0.82.2\n" not in metadata:
        raise ValueError("Unexpected source metadata")
    files = {
        "jsonpath.py": module,
        DIST + "/METADATA": metadata,
        DIST
        + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: Prism sealed-source 1\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        DIST + "/top_level.txt": b"jsonpath\n",
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, value in sorted(files.items()):
        value_hash = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + value_hash, len(value)])
    writer.writerow([DIST + "/RECORD", "", ""])
    files[DIST + "/RECORD"] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    path = output / NAME
    with path.open("xb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, value in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o100644 << 16
            wheel.writestr(info, value)
    return {
        "source_sha256": SOURCE_SHA256,
        "wheel": NAME,
        "wheel_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "module_sha256": hashlib.sha256(module).hexdigest(),
        "source_code_executed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output_dir)))
