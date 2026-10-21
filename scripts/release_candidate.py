#!/usr/bin/env python3
"""Build/verify inert Prism candidates from an explicit Git commit (stdlib only).

This tool never imports candidate Python, starts the application, accesses SSH,
installs dependencies, creates a release, or changes a running installation.
Dirty working-tree files are deliberately irrelevant: all input is read with Git
plumbing from the requested immutable commit. Output must be outside Git workspaces.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "release-manifest.json"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILES = 4096
MAX_TAR_BYTES = MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + MAX_FILES * 1024 + 10240
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
TOP_FILES = {"requirements.txt", "requirements-extra.txt", "CHANGELOG.md", "LICENSE"}
BUILTIN_FILES = {"skills/prism-research/CLAUDE.md", "skills/force-chinese/CLAUDE.md"}
REQUIRED_FILES = {
    "server/main.py",
    "server/config.py",
    "server/database.py",
    "server/release.py",
    "templates/app.html",
    "templates/login.html",
    "static/js/app.core.js",
    "skills/prism-research/CLAUDE.md",
    "requirements.txt",
    "requirements-extra.txt",
}
STATIC_SUFFIXES = {
    ".js",
    ".css",
    ".json",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
}
DENIED_PARTS = {
    "data",
    "logs",
    "reports",
    "sessions",
    "runs",
    "uploads",
    "backups",
    "backup",
    "archive",
    "archives",
    "privategit",
    "private-git",
    "old-git",
    "tracking",
    "private",
    "__pycache__",
    "node_modules",
    "cache",
}
DENIED_SUFFIXES = {
    ".db",
    ".db3",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".bundle",
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".7z",
    ".sparseimage",
    ".sparsebundle",
    ".pyc",
}
SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    (
        "provider_token",
        re.compile(
            rb"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{30,})"
        ),
    ),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("credential_url", re.compile(rb"(?i)https?://[^\s/:'\"]+:[^\s/@'\"]+@")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "literal_credential",
        re.compile(
            rb"(?i)(?<![\w])(?:[\"']?)(?:(?:[A-Z0-9]+_)*API_KEY|ANTHROPIC_AUTH_TOKEN|password|access_token|refresh_token|auth_token|token|cookie|authorization)[\"']?\s*[:=]\s*[\"'](?!Bearer *[\"'])([^\"'\r\n]{4,})[\"']"
        ),
    ),
)


class CandidateError(ValueError):
    """Reviewable failure containing categories and paths, never matched content."""


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        # A partial clone may otherwise fetch a missing object and invoke a
        # repository-configured transport or credential helper during cat-file.
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
    }


def _git(repo: Path, *args: str, allow_failure: bool = False) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "protocol.allow=never",
                "-c",
                "credential.helper=",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repo),
                *args,
            ],
            env=_git_env(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CandidateError("Git read unavailable") from None
    if result.returncode:
        if allow_failure:
            return b""
        raise CandidateError("Git read failed; check the repository and requested commit")
    return result.stdout


def _full_commit(repo: Path, commit: str) -> str:
    if not SHA1.fullmatch(commit):
        raise CandidateError("An explicit lowercase 40-character commit SHA is required")
    if _git(repo, "cat-file", "-t", commit).strip() != b"commit":
        raise CandidateError("The requested object is not a commit")
    return commit


def _path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if (
        not path
        or len(path) > 240
        or not path.isascii()
        or "\\" in path
        or ":" in path
        or path.startswith("/")
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not parts
    ):
        raise CandidateError("Unsafe archive path")
    return path


def allowed_path(path: str) -> bool:
    _path(path)
    parts = path.split("/")
    lower = [part.lower() for part in parts]
    if any(part.startswith(".") or part in DENIED_PARTS for part in lower):
        return False
    filename = lower[-1]
    if (
        PurePosixPath(filename).suffix in DENIED_SUFFIXES
        or re.search(r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$", filename)
        or filename in {"secret_key", "credentials", "credentials.json"}
    ):
        return False
    if path in TOP_FILES or path in BUILTIN_FILES:
        return True
    return (
        (parts[0] == "server" and path.endswith(".py"))
        or (parts[0] == "templates" and path.endswith(".html"))
        or (parts[0] == "static" and PurePosixPath(path).suffix.lower() in STATIC_SUFFIXES)
    )


def _scan(path: str, content: bytes) -> None:
    if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise CandidateError(f"Unmaterialized Git LFS pointer: {path}")
    for category, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise CandidateError(f"Sensitive-content review required ({category}): {path}")


def _version(content: bytes) -> str:
    try:
        module = ast.parse(content.decode("utf-8"))
        values = []
        for node in module.body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets
                )
                or isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "APP_VERSION"
            ):
                values.append(ast.literal_eval(node.value))
        if (
            len(values) == 1
            and isinstance(values[0], str)
            and len(values[0]) <= 80
            and VERSION.fullmatch(values[0])
        ):
            return values[0]
    except (ValueError, TypeError, SyntaxError, UnicodeDecodeError):
        pass
    raise CandidateError("server/release.py must declare one literal APP_VERSION")


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def collect_candidate(
    repo: Path, commit: str, allowed_tip: str | None = None
) -> tuple[dict, dict[str, bytes]]:
    """Read Git objects only; no source imports, hooks, checkout or dependency installs."""
    repo = Path(repo).resolve()
    _full_commit(repo, commit)
    if allowed_tip:
        _full_commit(repo, allowed_tip)
        _git(repo, "merge-base", "--is-ancestor", commit, allowed_tip)
    tree = _git(repo, "rev-parse", commit + "^{tree}").decode().strip()
    if not SHA1.fullmatch(tree):
        raise CandidateError("Invalid source tree")
    listing = _git(repo, "ls-tree", "-r", "-l", "-z", commit)
    selected: dict[str, bytes] = {}
    files = []
    total = 0
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise CandidateError("Unsupported Git tree entry") from None
        if not allowed_path(path):
            continue
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise CandidateError(f"Links and non-regular entries are not permitted: {path}")
        try:
            size = int(raw_size)
        except ValueError:
            raise CandidateError(
                "Git object unavailable; candidate requires a complete local snapshot"
            ) from None
        if size < 0:
            raise CandidateError("Invalid Git object size")
        if size > MAX_FILE_BYTES:
            raise CandidateError(f"Candidate file exceeds the size limit: {path}")
        total += size
        if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            raise CandidateError("Candidate exceeds the total size or file-count limit")
        content = _git(repo, "cat-file", "blob", object_id.decode("ascii"))
        if len(content) != size:
            raise CandidateError(f"Git object size mismatch: {path}")
        _scan(path, content)
        selected[path] = content
        files.append(
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": size,
                "mode": "0755" if mode == b"100755" else "0644",
            }
        )
    if not selected.keys() >= REQUIRED_FILES:
        raise CandidateError("Candidate is missing required runtime files")
    manifest = {
        "schema_version": 1,
        "kind": "candidate",
        "deployed": False,
        "version": _version(selected["server/release.py"]),
        "commit": commit,
        "source_tree": tree,
        "total_size": total,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    if len(_json_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise CandidateError("Manifest exceeds the size limit")
    return manifest, selected


def _outside_git(repo: Path, output_dir: Path) -> Path:
    output = Path(output_dir).expanduser().resolve()
    root = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if output == root or output.is_relative_to(root):
        raise CandidateError("Output must be outside every Git workspace")
    ancestor = output
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise CandidateError("Output parent is not a directory")
    # Also reject other/nested repositories, bare repositories and their .git areas.
    if _git(ancestor, "rev-parse", "--absolute-git-dir", allow_failure=True):
        raise CandidateError("Output must be outside every Git workspace")
    return output


def _tar_info(path: str, size: int, mode: int) -> tarfile.TarInfo:
    item = tarfile.TarInfo(path)
    item.size, item.mode, item.mtime = size, mode, 0
    item.uid = item.gid = 0
    item.uname = item.gname = ""
    return item


def build_candidate(repo: Path, commit: str, output_dir: Path, allowed_tip: str | None = None) -> dict:
    repo = Path(repo).resolve()
    output = _outside_git(repo, output_dir)
    manifest, contents = collect_candidate(repo, commit, allowed_tip)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = f"prism-{manifest['version']}-{commit}"
    archive_path, manifest_path = output / (stem + ".tar.gz"), output / (stem + ".manifest.json")
    if (
        archive_path.exists()
        or manifest_path.exists()
        or archive_path.is_symlink()
        or manifest_path.is_symlink()
    ):
        raise CandidateError("Candidate output already exists; choose another output directory")
    manifest_bytes = _json_bytes(manifest)
    with tempfile.TemporaryDirectory(prefix=".candidate-", dir=output) as temporary:
        staged = Path(temporary) / "candidate.tar.gz"
        with (
            staged.open("xb") as destination,
            gzip.GzipFile(
                fileobj=destination, mode="wb", filename="", mtime=0, compresslevel=9
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            archive.addfile(_tar_info(MANIFEST_NAME, len(manifest_bytes), 0o644), io.BytesIO(manifest_bytes))
            for item in manifest["files"]:
                archive.addfile(
                    _tar_info(item["path"], item["size"], int(item["mode"], 8)),
                    io.BytesIO(contents[item["path"]]),
                )
        verify_candidate(staged, expected_commit=commit, repo=repo)
        staged_manifest = Path(temporary) / "manifest.json"
        staged_manifest.write_bytes(manifest_bytes)
        # Atomic exclusive links expose only complete files and never overwrite
        # another run. The temporary and final files are on the same filesystem.
        os.link(staged, archive_path)
        try:
            os.link(staged_manifest, manifest_path)
        except BaseException:
            archive_path.unlink()
            raise
    return {
        "kind": "candidate",
        "deployed": False,
        "commit": commit,
        "version": manifest["version"],
        "archive": str(archive_path),
        "manifest": str(manifest_path),
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "file_count": len(contents),
    }


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError("Duplicate manifest key")
        result[key] = value
    return result


def _parse_manifest(content: bytes) -> dict:
    if len(content) > MAX_MANIFEST_BYTES:
        raise CandidateError("Manifest exceeds the size limit")
    try:
        manifest = json.loads(content, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CandidateError("Invalid candidate manifest") from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "kind",
        "deployed",
        "version",
        "commit",
        "source_tree",
        "total_size",
        "files",
    }:
        raise CandidateError("Invalid manifest fields")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != "candidate"
        or manifest["deployed"] is not False
    ):
        raise CandidateError("Manifest is not an inert candidate")
    if not all(
        isinstance(manifest[k], str) and SHA1.fullmatch(manifest[k]) for k in ("commit", "source_tree")
    ):
        raise CandidateError("Invalid manifest source identity")
    if (
        not isinstance(manifest["version"], str)
        or len(manifest["version"]) > 80
        or not VERSION.fullmatch(manifest["version"])
    ):
        raise CandidateError("Invalid manifest version")
    if type(manifest["total_size"]) is not int or not 0 <= manifest["total_size"] <= MAX_TOTAL_BYTES:
        raise CandidateError("Invalid manifest total size")
    files = manifest["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise CandidateError("Invalid manifest file count")
    paths = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size", "mode"}:
            raise CandidateError("Invalid manifest file entry")
        if not isinstance(item["path"], str) or not allowed_path(item["path"]):
            raise CandidateError("Manifest includes an excluded path")
        if (
            not isinstance(item["sha256"], str)
            or not SHA256.fullmatch(item["sha256"])
            or not isinstance(item["mode"], str)
            or item["mode"] not in {"0644", "0755"}
        ):
            raise CandidateError("Invalid manifest digest or mode")
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_FILE_BYTES:
            raise CandidateError("Invalid manifest file size")
        paths.append(item["path"])
    if (
        paths != sorted(set(paths))
        or not set(paths) >= REQUIRED_FILES
        or sum(item["size"] for item in files) != manifest["total_size"]
    ):
        raise CandidateError("Manifest file inventory is incomplete or inconsistent")
    return manifest


def verify_candidate(
    archive_path: Path,
    manifest_path: Path | None = None,
    *,
    expected_commit: str | None = None,
    repo: Path | None = None,
) -> dict:
    """Validate in memory without extracting or executing a single candidate file."""
    archive_path = Path(archive_path)
    with archive_path.open("rb") as source:
        compressed = source.read(MAX_ARCHIVE_BYTES + 1)
    if len(compressed) > MAX_ARCHIVE_BYTES:
        raise CandidateError("Compressed candidate exceeds the size limit")
    if compressed[:10] != b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff":
        raise CandidateError("Noncanonical gzip header or hidden metadata")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, MAX_TAR_BYTES + 1)
    except zlib.error:
        raise CandidateError("Invalid compressed candidate") from None
    if len(raw) > MAX_TAR_BYTES or len(raw) % 512 or not decoder.eof or decoder.unused_data:
        raise CandidateError("Invalid or oversized candidate archive")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_FILES + 1 or members[0].name != MANIFEST_NAME:
                raise CandidateError("Candidate manifest must be the first archive entry")
            names = []
            end = 0
            for item in members:
                _path(item.name)
                if (
                    not item.isreg()
                    or item.linkname
                    or item.pax_headers
                    or archive.pax_headers
                    or item.offset != end
                    or item.offset_data != item.offset + 512
                ):
                    raise CandidateError("Archive links, devices or extended headers are not permitted")
                limit = MAX_MANIFEST_BYTES if item.name == MANIFEST_NAME else MAX_FILE_BYTES
                if (
                    item.size > limit
                    or item.size < 0
                    or item.mode not in {0o644, 0o755}
                    or item.uid != 0
                    or item.gid != 0
                    or item.mtime != 0
                    or item.uname
                    or item.gname
                ):
                    raise CandidateError("Invalid archive size or metadata")
                # A parsed tar header can otherwise hide arbitrary bytes in unused
                # fields. Accept exactly the deterministic USTAR header we generate.
                canonical = _tar_info(item.name, item.size, item.mode).tobuf(format=tarfile.USTAR_FORMAT)
                if raw[item.offset : item.offset_data] != canonical:
                    raise CandidateError("Noncanonical archive header or hidden metadata")
                end = item.offset_data + ((item.size + 511) // 512) * 512
                if any(raw[item.offset_data + item.size : end]):
                    raise CandidateError("Hidden payload in archive padding")
                names.append(item.name)
            if len(names) != len(set(names)) or len(raw) - end < 1024 or any(raw[end:]):
                raise CandidateError("Duplicate files or trailing archive payload")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise CandidateError("Manifest is unreadable")
            manifest_bytes = stream.read()
            manifest = _parse_manifest(manifest_bytes)
            if manifest_path is not None:
                manifest_path = Path(manifest_path)
                if (
                    manifest_path.stat().st_size > MAX_MANIFEST_BYTES
                    or manifest_path.read_bytes() != manifest_bytes
                ):
                    raise CandidateError("External manifest does not match the archive")
            if expected_commit is not None and (
                not SHA1.fullmatch(expected_commit) or manifest["commit"] != expected_commit
            ):
                raise CandidateError("Candidate commit does not match the requested commit")
            if names != [MANIFEST_NAME, *[item["path"] for item in manifest["files"]]]:
                raise CandidateError("Archive files do not match the manifest")
            for member, item in zip(members[1:], manifest["files"], strict=True):
                stream = archive.extractfile(member)
                if stream is None:
                    raise CandidateError("Candidate file is unreadable")
                content = stream.read()
                if (
                    len(content) != item["size"]
                    or hashlib.sha256(content).hexdigest() != item["sha256"]
                    or member.mode != int(item["mode"], 8)
                ):
                    raise CandidateError(f"File integrity mismatch: {item['path']}")
                _scan(item["path"], content)
                if item["path"] == "server/release.py" and _version(content) != manifest["version"]:
                    raise CandidateError("Candidate version does not match its source")
    except tarfile.TarError:
        raise CandidateError("Invalid candidate tar") from None
    if repo is not None:
        if expected_commit is None:
            raise CandidateError("Source verification requires an explicit expected commit")
        expected, _ = collect_candidate(Path(repo), expected_commit)
        if expected != manifest:
            raise CandidateError("Candidate does not match the requested Git source tree")
    return {
        "valid": True,
        "kind": "candidate",
        "deployed": False,
        "commit": manifest["commit"],
        "version": manifest["version"],
        "source_tree": manifest["source_tree"],
        "source_verified": repo is not None,
        "provenance_verified": repo is not None,
        "file_count": len(manifest["files"]),
        "total_size": manifest["total_size"],
        "archive_sha256": hashlib.sha256(compressed).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Create an inert candidate; never deploy")
    build.add_argument("--repo", required=True, type=Path)
    build.add_argument("--commit", required=True)
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--allowed-tip", help="Require commit to be an ancestor of this full SHA")
    verify = commands.add_parser("verify", help="Verify without extracting or running candidate code")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--manifest", type=Path)
    verify.add_argument("--commit")
    verify.add_argument("--repo", type=Path)
    args = parser.parse_args(argv)
    try:
        result = (
            build_candidate(args.repo, args.commit, args.output_dir, args.allowed_tip)
            if args.command == "build"
            else verify_candidate(args.archive, args.manifest, expected_commit=args.commit, repo=args.repo)
        )
    except (CandidateError, OSError, tarfile.TarError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc)
                    if isinstance(exc, CandidateError)
                    else "Candidate file operation failed",
                }
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
